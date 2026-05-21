# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Attach/detach a nanoRag library to/from an Agentforce agent.

Uses Salesforce's NextGen Authoring API to inject (or strip) a library
topic block into the agent's AgentScript source. Flow:

1. Load the library's manifest.json from the org (ContentVersion).
2. GET /nextgen-authoring/projects to find the target agent by apiName.
3. Fetch current AgentScript source (probe NextGen GET, fall back to
   manifest cache, then bootstrap).
4. Inject/strip the library topic block via metadata_gen helpers.
5. POST the patched source back via NextGen (create draft, PATCH draft).
6. Cache AgentScript in manifest and record the attachment.
7. Share library files with the agent runtime user + assign permset.

Simplified standalone version -- no RQ tasks, no LLM topic generation,
no BYOO org routing.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .sf_files import (
    _get_sf_client,
    query_content_versions_by_title_prefix,
    share_content_document_with_user,
    query_agentforce_service_agent_id,
    assign_permission_set_to_user,
    resolve_user_id_by_username,
    fetch_content_version_data,
    upload_content_version,
    delete_content_documents_by_title_prefix,
)
from .metadata_gen import (
    inject_library_topic_afscript,
    strip_library_topic_afscript,
    bootstrap_agentscript,
    ensure_model_config,
    library_topic_exists,
)
from .scorer import generate_topic_metadata, generate_topic_metadata_llm, extract_corpus_from_memory
from . import manifest as manifest_mod

logger = logging.getLogger(__name__)

NEXTGEN_AUTHORING_API_VERSION = "v66.0"
NEXTGEN_TIMEOUT_SECONDS = 120
PERMSET_NAME = "NanoRag_User"

