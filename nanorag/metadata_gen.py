# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Render + strip AgentScript topic blocks for nanoRag libraries.

Attach/detach lives on top of Salesforce's NextGen Authoring API — we
POST AgentScript source to ``/services/data/v66.0/nextgen-authoring/projects``
and let Salesforce's compiler re-materialize the GenAi bundle. This
module owns the two text transforms that path needs:

- ``render_library_topic_afscript``: builds the ``topic search_<lib>:``
  block (plus matching ``actions: search_<lib>:``) to inject.
- ``strip_library_topic_afscript``: the inverse — drop both blocks by
  name, idempotently.

At attach time, topic metadata (topic_name, description, instructions)
is read from manifest.yaml. On first attach, the LLM generates this
metadata (with a deterministic fallback) and persists it to the manifest.
Subsequent attaches read directly from manifest — no memory.md fetch.

No Jinja templates, no XML — AgentScript is whitespace-sensitive but
structurally simple enough that a careful line-scanner is more
predictable than a parser. Tests in test_metadata_gen.py pin the exact
output.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

DEFAULT_AGENT_MODEL_URI = "model://sfdc_ai__DefaultGPT54_03_05"


def parse_memory_frontmatter(memory_content: str) -> Dict[str, Any]:
    """Extract YAML frontmatter from memory.md content.

    Expected format:
        ---
        topic_name: vehicle_diagnostics
        description: "Covers ..."
        instructions:
          - "Search when ..."
          - "Only answer from ..."
        ---
        # Knowledge Memory
        ...

    Returns a dict with keys: topic_name, description, instructions (list).
    Returns empty dict when frontmatter is missing or malformed.
    """
    if not memory_content or not memory_content.startswith("---"):
        return {}

    end_idx = memory_content.find("\n---", 3)
    if end_idx == -1:
        return {}

    frontmatter_block = memory_content[4:end_idx]
    result: Dict[str, Any] = {}
    instructions: List[str] = []
    in_instructions = False

    for line in frontmatter_block.splitlines():
        if line.startswith("topic_name:"):
            result["topic_name"] = line[len("topic_name:") :].strip().strip('"')
            in_instructions = False
        elif line.startswith("description:"):
            result["description"] = line[len("description:") :].strip().strip('"')
            in_instructions = False
        elif line.startswith("source:"):
            result["source"] = line[len("source:") :].strip().strip('"')
            in_instructions = False
        elif line.strip() == "instructions:":
            in_instructions = True
        elif in_instructions and line.strip().startswith("- "):
            instructions.append(line.strip()[2:].strip().strip('"'))
        elif not line.strip().startswith("-") and not line.strip() == "":
            in_instructions = False

    if instructions:
        result["instructions"] = instructions
    return result


def resolve_topic_id(library_name: str, memory_content: str | None) -> str:
    """Derive the AgentScript topic identifier.

    Uses ``topic_name`` from memory.md frontmatter when available
    (LLM-generated or deterministic at build time). Falls back to
    ``library_name`` directly. This keeps the AgentScript human-readable
    (e.g., ``search_vehicle_diagnostics``) regardless of how ugly the
    raw library_name is.
    """
    meta = parse_memory_frontmatter(memory_content or "")
    generated_name = meta.get("topic_name")
    if generated_name:
        return generated_name
    return library_name


def _topic_id_to_title(topic_id: str) -> str:
    """Convert a snake_case topic_id to Title Case for display labels."""
    return " ".join(word.capitalize() for word in topic_id.split("_") if word)


