# Copyright (c) 2024, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
BM25 file-level scorer — loads bm25.json, scores queries against file summaries.

Returns top-K source filenames sorted by relevance.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .tokenizer import tokenize, STOPWORDS

K1 = 1.5
B = 0.75


class BM25Index:
    def __init__(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self._load(data)

    @classmethod
    def from_string(cls, json_str: str) -> "BM25Index":
        instance = cls.__new__(cls)
        data = json.loads(json_str)
        instance._load(data)
        return instance

    def _load(self, data: dict) -> None:
        self.n: int = data["n"]
        self.avgdl: float = data["avgdl"]
        self.df: dict[str, int] = data["df"]
        self.docs: list[dict] = data["docs"]

    def query(self, text: str, top_k: int = 3) -> list[dict]:
        tokens = tokenize(text)
        if not tokens:
            return []

        scores: list[tuple[float, dict]] = []
        for doc in self.docs:
            score = 0.0
            dl = doc["dl"]
            tf_map = doc["tf"]
            for t in tokens:
                tf = tf_map.get(t, 0)
                if tf == 0:
                    continue
                df = self.df.get(t, 0)
                idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
                numerator = tf * (K1 + 1)
                denominator = tf + K1 * (1 - B + B * dl / self.avgdl)
                score += idf * numerator / denominator
            if score > 0:
                scores.append((score, doc))

        scores.sort(key=lambda x: -x[0])
        results = []
        for score, doc in scores[:top_k]:
            results.append({
                "source": doc["src"],
                "score": round(score, 2),
            })
        return results


_NOISE_RE = re.compile(
    r"(?i)(private and confidential|all rights reserved|©.*?\.|\bconfidential\b)",
)

_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,4}\s+.+|"
    r"[A-Z][A-Za-z,\s&/()\-]{2,50}$|"
    r"\d+[\.\)]\s+[A-Z][A-Za-z,\s&/()\-]{2,50}$"
    r")",
    re.MULTILINE,
)


def clean_text(text: str) -> str:
    """Remove noise patterns and collapse whitespace."""
    cleaned = _NOISE_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def extract_headings(text: str) -> list[str]:
    """Extract section headings from document text."""
    headings = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        cleaned = clean_text(line)
        if not cleaned:
            continue
        if _HEADING_RE.match(cleaned) and _is_real_heading(cleaned):
            heading = re.sub(r"^#+\s*", "", cleaned).strip()
            if heading and heading not in headings:
                headings.append(heading[:80])
    return headings


def _is_real_heading(text: str) -> bool:
    text = text.strip()
    if text.endswith((".", "!", "?", ",")):
        return False
    if len(text.split()) > 8:
        return False
    if text[0].islower():
        return False
    return True


def extract_key_topics(text: str, max_topics: int = 20) -> list[str]:
    """Extract high-frequency meaningful terms from text."""
    cleaned = clean_text(text).lower()
    words = re.findall(r"[a-z][a-z0-9]+", cleaned)
    term_counts: dict[str, int] = defaultdict(int)
    for w in words:
        if w not in STOPWORDS and len(w) >= 3:
            term_counts[w] += 1
    sorted_terms = sorted(term_counts.items(), key=lambda x: -x[1])
    return [t for t, _ in sorted_terms[:max_topics]]


def enrich_text(text: str, title: str = "", tags: list[str] | str = "") -> str:
    """Enrich raw extracted text with headings and topics for BM25 indexing.

    Prepends title, extracted headings, and key topics to the cleaned text.
    This boosts recall by giving BM25 stronger signal on structural content.
    """
    headings = extract_headings(text)
    topics = extract_key_topics(text)

    parts = []
    if title:
        parts.append(title)
    if headings:
        parts.append(" ".join(headings))
    if topics:
        parts.append(" ".join(topics))
    if isinstance(tags, list) and tags:
        parts.append(" ".join(tags))
    elif isinstance(tags, str) and tags:
        parts.append(tags)
    parts.append(clean_text(text))

    return " ".join(parts)


