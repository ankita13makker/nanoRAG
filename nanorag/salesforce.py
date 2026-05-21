# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Salesforce Files client — upload and download via ContentVersion REST API.

Upload: raw knowledge files + generated nanorag artifacts (index.md, memory.md, bm25.json)
Download: read file content by title at runtime

All files are linked to a parent record via ContentDocumentLink.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from pathlib import Path

try:
    import certifi
    _CA_BUNDLE = certifi.where()
except ImportError:
    _CA_BUNDLE = True  # requests default: use system CA bundle

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("nanorag.salesforce")

API_VERSION = "v62.0"


def _check_config(instance_url: str, access_token: str) -> None:
    if not HAS_REQUESTS:
        raise RuntimeError("requests library required: pip install requests")
    if not instance_url:
        raise RuntimeError("SFDC_INSTANCE_URL not set")
    if not access_token:
        raise RuntimeError(
            "SFDC_ACCESS_TOKEN not set — paste a session token or set SFDC_ACCESS_TOKEN env var"
        )


def _make_session(access_token: str) -> "requests.Session":
    """Create a session with TLS verification and retry logic."""
    session = requests.Session()
    session.verify = _CA_BUNDLE
    session.headers.update({"Authorization": f"Bearer {access_token}"})
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _base_url(instance_url: str) -> str:
    return f"{instance_url.rstrip('/')}/services/data/{API_VERSION}"


def _escape_soql(value: str) -> str:
    """Escape a string for SOQL — handles quotes and LIKE wildcards."""
    return (
        value
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _get_credentials() -> tuple[str, str, str]:
    """Read credentials from environment. Returns (instance_url, access_token, parent_id).

    Checks SF_* env vars first (set by the SF CLI plugin), falls back to
    SFDC_* for backwards compatibility with direct usage.
    """
    instance_url = os.environ.get("SF_INSTANCE_URL") or os.environ.get("SFDC_INSTANCE_URL", "")
    access_token = os.environ.get("SF_ACCESS_TOKEN") or os.environ.get("SFDC_ACCESS_TOKEN", "")
    parent_id = os.environ.get("SFDC_PARENT_ID", "")
    return instance_url, access_token, parent_id


def set_session_token(token: str) -> None:
    """Set the access token at runtime (e.g. from a request header)."""
    os.environ["SFDC_ACCESS_TOKEN"] = token


def set_instance_url(url: str) -> None:
    """Set the Salesforce instance URL at runtime (e.g. from a request header)."""
    os.environ["SFDC_INSTANCE_URL"] = url.rstrip("/")


# ---------------------------------------------------------------------------
# Core upload/link helper — single implementation, no duplication
# ---------------------------------------------------------------------------


def _upload_and_link(
    session: "requests.Session",
    base: str,
    title: str,
    filename: str,
    data: bytes,
    mime_type: str,
    parent_id: str,
) -> str:
    """Upload ContentVersion, retrieve ContentDocumentId, optionally link.

    Returns ContentDocumentId.
    """
    resp = session.post(
        f"{base}/sobjects/ContentVersion",
        files={
            "entity_content": (
                None,
                json.dumps({"Title": title, "PathOnClient": filename}),
                "application/json",
            ),
            "VersionData": (filename, data, mime_type),
        },
        timeout=300,
    )
    resp.raise_for_status()
    cv_id = resp.json()["id"]

    doc_resp = session.get(
        f"{base}/sobjects/ContentVersion/{cv_id}?fields=ContentDocumentId",
        timeout=30,
    )
    doc_resp.raise_for_status()
    doc_id = doc_resp.json()["ContentDocumentId"]

    if parent_id:
        link_resp = session.post(
            f"{base}/sobjects/ContentDocumentLink",
            json={
                "ContentDocumentId": doc_id,
                "LinkedEntityId": parent_id,
                "ShareType": "V",
                "Visibility": "AllUsers",
            },
            timeout=30,
        )
        if not (link_resp.status_code == 400 and "DUPLICATE_VALUE" in link_resp.text):
            link_resp.raise_for_status()

    return doc_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def upload_file(title: str, file_path: Path, mime_type: str | None = None) -> str:
    """Upload a file to Salesforce Files. Returns ContentDocumentId."""
    instance_url, access_token, parent_id = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)

    if mime_type is None:
        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

    data = file_path.read_bytes()
    return _upload_and_link(
        session, _base_url(instance_url), title, file_path.name, data, mime_type, parent_id
    )


def upload_content(title: str, content: str, extension: str = "md") -> str:
    """Upload text content as a file to Salesforce Files. Returns ContentDocumentId."""
    instance_url, access_token, parent_id = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)

    filename = f"{title.split('/')[-1]}.{extension}" if "/" in title else f"{title}.{extension}"
    mime_type = "application/json" if extension == "json" else "text/plain"

    return _upload_and_link(
        session, _base_url(instance_url), title, filename,
        content.encode("utf-8"), mime_type, parent_id
    )


def download_file(title: str) -> str | None:
    """Download file content by title. Returns text content or None."""
    instance_url, access_token, _ = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)
    base = _base_url(instance_url)

    query = (
        f"SELECT Id FROM ContentVersion "
        f"WHERE Title = '{_escape_soql(title)}' AND IsLatest = true "
        f"LIMIT 1"
    )
    resp = session.get(f"{base}/query", params={"q": query}, timeout=30)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        return None

    cv_id = records[0]["Id"]
    blob_resp = session.get(
        f"{base}/sobjects/ContentVersion/{cv_id}/VersionData", timeout=60
    )
    blob_resp.raise_for_status()
    return blob_resp.content.decode("utf-8", errors="replace")