def render_library_topic_afscript(
    *,
    library_name: str,
    description: str = "",
    agent_script: str = "",
    memory_content: str | None = None,
    topic_id_override: str | None = None,
    topic_metadata: Dict[str, Any] | None = None,
) -> Tuple[str, str]:
    """Return ``(topic_block, action_block)`` for a nanoRag library.

    Uses ``topic_metadata`` dict (from manifest) as primary source for
    description and instructions. Falls back to parsing memory_content
    frontmatter for backwards compatibility.

    When ``topic_id_override`` is provided, it is used instead of the
    resolved topic_id — this handles name collisions where the
    LLM-generated topic_name matches an existing block from another library.

    Each block ends with a trailing newline so concatenation onto
    existing AgentScript doesn't collapse adjacent headers.
    """
    block_keyword = "subagent"

    # Resolve the AgentScript-visible identifier
    meta = topic_metadata or parse_memory_frontmatter(memory_content or "")
    topic_id = topic_id_override or meta.get("topic_name") or library_name

    topic_desc = meta.get("description") or description or library_name
    instructions_list = meta.get("instructions") or []

    label = f"NanoRAG Search {_topic_id_to_title(topic_id)}"

    # Format instructions block
    if instructions_list:
        instructions = ""
        for inst in instructions_list:
            instructions += f"            | {inst}\n"
    else:
        instructions = (
            "            | Search this library when the user asks a question.\n"
            "            | Only answer from retrieved document content — never from "
            "general knowledge.\n"
            "            | When citing sources, ONLY use filenames returned in the "
            "sources output. NEVER invent or guess filenames.\n"
        )

    # Subagent block uses nanorag_ prefix for branding; action stays search_{topic_id}
    block_name = f"nanorag_{topic_id}"

    topic_block = (
        f"{block_keyword} {block_name}:\n"
        f'    label: "{label}"\n'
        f'    description: "{topic_desc}"\n'
        f"    reasoning:\n"
        f"        instructions: ->\n"
        f"{instructions}"
        f"        actions:\n"
        f"            do_search: @actions.search_{topic_id}\n"
        f"                with userQuery = ...\n"
        f'                with libraryName = "{library_name}"\n'
    )

    action_block = (
        f"    actions:\n"
        f"        search_{topic_id}:\n"
        f'            target: "apex://NanoRAGQueryService"\n'
        f'            description: "Search the knowledge library for relevant documents"\n'
        f"            inputs:\n"
        f"                userQuery: string\n"
        f"                libraryName: string\n"
        f"            outputs:\n"
        f"                fileContent: string\n"
        f"                sources: list[object]\n"
        f'                    complex_data_type_name: "lightning__textType"\n'
        f"                reasoning: string\n"
    )
    return topic_block, action_block


_TOP_LEVEL_SECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]*:\s*$")


def _is_top_level_header(line: str) -> bool:
    """A line is a top-level header if it has no leading whitespace and
    ends with ``:``. Used as the stop condition when scanning a block."""
    if not line or line[0] in (" ", "\t"):
        return False
    return bool(_TOP_LEVEL_SECTION_RE.match(line.rstrip("\n")))


def library_topic_exists(afscript: str, library_name: str, **_kwargs) -> bool:
    """True iff a nanoRag topic for this library already appears.

    Checks for ``with libraryName = "{library_name}"`` which is the
    authoritative marker — stable regardless of topic renames or
    LLM-generated topic_name collisions.
    """
    marker = f'with libraryName = "{library_name}"'
    return marker in afscript


def _topic_name_collides(afscript: str, topic_id: str) -> bool:
    """True if a block named ``nanorag_{topic_id}`` already exists in the source."""
    block_name = f"nanorag_{topic_id}"
    pattern = rf"(?m)^(?:topic|subagent)\s+{re.escape(block_name)}\s*:"
    return bool(re.search(pattern, afscript))