def _compute_related(corpus: list[tuple[str, str]], min_shared: int = 5, max_links: int = 3) -> list[list[str]]:
    """Compute related file links based on term overlap between files."""
    file_terms: list[set[str]] = []
    titles: list[str] = []
    for src, text in corpus:
        cleaned = clean_text(text).lower()
        words = re.findall(r"[a-z][a-z0-9]+", cleaned)
        terms = {w for w in words if w not in STOPWORDS and len(w) >= 2}
        file_terms.append(terms)
        title = src.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        titles.append(title)

    links: list[list[str]] = [[] for _ in corpus]
    for i in range(len(corpus)):
        shared_counts: list[tuple[int, int]] = []
        for j in range(len(corpus)):
            if i == j:
                continue
            shared = len(file_terms[i] & file_terms[j])
            if shared >= min_shared:
                shared_counts.append((shared, j))
        shared_counts.sort(key=lambda x: -x[0])
        for _, j in shared_counts[:max_links]:
            links[i].append(titles[j])
    return links


def build_memory_md(corpus: list[tuple[str, str]]) -> str:
    """Build memory.md content from [(source_filename, text)] pairs.

    Generates per-file summaries with sections, key topics, and related
    file links. Used as agent fallback when source files are unavailable.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    related = _compute_related(corpus)

    lines = [
        "# Knowledge Memory",
        "",
        f"Generated: {now}",
        "",
    ]

    for i, (src, text) in enumerate(corpus):
        title = src.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        headings = extract_headings(text)
        topics = extract_key_topics(text)

        lines.append(f"## {title}")
        lines.append(f"Source: {src}")
        lines.append("")

        if headings:
            lines.append("Sections: " + ", ".join(headings))
            lines.append("")

        if topics:
            lines.append("Key topics: " + ", ".join(topics))
            lines.append("")

        if related[i]:
            links = ", ".join(f"[[{t}]]" for t in related[i])
            lines.append(f"Related: {links}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def build_index(corpus: list[tuple[str, str]]) -> dict:
    """Build the BM25 index dict from [(source_filename, text)] pairs.

    Output schema matches NanoRagBM25Scorer's parse:
      {n, avgdl, df: {term: int}, docs: [{src, dl, tf: {term: int}}]}

    For best recall, pass enriched text via enrich_text() before calling.
    Raw text works too — just with lower recall on heading/topic queries.
    """
    docs: list[dict] = []
    df: Counter = Counter()
    total_dl = 0
    for src, text in corpus:
        toks = tokenize(text)
        tf = Counter(toks)
        docs.append({"src": src, "dl": len(toks), "tf": dict(tf)})
        for term in tf:
            df[term] += 1
        total_dl += len(toks)
    n = len(docs)
    avgdl = (total_dl / n) if n else 0
    return {"n": n, "avgdl": avgdl, "df": dict(df), "docs": docs}


def generate_topic_metadata(
    corpus: list[tuple[str, str]],
) -> tuple[str, str, list[str]]:
    """Generate topic_name, description, and instructions from corpus.

    Derives meaningful metadata from file titles and key topics extracted
    from document content. Used to populate the manifest and drive
    AgentScript topic injection with content-aware routing instructions.

    Args:
        corpus: List of (source_filename, text_content) pairs.

    Returns:
        (topic_name, description, instructions_list)
    """
    all_topics: list[str] = []
    file_titles: list[str] = []

    for src, text in corpus:
        title = src.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        file_titles.append(title)
        topics = extract_key_topics(text, max_topics=10)
        all_topics.extend(topics)

    seen: set[str] = set()
    unique_topics: list[str] = []
    for t in all_topics:
        if t and t not in seen:
            seen.add(t)
            unique_topics.append(t)

    name_words = []
    for title in file_titles[:3]:
        for word in title.lower().split():
            cleaned = re.sub(r"[^a-z0-9]", "", word)
            if cleaned and len(cleaned) >= 3 and cleaned not in STOPWORDS:
                name_words.append(cleaned)
                if len(name_words) >= 3:
                    break
        if len(name_words) >= 3:
            break
    topic_name = "_".join(name_words[:3]) if name_words else "knowledge_search"
    topic_name = topic_name[:40]

    topic_part = ", ".join(unique_topics[:10])
    if topic_part:
        description = f"Route here for questions about {topic_part}"
    else:
        description = "Route here for questions about topics in this document library"
    if len(description) > 200:
        description = description[:197] + "..."

    subjects = (
        ", ".join(unique_topics[:8]) if unique_topics else "topics in this library"
    )
    instructions = [
        f"Search this library when the user asks about: {subjects}.",
        "This library contains multiple documents — search it for any question related to these subjects.",
        "Only answer from retrieved document content — never provide generic information or answer from general knowledge.",
        "When citing sources, ONLY use filenames returned in the sources output field. NEVER invent, guess, or infer filenames.",
    ]

    return topic_name, description, instructions


def extract_corpus_from_memory(memory_content: str) -> list[tuple[str, str]]:
    """Extract (filename, key_topics_text) pairs from memory.md content.

    Parses the structured memory.md format to reconstruct a lightweight
    corpus suitable for topic metadata generation without needing original files.
    """
    if memory_content.startswith("---\n"):
        end_idx = memory_content.find("\n---", 3)
        if end_idx == -1:
            return []
        body = memory_content[end_idx + 4:]
    else:
        body = memory_content

    corpus: list[tuple[str, str]] = []
    current_source = ""
    current_text_parts: list[str] = []

    for line in body.splitlines():
        if line.startswith("Source: "):
            if current_source and current_text_parts:
                corpus.append((current_source, " ".join(current_text_parts)))
            current_source = line[len("Source: "):]
            current_text_parts = []
        elif line.startswith("Sections: "):
            current_text_parts.append(line[len("Sections: "):])
        elif line.startswith("Key topics: "):
            current_text_parts.append(line[len("Key topics: "):])

    if current_source and current_text_parts:
        corpus.append((current_source, " ".join(current_text_parts)))

    return corpus


_TOPIC_METADATA_PROMPT = """You are generating metadata for a knowledge search topic in an Agentforce agent.