def download_binary(title: str) -> bytes | None:
    """Download binary file content by title. Returns bytes or None."""
    instance_url, access_token, _ = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)
    base = _base_url(instance_url)

    query = (
        f"SELECT Id FROM ContentVersion "
        f"WHERE Title = '{_escape_soql(title)}' AND IsLatest = true "
        f"LIMIT 1"
    )
    resp = session.get(f"{base}/query", params={"q": query}, timeout=30)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        return None

    cv_id = records[0]["Id"]
    blob_resp = session.get(
        f"{base}/sobjects/ContentVersion/{cv_id}/VersionData", timeout=60
    )
    blob_resp.raise_for_status()
    return blob_resp.content


def list_files(prefix: str = "") -> list[dict]:
    """List files in Salesforce. Returns list of {title, id, size}."""
    instance_url, access_token, _ = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)
    base = _base_url(instance_url)

    where = "IsLatest = true"
    if prefix:
        where += f" AND Title LIKE '{_escape_soql(prefix)}%'"

    query = f"SELECT Id, Title, ContentSize FROM ContentVersion WHERE {where} ORDER BY Title"
    resp = session.get(f"{base}/query", params={"q": query}, timeout=30)
    resp.raise_for_status()
    return [
        {"title": r["Title"], "id": r["Id"], "size": r.get("ContentSize", 0)}
        for r in resp.json().get("records", [])
    ]


def delete_files(prefix: str = "") -> int:
    """Delete files from Salesforce Files by title prefix. Returns count deleted."""
    instance_url, access_token, _ = _get_credentials()
    _check_config(instance_url, access_token)
    session = _make_session(access_token)
    base = _base_url(instance_url)

    where = "IsLatest = true"
    if prefix:
        where += f" AND Title LIKE '{_escape_soql(prefix)}%'"

    query = f"SELECT ContentDocumentId FROM ContentVersion WHERE {where}"
    resp = session.get(f"{base}/query", params={"q": query}, timeout=30)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        return 0

    doc_ids = list({r["ContentDocumentId"] for r in records})
    deleted = 0
    for doc_id in doc_ids:
        del_resp = session.delete(
            f"{base}/sobjects/ContentDocument/{doc_id}", timeout=30
        )
        if del_resp.status_code == 204:
            deleted += 1
        elif del_resp.status_code in (400, 404):
            pass
        else:
            del_resp.raise_for_status()
    return deleted


def _delete_existing_by_title(title: str) -> None:
    """Delete any existing ContentDocuments with this exact title.

    Prevents stale duplicates when re-uploading artifacts. Silently
    ignores failures (non-critical for the upload flow).
    """
    try:
        instance_url, access_token, _ = _get_credentials()
        session = _make_session(access_token)
        base = _base_url(instance_url)

        query = (
            f"SELECT ContentDocumentId FROM ContentVersion "
            f"WHERE Title = '{_escape_soql(title)}' AND IsLatest = true"
        )
        resp = session.get(f"{base}/query", params={"q": query}, timeout=30)
        if resp.status_code != 200:
            return
        records = resp.json().get("records", [])
        for r in records:
            session.delete(
                f"{base}/sobjects/ContentDocument/{r['ContentDocumentId']}",
                timeout=30,
            )
    except Exception:
        pass


def upload_nanorag(output_dir: Path, knowledge_dir: Path, label: str) -> dict:
    """Upload all nanorag artifacts and raw knowledge files.

    Naming convention:
      Artifacts:       "{SF_PATH_PREFIX}/{label}/index.md", etc.
      Knowledge files: "{SF_PATH_PREFIX}/{label}/raw/{filename}"
      Extracted text:  "{SF_PATH_PREFIX}/{label}/extracted/{filename}.txt"

    Deletes existing copies before uploading to prevent stale duplicates.
    Returns dict with upload counts.
    """
    from .constants import SF_PATH_PREFIX, ARTIFACTS
    from .extractors import extract_text, is_supported

    instance_url, access_token, _ = _get_credentials()
    _check_config(instance_url, access_token)

    prefix = f"{SF_PATH_PREFIX}/{label}"
    uploaded = {"artifacts": 0, "raw_files": 0, "extracted": 0}

    for artifact in ARTIFACTS:
        artifact_path = output_dir / artifact
        if artifact_path.exists():
            # Apex runtime expects the BM25 index under /index/ subpath.
            if artifact == "bm25.json":
                title = f"{prefix}/index/{artifact}"
            else:
                title = f"{prefix}/{artifact}"
            _delete_existing_by_title(title)
            ext = artifact_path.suffix.lstrip(".")
            content = artifact_path.read_text(encoding="utf-8")
            upload_content(title, content, extension=ext)
            uploaded["artifacts"] += 1
            print(f"    Uploaded {title}")

    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.rglob("*")):
            if not f.is_file() or not is_supported(f):
                continue

            raw_title = f"{prefix}/raw/{f.name}"
            _delete_existing_by_title(raw_title)
            upload_file(raw_title, f)
            uploaded["raw_files"] += 1
            print(f"    Uploaded {raw_title}")

            if f.suffix.lower() in {".pdf", ".docx", ".pptx", ".xlsx", ".rtf", ".epub", ".odt"}:
                text = extract_text(f)
                if text.strip():
                    ext_title = f"{prefix}/extracted/{f.name}.txt"
                    _delete_existing_by_title(ext_title)
                    upload_content(ext_title, text, extension="txt")
                    uploaded["extracted"] += 1
                    print(f"    Uploaded {ext_title}")

    return uploaded