def inject_library_topic_afscript(
    afscript: str,
    *,
    library_name: str,
    description: str = "",
    memory_content: str | None = None,
    topic_metadata: Dict[str, Any] | None = None,
) -> Tuple[str, bool]:
    """Insert topic + action blocks into ``afscript``.

    Returns ``(new_source, changed)``. Idempotent — when the topic is
    already present, returns the source unchanged.

    ``topic_metadata`` is the preferred source for topic_name, description,
    and instructions (read from manifest). Falls back to parsing
    ``memory_content`` frontmatter for backwards compatibility.
    """
    if library_topic_exists(afscript, library_name):
        return afscript, False

    meta = topic_metadata or parse_memory_frontmatter(memory_content or "")

    # Resolve topic_id; fall back to library_name on collision
    topic_id = meta.get("topic_name") or library_name
    if _topic_name_collides(afscript, topic_id):
        logger.info(
            "nanorag.attach.topic_name_collision",
            extra={
                "library_name": library_name,
                "colliding_topic_id": topic_id,
                "fallback_topic_id": library_name,
            },
        )
        topic_id = library_name

    topic_block, action_block = render_library_topic_afscript(
        library_name=library_name,
        description=description,
        agent_script=afscript,
        memory_content=memory_content,
        topic_id_override=topic_id,
        topic_metadata=meta,
    )

    # Append topic block + action block (which is part of the topic) at the end.
    prefix = afscript.rstrip("\n") + "\n\n" if afscript.strip() else ""
    new_source = prefix + topic_block + action_block

    # Inject a transition action into the start_agent/topic_selector so
    # the router knows to route to our new topic.
    block_name = f"nanorag_{topic_id}"
    topic_desc = meta.get("description") or description or ""
    new_source = _inject_transition_to_router(new_source, block_name, topic_desc)

    # Inject a priority instruction into the router so nanoRag takes
    # precedence over other subagents for factual/document questions.
    new_source = _inject_router_priority_instruction(new_source, block_name, topic_desc)

    return new_source, True


def _inject_transition_to_router(
    afscript: str, library_name: str, topic_desc: str = ""
) -> str:
    """Add a go_<lib> transition in the start_agent/agent_router reasoning.actions block.

    Handles both old format (start_agent topic_selector, @topic.) and
    new format (start_agent agent_router, @subagent.).
    """
    # Check for existing transition — also check the un-prefixed form for
    # backwards compat with agents attached before the nanorag_ prefix.
    bare_name = library_name.removeprefix("nanorag_")
    prefixes = ("go_", "go_search_", "go_to_search_")
    if any(f"{p}{n}:" in afscript for p in prefixes for n in (library_name, bare_name)):
        return afscript

    ref = f"@subagent.{library_name}"
    go_prefix = "go"

    # Build description from topic metadata — tells classifier WHEN to route here
    if not topic_desc:
        topic_desc = (
            "User asks a question that could be answered from the "
            "uploaded knowledge documents."
        )
    desc_line = f'                description: "{topic_desc}"\n'

    transition_line = (
        f"            {go_prefix}_{library_name}: @utils.transition to {ref}\n"
        + desc_line
    )

    lines = afscript.splitlines(keepends=True)
    last_goto_idx = None
    in_router = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("start_agent "):
            in_router = True
        elif in_router and not line[0:1].isspace() and stripped:
            in_router = False
        if in_router and "@utils.transition" in line:
            last_goto_idx = i

    if last_goto_idx is not None:
        # Find the end of the last transition block (skip its description line)
        insert_idx = last_goto_idx + 1
        while insert_idx < len(lines):
            next_line = lines[insert_idx]
            next_stripped = next_line.strip()
            if next_stripped.startswith("description:") or (
                next_stripped
                and not next_stripped.startswith("go_")
                and "@utils.transition" not in next_stripped
                and len(next_line) - len(next_line.lstrip()) > 12
            ):
                insert_idx += 1
            else:
                break
        lines.insert(insert_idx, transition_line)
        return "".join(lines)

    return afscript