Given the following library content summary, generate THREE things:

1. TOPIC_NAME: A short snake_case name (max 40 chars) that describes what this library is about. This becomes the AgentScript topic identifier. Examples: "vehicle_diagnostics", "hr_policies", "product_specifications". Do NOT use the raw filename or library ID.

2. DESCRIPTION: A one-line description (max 200 chars) for the routing agent. It MUST start with "Route here for questions about " followed by the key domains this library covers across ALL files. This tells the reasoner WHEN to route to this topic. Examples:
   - "Route here for questions about NOx sensor diagnostics, hydraulic pressure faults, and engine repair procedures"
   - "Route here for questions about employee benefits, PTO policies, and workplace safety guidelines"

3. INSTRUCTIONS: 3-5 lines that tell the agent:
   - When to search this library (what kinds of user questions should trigger it)
   - How to handle results (use ONLY filenames from the sources output field — never invent or guess filenames)
   - Never answer from general knowledge — only from retrieved documents

Format your response EXACTLY like this (no other text):
TOPIC_NAME: <snake_case_name>
DESCRIPTION: <one-line description starting with "Route here for questions about ...">
INSTRUCTIONS:
| <line 1>
| <line 2>
| <line 3>
...

Library content summary:
{memory_summary}"""


def _build_condensed_summary(corpus: list[tuple[str, str]]) -> str:
    """Build a compact summary for the LLM prompt."""
    lines = []
    for src, text in corpus:
        title = src.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        topics = extract_key_topics(text, max_topics=10)
        lines.append(f"File: {title}")
        if topics:
            lines.append(f"  Topics: {', '.join(topics)}")
        lines.append("")
    return "\n".join(lines)


def _parse_llm_frontmatter_response(
    response: str,
) -> tuple[str, str, list[str]] | None:
    """Parse the structured LLM response into (topic_name, description, instructions)."""
    lines = response.strip().splitlines()

    topic_name = ""
    description = ""
    instruction_lines: list[str] = []
    in_instructions = False

    for line in lines:
        if line.startswith("TOPIC_NAME:"):
            raw = line[len("TOPIC_NAME:"):].strip().strip('"')
            topic_name = re.sub(r"[^a-z0-9_]", "_", raw.lower())[:40]
        elif line.startswith("DESCRIPTION:"):
            description = line[len("DESCRIPTION:"):].strip().strip('"')
        elif line.strip() == "INSTRUCTIONS:":
            in_instructions = True
        elif in_instructions:
            stripped = line.strip()
            if stripped.startswith("|"):
                instruction_lines.append(stripped[1:].strip())
            elif stripped:
                instruction_lines.append(stripped)

    if not topic_name or not description or not instruction_lines:
        return None

    if len(description) > 200:
        description = description[:197] + "..."

    return topic_name, description, instruction_lines


def _get_org_jwt(session_id: str, instance_url: str) -> str | None:
    """Get orgJWT via NamedUser bootstrap endpoint.

    Uses the existing SF CLI session_id to obtain a JWT suitable for
    the global LLM Gateway (api.salesforce.com/ai/gpt/v1).
    """
    import requests as _requests
    import logging

    logger = logging.getLogger(__name__)

    url = f"{instance_url}/agentforce/bootstrap/nameduser"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": f"sid={session_id}",
    }

    try:
        resp = _requests.get(url, headers=headers, timeout=30, allow_redirects=False)
        content_type = resp.headers.get("Content-Type", "")

        if resp.status_code == 200 and content_type.startswith("text/html"):
            logger.debug("NamedUser bootstrap returned HTML redirect")
            return None

        body_text = resp.text or ""
        if body_text.startswith("while(1);"):
            body_text = body_text[9:]

        import json as _json
        payload = _json.loads(body_text)
        jwt_token = payload.get("access_token")
        if not jwt_token:
            logger.debug("Bootstrap response missing access_token: %s", list(payload.keys()))
            return None

        return jwt_token
    except Exception as exc:
        logger.debug("NamedUser bootstrap failed: %s", str(exc)[:200])
        return None


def generate_topic_metadata_llm(
    corpus: list[tuple[str, str]],
    sf=None,
) -> tuple[str, str, list[str]] | None:
    """Generate topic metadata via global LLM Gateway. Returns None on failure.

    Obtains an orgJWT from the existing SF session, then calls the global
    Salesforce LLM Gateway (api.salesforce.com/ai/gpt/v1/chat/generations).
    No AiApplication registration needed — just an authenticated org session.

    Falls back gracefully — callers should use generate_topic_metadata()
    as the deterministic fallback.
    """
    import os
    import logging

    logger = logging.getLogger(__name__)

    if not corpus:
        return None

    if sf is None:
        return None

    instance_url = f"https://{sf.sf_instance}"
    jwt_token = _get_org_jwt(sf.session_id, instance_url)
    if not jwt_token:
        logger.warning("Could not obtain orgJWT — LLM topic generation unavailable")
        return None

    model = os.environ.get("LLM_MODEL", "llmgateway__BedrockAnthropicClaude45Opus")
    domain = sf.sf_instance.split(".")[0] if sf.sf_instance else ""
    is_sandbox = "test" in domain or "scratch" in domain or ".cs" in sf.sf_instance
    base_url = "https://test.api.salesforce.com/ai/gpt/v1" if is_sandbox else "https://api.salesforce.com/ai/gpt/v1"

    summary = _build_condensed_summary(corpus)
    prompt = _TOPIC_METADATA_PROMPT.format(memory_summary=summary)

    try:
        import requests
        url = f"{base_url}/chat/generations"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "x-sfdc-app-context": "EinsteinGPT",
            "x-client-feature-id": "nextGenAuthoring",
        }
        body = {
            "messages": [{"role": "user", "content": prompt}],
            "model": model,
            "generation_settings": {
                "max_tokens": 1024,
                "parameters": {"temperature": 0},
            },
        }
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code >= 400:
            logger.warning(
                "LLM Gateway call failed: %s %s", resp.status_code, resp.text[:200]
            )
            return None

        data = resp.json()
        generation_details = data.get("generation_details", {})
        generations = generation_details.get("generations", [])
        text = generations[0].get("content", "") if generations else ""

        if not text:
            logger.warning("LLM Gateway returned empty response")
            return None

        return _parse_llm_frontmatter_response(text)

    except Exception as exc:
        logger.warning("LLM topic generation failed: %s", str(exc)[:200])
        return None