# Strict developerName validator matching Salesforce API naming rules.
_AGENT_DEV_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _failed(
    error: str,
    message: str,
    *,
    library_name: Optional[str] = None,
    agent: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a standardised failure result dict."""
    return {
        "status": "failed",
        "error": error,
        "message": (message or "")[:400],
        "library_name": library_name,
        "agent_developer_name": agent,
        "project_id": project_id,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nextgen_base(sf) -> str:
    """Return the NextGen Authoring URL prefix for this org."""
    return (
        f"https://{sf.sf_instance}/services/data/"
        f"{NEXTGEN_AUTHORING_API_VERSION}/nextgen-authoring/"
    )


def _auth_headers(sf) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {sf.session_id}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _decode_html_entities(text: str) -> str:
    """Decode HTML entities returned by NextGen GET responses."""
    return html.unescape(text)


# ---------------------------------------------------------------------------
# Topic metadata generation
# ---------------------------------------------------------------------------


def _ensure_topic_metadata(
    sf, library_name: str, manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate topic metadata from memory.md if not already in manifest.

    Fetches memory.md from the org, extracts corpus, and generates
    topic_name/description/instructions deterministically. Persists
    the result into the manifest dict so subsequent attaches skip this.

    Returns dict with keys: topic_name, description, instructions.
    """
    # Try fetching memory.md from the org
    memory_content = None
    try:
        memory_title = f"nanorag/{library_name}/memory.md"
        rows = query_content_versions_by_title_prefix(
            sf=sf, prefix=memory_title, limit=1, newest_first=True
        )
        if rows:
            raw_data = fetch_content_version_data(sf, rows[0]["id"])
            if isinstance(raw_data, bytes):
                memory_content = raw_data.decode("utf-8", errors="replace")
            else:
                memory_content = raw_data
    except Exception as exc:
        logger.warning("attach: memory.md fetch failed: %s", str(exc)[:200])

    # Extract corpus from memory.md and generate metadata
    corpus = []
    if memory_content:
        corpus = extract_corpus_from_memory(memory_content)

    topic_name = None
    description = None
    instructions = None
    source = "deterministic"

    # Try LLM-based generation first (higher quality)
    if corpus:
        llm_result = generate_topic_metadata_llm(corpus, sf=sf)
        if llm_result:
            topic_name, description, instructions = llm_result
            source = "llm"
            logger.info("attach: LLM topic generation succeeded")

    # Deterministic fallback
    if not topic_name:
        if corpus:
            topic_name, description, instructions = generate_topic_metadata(corpus)
        else:
            topic_name = library_name
            description = f"Route here for questions about {library_name}"
            instructions = [
                f"Search this library when the user asks about {library_name}.",
                "Only answer from retrieved document content.",
            ]

    # Persist into manifest
    manifest["topic_name"] = topic_name
    manifest["topic_description"] = description
    manifest["topic_instructions"] = instructions
    manifest["topic_source"] = source

    logger.info(
        "attach: topic metadata generated from memory.md",
        extra={
            "library_name": library_name,
            "topic_name": topic_name,
        },
    )

    return {
        "topic_name": topic_name,
        "description": description,
        "instructions": instructions,
    }


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def _load_manifest(
    sf, library_name: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Load manifest from the org.

    Returns (manifest_dict, content_document_id, err_dict).
    On error the first two values are None and err_dict is a failure dict.
    """
    manifest_title = f"nanorag/{library_name}/manifest.json"
    try:
        rows = query_content_versions_by_title_prefix(
            sf=sf, prefix=manifest_title, limit=5, newest_first=True
        )
    except Exception as exc:
        logger.exception("attach: manifest lookup failed")
        return (
            None,
            None,
            _failed(
                "salesforce_error",
                f"Could not list manifest ContentVersions: {exc}",
                library_name=library_name,
            ),
        )

    rows = [r for r in rows if r.get("title") == manifest_title]
    if not rows:
        return (
            None,
            None,
            _failed(
                "library_not_found",
                f"No manifest.json for '{library_name}' in this org.",
                library_name=library_name,
            ),
        )

    cv = rows[0]
    try:
        raw = fetch_content_version_data(sf=sf, content_version_id=cv["id"])
    except Exception as exc:
        logger.exception("attach: manifest fetch failed")
        return (
            None,
            None,
            _failed(
                "salesforce_error",
                f"Could not fetch manifest.json: {exc}",
                library_name=library_name,
            ),
        )

    if raw is None:
        return (
            None,
            None,
            _failed(
                "library_not_found",
                "manifest.json ContentVersion returned no data",
                library_name=library_name,
            ),
        )

    try:
        manifest = manifest_mod.load_manifest(raw.decode("utf-8"))
    except Exception as exc:
        return (
            None,
            None,
            _failed(
                "manifest_corrupt",
                f"manifest.json could not be parsed: {exc}",
                library_name=library_name,
            ),
        )

    return manifest, cv.get("content_document_id"), None


def _upload_manifest(
    sf,
    *,
    library_name: str,
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Serialize manifest and upload as a new ContentVersion.

    Returns a failure dict on error, None on success.
    """
    manifest_yaml = manifest_mod.dump_manifest(manifest)
    manifest_title = f"nanorag/{library_name}/manifest.json"
    try:
        delete_content_documents_by_title_prefix(sf, manifest_title)
        upload_content_version(
            sf=sf,
            title=manifest_title,
            data=manifest_yaml.encode("utf-8"),
            path_on_client="manifest.json",
        )
    except Exception as exc:
        logger.exception("attach: manifest upload failed")
        return _failed(
            "salesforce_error",
            f"Failed to upload new manifest.json: {exc}",
            library_name=library_name,
        )
    return None


# ---------------------------------------------------------------------------
# NextGen Authoring API interactions
# ---------------------------------------------------------------------------


def _find_project_by_api_name(
    sf, agent_developer_name: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """GET /nextgen-authoring/projects, filter by apiName.

    Returns (project_dict, error). On success error is None.
    """
    url = f"{_nextgen_base(sf)}projects"
    try:
        resp = requests.get(
            url, headers=_auth_headers(sf), timeout=NEXTGEN_TIMEOUT_SECONDS
        )
    except Exception as exc:
        return None, f"NextGen GET /projects failed: {exc}"

    if resp.status_code >= 400:
        return None, (
            f"NextGen GET /projects returned {resp.status_code}: "
            f"{resp.text[:400]}"
        )

    try:
        payload = resp.json()
    except ValueError:
        return None, "NextGen GET /projects returned non-JSON body"

    projects = payload.get("projects") or []
    for p in projects:
        if p.get("apiName") == agent_developer_name:
            return p, None

    # Check if it exists as a legacy Bot
    legacy_msg = _check_legacy_bot(sf, agent_developer_name)
    if legacy_msg:
        return None, legacy_msg

    return None, f"No NextGen project found with apiName '{agent_developer_name}'"


def _check_legacy_bot(sf, agent_developer_name: str) -> Optional[str]:
    """Check if the agent exists as a legacy Bot record.

    Returns a helpful error message if found, None otherwise.
    """
    try:
        result = sf.query(
            "SELECT Id, DeveloperName, MasterLabel FROM BotDefinition "
            f"WHERE DeveloperName = '{agent_developer_name}' LIMIT 1"
        )
        if result["totalSize"] > 0:
            label = result["records"][0].get("MasterLabel", agent_developer_name)
            return (
                f"'{label}' is a legacy Bot agent (BotDefinition), not a NextGen "
                f"AgentScript agent. nanoRag requires a NextGen agent. "
                f"To upgrade: Setup > Agents > {label} > Upgrade to AgentScript."
            )
    except Exception:
        pass
    return None


def _extract_afscript_from_body(body: Any) -> Optional[str]:
    """Walk a NextGen response body looking for the AgentScript asset.

    Handles known envelope shapes: {assets: [...]},
    {project: {assets: [...]}}, {bundleVersion: {assets: [...]}}.
    """
    if not isinstance(body, dict):
        return None

    assets = body.get("assets")
    if assets is None and isinstance(body.get("project"), dict):
        assets = body["project"].get("assets")
    if assets is None and isinstance(body.get("bundleVersion"), dict):
        assets = body["bundleVersion"].get("assets")
    if assets is None:
        return None

    for a in assets:
        if not isinstance(a, dict):
            continue
        if (
            a.get("resourceAuthoringFormat") == "afscript"
            or a.get("resourceType") == "agentDefinition"
            or a.get("resourceType") == "AgentScript"
        ):
            content = a.get("resourceContent")
            if isinstance(content, str) and content.strip():
                return _decode_html_entities(content)
    return None


def _fetch_project_agentscript(sf, project: Dict[str, Any]) -> Optional[str]:
    """Best-effort probe of NextGen to fetch the current AgentScript source.

    Tries multiple known URL shapes since the GET contract is not stable.
    Returns None when every probe fails.
    """
    project_id = project.get("id") or project.get("bundleId")
    latest_version = project.get("latestBundleVersionId")
    if not project_id:
        return None

    base = _nextgen_base(sf)
    candidates = [
        (f"{base}projects/{latest_version}" if latest_version else None),
        f"{base}projects/{project_id}",
        f"{base}projects/{project_id}/assets",
    ]
    headers = _auth_headers(sf)

    for url in candidates:
        if not url:
            continue
        try:
            resp = requests.get(url, headers=headers, timeout=NEXTGEN_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug("nextgen probe %s failed: %s", url, exc)
            continue
        if resp.status_code >= 400:
            continue
        try:
            body = resp.json()
        except ValueError:
            continue
        src = _extract_afscript_from_body(body)
        if src:
            return src

    return None


def _post_project_agentscript(
    sf,
    *,
    api_name: str,
    agentscript: str,
    project_id: Optional[str] = None,
    latest_version_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Create a new draft version and write the patched AgentScript.

    Strategy 1: POST /projects/{latestBundleVersionId}/draft then PATCH.
    Strategy 2 (fallback): POST /projects with full payload.

    Returns (success, error_message).
    """
    assets = [
        {
            "resourceName": "definition.agent",
            "resourceType": "agentDefinition",
            "resourceContent": agentscript,
            "resourceAuthoringFormat": "afscript",
        }
    ]
    base = _nextgen_base(sf)
    headers = _auth_headers(sf)
    body = {"apiName": api_name, "label": api_name, "assets": assets}

    # --- Strategy 1: Create new draft + PATCH ---
    draft_source = latest_version_id or project_id
    if draft_source:
        draft_url = f"{base}projects/{draft_source}/draft"
        logger.info(
            "Creating draft version via POST %s for agent %s",
            draft_source,
            api_name,
        )
        try:
            draft_resp = requests.post(
                draft_url, headers=headers, json={}, timeout=NEXTGEN_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning("POST /draft failed: %s", exc)
            draft_resp = None

        if draft_resp is not None and draft_resp.status_code in (200, 201):
            draft_version_id = None
            try:
                draft_version_id = draft_resp.json().get("draftBundleVersionId")
            except (ValueError, AttributeError):
                pass

            if draft_version_id:
                logger.info(
                    "Writing AgentScript to draft %s for agent %s",
                    draft_version_id,
                    api_name,
                )
                try:
                    patch_resp = requests.patch(
                        f"{base}projects/{draft_version_id}",
                        headers=headers,
                        json=body,
                        timeout=NEXTGEN_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.error(
                        "PATCH on draft %s failed: %s", draft_version_id, exc
                    )
                    return False, (
                        f"Could not attach library to agent '{api_name}'. "
                        f"Draft created but write failed."
                    )

                if patch_resp.status_code < 400:
                    logger.info(
                        "Attach succeeded: new draft %s for agent %s",
                        draft_version_id,
                        api_name,
                    )
                    return True, None

                logger.error(
                    "nanorag.attach.draft_patch_failed",
                    extra={
                        "api_name": api_name,
                        "draft_version_id": draft_version_id,
                        "status": patch_resp.status_code,
                        "detail": patch_resp.text[:200],
                    },
                )
                return False, (
                    f"Could not attach library to agent '{api_name}'. "
                    f"Draft created but PATCH returned {patch_resp.status_code}."
                )

            logger.warning(
                "POST /draft returned 201 but no draftBundleVersionId"
            )

        # Draft creation failed -- fall through to POST /projects
        if draft_resp is not None:
            logger.warning(
                "nanorag.attach.draft_creation_failed: status=%s body=%s",
                draft_resp.status_code,
                draft_resp.text[:300],
            )

    # --- Strategy 2: PATCH /projects/{project_id} (update existing) ---
    if project_id:
        logger.info("Trying PATCH /projects/%s for agent %s", project_id, api_name)
        try:
            patch_resp = requests.patch(
                f"{base}projects/{project_id}",
                headers=headers,
                json=body,
                timeout=NEXTGEN_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("PATCH /projects/%s failed: %s", project_id, exc)
            patch_resp = None

        if patch_resp is not None and patch_resp.status_code < 400:
            logger.info("Attach succeeded via PATCH /projects/%s for agent %s", project_id, api_name)
            return True, None

        if patch_resp is not None:
            logger.warning(
                "PATCH /projects/%s returned %s: %s",
                project_id, patch_resp.status_code, patch_resp.text[:300],
            )

    # --- Strategy 3: POST /projects (first-time only) ---
    logger.info("Falling back to POST /projects for agent %s", api_name)
    try:
        resp = requests.post(
            f"{base}projects",
            headers=headers,
            json=body,
            timeout=NEXTGEN_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, f"NextGen POST /projects failed: {exc}"

    if resp.status_code < 400:
        logger.info("Attach succeeded via POST /projects for agent %s", api_name)
        return True, None

    detail = f"POST returned {resp.status_code}: {resp.text[:500]}"
    logger.error(
        "nanorag.attach.post_failed: %s", detail,
    )
    return False, (
        f"Could not attach library to agent '{api_name}'. "
        f"NextGen returned {resp.status_code}. Detail: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Manifest cache helpers
# ---------------------------------------------------------------------------


def _load_cached_agentscript(
    manifest: Dict[str, Any], agent_developer_name: str
) -> Optional[str]:
    """Pull cached AgentScript for the agent from manifest's attached_agentscripts."""
    cache = manifest.get("attached_agentscripts") or {}
    value = cache.get(agent_developer_name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _store_cached_agentscript(
    manifest: Dict[str, Any], agent_developer_name: str, source: str
) -> None:
    """Store AgentScript in manifest's attached_agentscripts cache."""
    cache = manifest.setdefault("attached_agentscripts", {}) or {}
    cache[agent_developer_name] = source
    manifest["attached_agentscripts"] = cache


# ---------------------------------------------------------------------------
# Attachment record helpers
# ---------------------------------------------------------------------------


def _record_attachment(
    manifest: Dict[str, Any],
    *,
    agent_developer_name: str,
    topic_name: str,
) -> None:
    """Record an attachment in the manifest (idempotent by agent name)."""
    attachments: List[Dict[str, Any]] = manifest.setdefault("attachments", []) or []
    attachments = [
        a
        for a in attachments
        if a.get("agent_developer_name") != agent_developer_name
    ]
    attachments.append(
        {
            "agent_developer_name": agent_developer_name,
            "topic_name": topic_name,
            "attached_at": _now_iso(),
        }
    )
    manifest["attachments"] = attachments


def _drop_attachment(
    manifest: Dict[str, Any], agent_developer_name: str
) -> bool:
    """Remove an attachment record from the manifest. Returns True if removed."""
    attachments = manifest.get("attachments") or []
    new_attachments = [
        a
        for a in attachments
        if a.get("agent_developer_name") != agent_developer_name
    ]
    if len(new_attachments) == len(attachments):
        return False
    manifest["attachments"] = new_attachments
    return True


# ---------------------------------------------------------------------------
# Agent user resolution
# ---------------------------------------------------------------------------


def _extract_default_agent_user(agentscript: str) -> Optional[str]:
    """Extract default_agent_user value from AgentScript source."""
    match = re.search(r'default_agent_user:\s*["\']([^"\']+)["\']', agentscript)
    if match:
        return match.group(1)
    match = re.search(r"default_agent_user:\s*(\S+)", agentscript)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# File sharing
# ---------------------------------------------------------------------------


def _share_library_files_with_agent(
    sf,
    *,
    library_name: str,
    agent_developer_name: str,
    patched_src: str,
    manifest: Dict[str, Any],
    manifest_document_id: Optional[str],
) -> bool:
    """Share all library ContentDocuments with the agent runtime user.

    Also assigns the NanoRag_User permission set. Returns True if sharing
    succeeded, False otherwise. Errors are logged but not raised.
    """
    try:
        # Resolve agent runtime user
        agent_username = _extract_default_agent_user(patched_src)
        target_user_id = None
        if agent_username:
            target_user_id = resolve_user_id_by_username(
                sf=sf, username=agent_username
            )
        if not target_user_id:
            # Try to find ASA user by agent developer name pattern
            agent_name_lower = agent_developer_name.lower()
            try:
                asa_result = sf.query_all(
                    "SELECT Id, Username FROM User "
                    "WHERE IsActive = true "
                    "AND Name = 'EinsteinServiceAgent User' "
                    f"AND Username LIKE '%{agent_name_lower}%' "
                    "LIMIT 1"
                )
                if asa_result["totalSize"] > 0:
                    target_user_id = asa_result["records"][0]["Id"]
            except Exception:
                pass
        if not target_user_id:
            target_user_id = query_agentforce_service_agent_id(sf=sf)
        if not target_user_id:
            logger.warning(
                "attach: could not resolve agent runtime user",
                extra={
                    "library_name": library_name,
                    "agent": agent_developer_name,
                },
            )
            return {
                "shared": False,
                "agent_user_id": None,
                "agent_username": agent_username,
                "shared_doc_count": 0,
                "failed_doc_count": 0,
                "failures": [],
                "user_resolution_error": (
                    f"Could not resolve a runtime user for agent "
                    f"'{agent_developer_name}'. AgentScript declared "
                    f"default_agent_user={agent_username!r} but no matching "
                    f"User row was found in the org."
                ),
            }

        # Collect active ContentDocumentIds
        active_doc_ids: set = set()

        # Authoritative source: query the org for current library files.
        # query_content_versions_by_title_prefix filters out soft-deleted
        # ContentDocuments (recycle-bin entries from prior dedupe cycles),
        # so we don't pick up stale IDs that would 422 in the share loop.
        # NOTE: We intentionally do NOT seed from manifest['documents'] or
        # the caller's manifest_document_id — those can hold IDs of CDs
        # that were just deleted-and-replaced as part of this attach call.
        try:
            all_rows = query_content_versions_by_title_prefix(
                sf=sf,
                prefix=f"nanorag/{library_name}/",
                limit=200,
            )
            for r in all_rows:
                if r.get("content_document_id"):
                    active_doc_ids.add(r["content_document_id"])
        except Exception as exc:
            logger.warning(
                "attach: library file lookup failed: %s", str(exc)[:200]
            )

        # Share each document with the agent user.
        # Per-doc try/except so one stale ContentDocumentId (e.g. ENTITY_IS_DELETED)
        # doesn't block sharing the rest of the library. Per-doc failures are
        # collected and surfaced in the response, not silently swallowed.
        share_failures: list[Dict[str, str]] = []
        for doc_id in active_doc_ids:
            try:
                share_content_document_with_user(
                    sf=sf, content_document_id=doc_id, user_id=target_user_id
                )
            except Exception as exc:
                share_failures.append(
                    {"content_document_id": doc_id, "error": str(exc)[:300]}
                )
                logger.warning(
                    "attach: failed to share doc",
                    extra={"doc_id": doc_id, "error": str(exc)[:200]},
                )

        # Assign NanoRag_User permission set
        try:
            assign_permission_set_to_user(
                sf=sf,
                permission_set_name=PERMSET_NAME,
                user_id=target_user_id,
            )
        except Exception as exc:
            logger.warning(
                "attach: permset assignment failed",
                extra={"target_user_id": target_user_id, "error": str(exc)[:200]},
            )
            return {
                "shared": False,
                "agent_user_id": target_user_id,
                "agent_username": agent_username,
                "shared_doc_count": len(active_doc_ids) - len(share_failures),
                "failed_doc_count": len(share_failures),
                "failures": share_failures,
                "permset_error": str(exc)[:300],
            }

        logger.info(
            "attach: files shared with agent user",
            extra={
                "library_name": library_name,
                "agent": agent_developer_name,
                "target_user_id": target_user_id,
                "shared_doc_count": len(active_doc_ids) - len(share_failures),
                "failed_doc_count": len(share_failures),
            },
        )
        return {
            "shared": len(share_failures) == 0,
            "agent_user_id": target_user_id,
            "agent_username": agent_username,
            "shared_doc_count": len(active_doc_ids) - len(share_failures),
            "failed_doc_count": len(share_failures),
            "failures": share_failures,
        }

    except Exception as exc:
        logger.warning(
            "attach: file sharing failed",
            extra={
                "library_name": library_name,
                "agent": agent_developer_name,
                "error": str(exc)[:300],
            },
        )
        return {
            "shared": False,
            "agent_user_id": None,
            "agent_username": None,
            "shared_doc_count": 0,
            "failed_doc_count": 0,
            "failures": [],
            "fatal_error": str(exc)[:300],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attach_library(library_name: str, agent_developer_name: str) -> dict:
    """Attach a nanoRag library to the target agent via NextGen Authoring.

    Idempotent: if the topic block is already present in the source, the
    POST is skipped; if the agent already appears in attachments the
    timestamp is refreshed.

    Args:
        library_name: The nanoRag library name (e.g. "vehicle_manual").
        agent_developer_name: The Salesforce API name of the target agent.

    Returns:
        A result dict with status="attached" on success or
        status="failed" with error details on failure.
    """
    library_name = library_name.strip()
    agent_developer_name = agent_developer_name.strip()

    if not library_name:
        return _failed("missing_input", "library_name is required")
    if not agent_developer_name:
        return _failed("missing_input", "agent_developer_name is required")
    if not _AGENT_DEV_NAME_RE.match(agent_developer_name):
        return _failed(
            "invalid_agent_developer_name",
            f"'{agent_developer_name}' is not a valid Salesforce API name.",
            agent=agent_developer_name,
        )

    # Get Salesforce client from environment
    try:
        sf = _get_sf_client()
    except RuntimeError as exc:
        return _failed(
            "auth_error",
            str(exc),
            library_name=library_name,
            agent=agent_developer_name,
        )

    # Load manifest
    manifest, manifest_document_id, err = _load_manifest(sf, library_name)
    if err is not None:
        return err
    assert manifest is not None and manifest_document_id is not None

    library_description = manifest.get("description") or library_name

    # Find agent project in NextGen
    project, proj_err = _find_project_by_api_name(sf, agent_developer_name)
    if project is None:
        return _failed(
            "agent_not_found",
            proj_err or f"Agent '{agent_developer_name}' not found",
            library_name=library_name,
            agent=agent_developer_name,
        )

    project_id = project.get("id") or project.get("bundleId")
    latest_version_id = project.get("latestBundleVersionId")

    # Fetch current AgentScript source
    current_src = _fetch_project_agentscript(sf, project)
    source_origin = "nextgen"
    if current_src is not None:
        _store_cached_agentscript(manifest, agent_developer_name, current_src)
    if current_src is None:
        current_src = _load_cached_agentscript(manifest, agent_developer_name)
        source_origin = "manifest_cache" if current_src else "bootstrap"
    if current_src is None:
        current_src = bootstrap_agentscript(agent_developer_name)

    # Build topic metadata — try LLM upgrade if only deterministic exists
    topic_metadata: Dict[str, Any] = {}
    if manifest.get("topic_name") and manifest.get("topic_source") == "llm":
        topic_metadata = {
            "topic_name": manifest["topic_name"],
            "description": manifest.get("topic_description") or "",
            "instructions": manifest.get("topic_instructions") or [],
        }
    else:
        # Try LLM generation, falls back to deterministic
        topic_metadata = _ensure_topic_metadata(sf, library_name, manifest)

    # Inject library topic block
    patched_src, source_changed = inject_library_topic_afscript(
        current_src,
        library_name=library_name,
        description=library_description,
        topic_metadata=topic_metadata if topic_metadata else None,
    )

    # Ensure model_config block
    model_config_src = ensure_model_config(patched_src)
    if model_config_src != patched_src:
        patched_src = model_config_src
        source_changed = True

    # POST patched source to NextGen
    if source_changed:
        ok, post_err = _post_project_agentscript(
            sf,
            api_name=agent_developer_name,
            agentscript=patched_src,
            project_id=project_id,
            latest_version_id=latest_version_id,
        )
        if not ok:
            return _failed(
                "nextgen_post_failed",
                post_err or "NextGen POST failed",
                library_name=library_name,
                agent=agent_developer_name,
                project_id=project_id,
            )

    # Update manifest cache and record attachment
    _store_cached_agentscript(manifest, agent_developer_name, patched_src)

    topic_name = topic_metadata.get("topic_name") or library_name
    _record_attachment(
        manifest,
        agent_developer_name=agent_developer_name,
        topic_name=topic_name,
    )

    err = _upload_manifest(sf, library_name=library_name, manifest=manifest)
    if err is not None:
        return err

    # Share library files with agent runtime user
    share_result = _share_library_files_with_agent(
        sf,
        library_name=library_name,
        agent_developer_name=agent_developer_name,
        patched_src=patched_src,
        manifest=manifest,
        manifest_document_id=manifest_document_id,
    )

    manifest_hash = hashlib.sha256(
        manifest_mod.dump_manifest(manifest).encode("utf-8")
    ).hexdigest()

    # If sharing failed, the AgentScript was patched but the agent runtime
    # user can't read the library files — the agent will return empty
    # search results at runtime. Mark the response as a partial success so
    # the user knows action is required, and surface the failure details.
    status = "attached" if share_result["shared"] else "attached_with_share_failure"

    logger.info(
        "attach_library: attached",
        extra={
            "library_name": library_name,
            "agent": agent_developer_name,
            "topic_name": topic_name,
            "source_changed": source_changed,
            "source_origin": source_origin,
            "project_id": project_id,
            "shared": share_result["shared"],
        },
    )

    response: Dict[str, Any] = {
        "status": status,
        "library_name": library_name,
        "agent_developer_name": agent_developer_name,
        "topic_name": topic_name,
        "source_changed": source_changed,
        "source_origin": source_origin,
        "project_id": project_id,
        "manifest_hash": f"sha256:{manifest_hash}",
        "agent_user_shared": share_result["shared"],
        "agent_user_id": share_result["agent_user_id"],
        "agent_username": share_result["agent_username"],
        "shared_doc_count": share_result["shared_doc_count"],
        "failed_doc_count": share_result["failed_doc_count"],
    }
    # Surface failure details when sharing didn't fully succeed
    if not share_result["shared"]:
        response["share_failures"] = share_result["failures"]
        for k in ("fatal_error", "user_resolution_error", "permset_error"):
            if k in share_result:
                response[k] = share_result[k]
        response["warning"] = (
            f"AgentScript was patched, but file sharing did not fully succeed. "
            f"The agent runtime user may not be able to read the library files, "
            f"which will cause the agent to return empty search results. "
            f"Inspect 'share_failures' and re-run attach after fixing."
        )
    return response


def detach_library(library_name: str, agent_developer_name: str) -> dict:
    """Detach a nanoRag library from the target agent.

    Strips the library's topic block from the agent's AgentScript and
    removes the attachment record from the manifest.

    Args:
        library_name: The nanoRag library name.
        agent_developer_name: The Salesforce API name of the target agent.

    Returns:
        A result dict with status="detached" on success or
        status="failed" with error details on failure.
    """
    library_name = library_name.strip()
    agent_developer_name = agent_developer_name.strip()

    if not library_name:
        return _failed("missing_input", "library_name is required")
    if not agent_developer_name:
        return _failed("missing_input", "agent_developer_name is required")
    if not _AGENT_DEV_NAME_RE.match(agent_developer_name):
        return _failed(
            "invalid_agent_developer_name",
            f"'{agent_developer_name}' is not a valid Salesforce API name.",
            agent=agent_developer_name,
        )

    # Get Salesforce client from environment
    try:
        sf = _get_sf_client()
    except RuntimeError as exc:
        return _failed(
            "auth_error",
            str(exc),
            library_name=library_name,
            agent=agent_developer_name,
        )

    # Load manifest
    manifest, manifest_document_id, err = _load_manifest(sf, library_name)
    if err is not None:
        return err
    assert manifest is not None and manifest_document_id is not None

    # Find agent project in NextGen
    project, proj_err = _find_project_by_api_name(sf, agent_developer_name)
    if project is None:
        return _failed(
            "agent_not_found",
            proj_err or f"Agent '{agent_developer_name}' not found",
            library_name=library_name,
            agent=agent_developer_name,
        )

    project_id = project.get("id") or project.get("bundleId")
    latest_version_id = project.get("latestBundleVersionId")

    # Fetch current AgentScript source
    current_src = _fetch_project_agentscript(sf, project)
    source_origin = "nextgen"
    if current_src is None:
        current_src = _load_cached_agentscript(manifest, agent_developer_name)
        source_origin = "manifest_cache" if current_src else "unavailable"

    source_changed = False
    if current_src is not None:
        stripped, source_changed = strip_library_topic_afscript(
            current_src, library_name
        )
        if source_changed:
            ok, post_err = _post_project_agentscript(
                sf,
                api_name=agent_developer_name,
                agentscript=stripped,
                project_id=project_id,
                latest_version_id=latest_version_id,
            )
            if not ok:
                return _failed(
                    "nextgen_post_failed",
                    post_err or "NextGen POST failed",
                    library_name=library_name,
                    agent=agent_developer_name,
                    project_id=project_id,
                )
            _store_cached_agentscript(manifest, agent_developer_name, stripped)

    # Remove attachment from manifest
    manifest_changed = _drop_attachment(manifest, agent_developer_name)

    if manifest_changed or source_changed:
        err = _upload_manifest(sf, library_name=library_name, manifest=manifest)
        if err is not None:
            return err

    logger.info(
        "detach_library: detached",
        extra={
            "library_name": library_name,
            "agent": agent_developer_name,
            "source_changed": source_changed,
            "source_origin": source_origin,
            "project_id": project_id,
        },
    )

    return {
        "status": "detached",
        "library_name": library_name,
        "agent_developer_name": agent_developer_name,
        "source_changed": source_changed,
        "source_origin": source_origin,
        "manifest_changed": manifest_changed,
        "project_id": project_id,
    }