def _inject_router_priority_instruction(
    afscript: str, block_name: str, topic_desc: str
) -> str:
    """Inject a priority instruction into the router's instructions block.

    Tells the router to prefer the nanoRag subagent for factual/document
    questions, even when another subagent covers a similar domain.
    """
    marker = f"ALWAYS route to @subagent.{block_name}"
    if marker in afscript:
        return afscript

    scope = (topic_desc or "the attached knowledge library").replace("\n", " ").strip()

    lines = afscript.splitlines(keepends=True)
    in_router = False
    last_instruction_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("start_agent "):
            in_router = True
        elif in_router and not line[0:1].isspace() and stripped:
            in_router = False
        if in_router and stripped.startswith("| "):
            last_instruction_idx = i

    if last_instruction_idx is not None:
        prefix = lines[last_instruction_idx].split("| ")[0]
        instruction = (
            f"{prefix}| For questions about {scope}, "
            f"ALWAYS route to @subagent.{block_name} first. "
            f"Only route to other subagents for live actions "
            f"(creating records, lookups, escalation).\n"
        )
        lines.insert(last_instruction_idx + 1, instruction)
        return "".join(lines)

    return afscript


def _find_top_level_header_index(lines: List[str], header: str) -> int | None:
    """Return the index of the first top-level ``header`` line, or None."""
    target = header.rstrip(":").strip()
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped == f"{target}:" or stripped.rstrip() == f"{target}:":
            return i
    return None


def strip_library_topic_afscript(
    afscript: str, library_name: str, memory_content: str | None = None
) -> Tuple[str, bool]:
    """Remove the nanoRag topic block AND matching action sub-block.

    Primary strategy: find the top-level block that contains
    ``with libraryName = "{library_name}"`` — this is stable regardless
    of topic renames by users or LLM-generated name changes across rebuilds.

    Falls back to name-based matching (topic_id, legacy formats) when
    the libraryName marker isn't found.

    Returns ``(new_source, changed)``.
    Idempotent — returns unchanged source + ``False`` when neither block
    exists.
    """
    changed = False
    new_source = afscript

    # Primary: find block by libraryName marker in the action body
    new_source, t_removed = _strip_block_by_library_marker(new_source, library_name)

    if not t_removed:
        # Fallback: name-based matching (nanorag_ prefix + legacy formats)
        topic_id = resolve_topic_id(library_name, memory_content)
        candidate_names = [
            f"nanorag_{topic_id}",
            topic_id,
            f"search_{topic_id}",
            f"search_{library_name}",
        ]
        for name in candidate_names:
            for keyword in ("subagent", "topic"):
                new_source, t_removed = _strip_top_level_block(
                    new_source, f"{keyword} {name}:"
                )
                if t_removed:
                    break
            if t_removed:
                break

    if t_removed:
        changed = True

    # Strip standalone action block (try new name, then legacy)
    topic_id = resolve_topic_id(library_name, memory_content)
    new_source, a_removed = _strip_nested_action_block(new_source, topic_id)
    if not a_removed:
        new_source, a_removed = _strip_nested_action_block(new_source, library_name)
    if a_removed:
        changed = True

    new_source = re.sub(r"\n{3,}", "\n\n", new_source)
    return new_source, changed


def _strip_block_by_library_marker(
    afscript: str, library_name: str
) -> Tuple[str, bool]:
    """Find and remove the top-level block containing the libraryName marker.

    Also removes the corresponding ``go_*`` transition line from the
    router block that references this subagent.

    This is the most robust strip strategy: it doesn't depend on the
    topic/subagent name (which the LLM can change on rebuild, or the
    user can rename manually). It anchors on the action's libraryName
    parameter which is always the raw library identifier.
    """
    marker = f'with libraryName = "{library_name}"'
    lines = afscript.splitlines(keepends=True)

    marker_idx = None
    for i, line in enumerate(lines):
        if marker in line:
            marker_idx = i
            break
    if marker_idx is None:
        return afscript, False

    # Walk backward to find the owning top-level block header
    block_start = 0
    subagent_name = ""
    for i in range(marker_idx - 1, -1, -1):
        if _is_top_level_header(lines[i]):
            block_start = i
            # Extract the subagent/topic name from header like "subagent foo:"
            header = lines[i].rstrip().rstrip(":")
            parts = header.split(None, 1)
            if len(parts) == 2:
                subagent_name = parts[1].strip()
            break

    # Walk forward to find end (next top-level header or EOF)
    block_end = len(lines)
    for j in range(marker_idx + 1, len(lines)):
        if _is_top_level_header(lines[j]):
            block_end = j
            break

    result = "".join(lines[:block_start] + lines[block_end:])

    # Strip the router transition line that references this subagent
    if subagent_name:
        result = _strip_router_transition(result, subagent_name)
        result = _strip_router_priority_instruction(result, subagent_name)

    return result, True


