# Copyright (c) 2024, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Read/write the manifest.yaml that describes a nanoRag library.

The manifest is the one-source-of-truth per library. It lives as a
ContentVersion in Salesforce at Title `nanorag/<library>/manifest.yaml`,
versioned via the native ContentVersion chain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_manifest(
    *,
    name: str,
    display_name: str,
    description: str,
    user_email: str,
) -> Dict[str, Any]:
    now = _now()
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "created_at": now,
        "created_by": user_email,
        "active_version": 0,
        "versions": [],
        "documents": [],
        "attachments": [],
        "activity_log": [
            {
                "ts": now,
                "actor": user_email,
                "event": "library.create",
                "data": {"name": name},
            }
        ],
    }


def bump_version(
    manifest: Dict[str, Any],
    document_count: int,
    bm25_content_version_id: str,
    manifest_hash: str,
    built_by: str,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    now = _now()
    next_n = (manifest["versions"][-1]["n"] + 1) if manifest["versions"] else 1
    version: Dict[str, Any] = {
        "n": next_n,
        "built_at": now,
        "built_by": built_by,
        "document_count": document_count,
        "bm25_content_version_id": bm25_content_version_id,
        "manifest_hash": manifest_hash,
    }
    if comment:
        version["comment"] = comment
    manifest["versions"].append(version)
    manifest["active_version"] = next_n
    manifest["activity_log"].append(
        {
            "ts": now,
            "actor": built_by,
            "event": "version.build",
            "data": {"version": next_n, "doc_count": document_count},
        }
    )
    return manifest


def set_active_version(manifest: Dict[str, Any], version_n: int) -> Dict[str, Any]:
    valid = [v["n"] for v in manifest["versions"]]
    if version_n not in valid:
        raise ValueError(f"version {version_n} not in {valid}")
    manifest["active_version"] = version_n
    now = _now()
    manifest["activity_log"].append(
        {
            "ts": now,
            "actor": "system",
            "event": "version.rollback",
            "data": {"to": version_n},
        }
    )
    return manifest


def load_manifest(yaml_str: str) -> Dict[str, Any]:
    return yaml.safe_load(yaml_str)


def dump_manifest(manifest: Dict[str, Any]) -> str:
    return yaml.safe_dump(manifest, sort_keys=False)