def _strip_router_priority_instruction(afscript: str, block_name: str) -> str:
    """Remove the priority instruction line for a nanoRag subagent from the router."""
    marker = f"ALWAYS route to @subagent.{block_name}"
    lines = afscript.splitlines(keepends=True)
    filtered = [line for line in lines if marker not in line]
    if len(filtered) == len(lines):
        return afscript
    return "".join(filtered)


def _strip_router_transition(afscript: str, subagent_name: str) -> str:
    """Remove the ``go_*`` transition + its description line from the router."""
    lines = afscript.splitlines(keepends=True)
    transition_idx = None
    for i, line in enumerate(lines):
        if f"@subagent.{subagent_name}" in line or f"@topic.{subagent_name}" in line:
            if "@utils.transition" in line:
                transition_idx = i
                break
    if transition_idx is None:
        return afscript

    # Remove the transition line and any immediately following description line
    end_idx = transition_idx + 1
    if end_idx < len(lines):
        next_stripped = lines[end_idx].strip()
        if next_stripped.startswith("description:"):
            end_idx += 1

    return "".join(lines[:transition_idx] + lines[end_idx:])


def _strip_top_level_block(afscript: str, header_line: str) -> Tuple[str, bool]:
    """Delete a whole top-level block whose first line matches ``header_line``.

    Block ends at the next top-level header (line with zero indent
    ending in ``:``) or EOF. Only matches exact zero-indent headers.
    """
    lines = afscript.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\n").rstrip() == header_line:
            start = i
            break
    if start is None:
        return afscript, False
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _is_top_level_header(lines[j]):
            end = j
            break
    return "".join(lines[:start] + lines[end:]), True


def _strip_nested_action_block(afscript: str, library_name: str) -> Tuple[str, bool]:
    """Remove a ``    search_<library>:`` sub-block under the ``actions:`` section.

    Action blocks are indented one level (4 spaces by convention). Block
    ends at the next line with equal-or-shallower indent or EOF. When
    removing the block leaves the ``actions:`` section empty, the
    section header is removed too — keeps the source clean.
    """
    lines = afscript.splitlines(keepends=True)
    actions_idx = _find_top_level_header_index(lines, "actions:")
    if actions_idx is None:
        return afscript, False

    target_name = f"search_{library_name}"
    block_start = None
    block_indent = None
    for i in range(actions_idx + 1, len(lines)):
        line = lines[i]
        if _is_top_level_header(line):
            break
        stripped = line.strip()
        if stripped == f"{target_name}:" or stripped.startswith(f"{target_name}:"):
            block_start = i
            block_indent = len(line) - len(line.lstrip(" "))
            break
    if block_start is None:
        return afscript, False

    block_end = len(lines)
    for j in range(block_start + 1, len(lines)):
        line = lines[j]
        if _is_top_level_header(line):
            block_end = j
            break
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= (block_indent or 0):
            block_end = j
            break

    new_lines = lines[:block_start] + lines[block_end:]

    section_has_body = False
    for k in range(actions_idx + 1, len(new_lines)):
        line = new_lines[k]
        if _is_top_level_header(line):
            break
        if line.strip():
            section_has_body = True
            break
    if not section_has_body:
        drop_end = actions_idx + 1
        while drop_end < len(new_lines) and new_lines[drop_end].strip() == "":
            drop_end += 1
        new_lines = new_lines[:actions_idx] + new_lines[drop_end:]

    return "".join(new_lines), True


BOOTSTRAP_AGENTSCRIPT_TEMPLATE = """system:
    instructions: "You are an AI Agent."
    messages:
        welcome: |
            Hi, I'm an AI assistant. How can I help you?
        error: "Sorry, it looks like something has gone wrong."

model_config:
    model: "{model_uri}"

config:
    developer_name: "{agent_name}"
    agent_label: "{agent_label}"
    description: "AI agent with knowledge search capabilities"

language:
    default_locale: "en_US"
    additional_locales: ""
    all_additional_locales: False

start_agent agent_router:
    label: "Agent Router"
    description: "Welcome the user and determine the appropriate subagent based on user input"
    reasoning:
        instructions: ->
            | Select the best tool to call based on conversation history and user's intent.
        actions:
            go_to_escalation: @utils.transition to @subagent.escalation
            go_to_off_topic: @utils.transition to @subagent.off_topic

subagent escalation:
    label: "Escalation"
    description: "Handles requests from users who want to transfer or escalate their conversation to a live human agent."
    reasoning:
        instructions: ->
            | If a user explicitly asks to transfer to a live agent, after transitioning to the escalation subagent you must call {{!@actions.escalate_to_human}} to complete the escalation.
              If escalation to a live agent fails for any reason, acknowledge the issue and ask the user whether they would like to log a support case instead.
        actions:
            escalate_to_human: @utils.escalate
                description: "Call this tool if the user indicates that they wish to escalate to a human agent."

subagent off_topic:
    label: "Off Topic"
    description: "Redirect conversation to relevant topics when user request goes off-topic"
    reasoning:
        instructions: ->
            | Your job is to redirect the conversation to relevant topics politely and succinctly.
              The user request is off-topic. NEVER answer general knowledge questions. Only respond to general greetings and questions about your capabilities.
"""


def bootstrap_agentscript(agent_name: str) -> str:
    """Return a minimal AgentScript source that will compile. Used only
    on first attach when we can't retrieve the project's existing source.
    """
    agent_label = agent_name.replace("_", " ").title()
    return BOOTSTRAP_AGENTSCRIPT_TEMPLATE.format(
        agent_name=agent_name,
        agent_label=agent_label,
        model_uri=DEFAULT_AGENT_MODEL_URI,
    )


# ---------------------------------------------------------------------------
# Model config injection
# ---------------------------------------------------------------------------

_NESTED_MODEL_CONFIG_RE = re.compile(
    r"^([ \t]+)model_config:.*?(?=^\1\S|\Z)", re.MULTILINE | re.DOTALL
)


def ensure_model_config(afscript: str) -> str:
    """Ensure a top-level model_config block exists in the AgentScript.

    Also strips nested (indented) model_config blocks that cause
    MODEL_CONFIGURATION_NOT_SUPPORTED_AT_TOPIC_LEVEL errors.
    Returns the source unchanged only if already correct.
    """
    cleaned = _NESTED_MODEL_CONFIG_RE.sub("", afscript)

    if "\nmodel_config:" in cleaned or cleaned.startswith("model_config:"):
        return cleaned if cleaned != afscript else afscript

    block = f'\nmodel_config:\n    model: "{DEFAULT_AGENT_MODEL_URI}"\n'
    m = re.search(r"^config:", cleaned, re.MULTILINE)
    if m:
        result = cleaned[: m.start()] + block + "\n" + cleaned[m.start() :]
    else:
        result = cleaned.rstrip("\n") + "\n" + block

    logger.info("nanorag.attach.model_config_injected")
    return result
