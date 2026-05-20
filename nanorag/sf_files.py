# Copyright (c) 2024, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Salesforce REST/SOQL helpers for standalone nanoRag operations.

Uses simple_salesforce with auth from SF_ACCESS_TOKEN + SF_INSTANCE_URL
environment variables (passed by the SF CLI plugin).
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from simple_salesforce import Salesforce

logger = logging.getLogger("nanorag.sf_files")

SF_API_VERSION = "63.0"


def _get_sf_client() -> Salesforce:
    """Create a Salesforce client from environment variables."""
    access_token = os.environ.get("SF_ACCESS_TOKEN", "")
    instance_url = os.environ.get("SF_INSTANCE_URL", "")
    if not access_token:
        raise RuntimeError("SF_ACCESS_TOKEN environment variable not set")
    if not instance_url:
        raise RuntimeError("SF_INSTANCE_URL environment variable not set")
    return Salesforce(
        instance_url=instance_url,
        session_id=access_token,
        version=SF_API_VERSION,
    )


def _soql_escape(value: str) -> str:
    """Escape a string for safe inclusion in a SOQL string literal.

    Backslashes first (to avoid double-escaping), then single quotes.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def query_content_versions_by_title_prefix(
    sf: Salesforce,
    prefix: str,
    limit: int = 200,
    newest_first: bool = False,
) -> List[Dict[str, Any]]:
    """Return latest ContentVersion rows whose Title starts with *prefix*.

    Filters to IsLatest = true to skip historical versions. Returns a list
    of dicts with keys: id, title, content_document_id, content_size,
    last_modified.

    When newest_first=True, orders by CreatedDate DESC (useful when
    duplicate titles exist and you want the most recent upload).
    """
    safe_prefix = _soql_escape(prefix)
    order = "ORDER BY CreatedDate DESC" if newest_first else "ORDER BY Title ASC"
    soql = (
        "SELECT Id, Title, ContentDocumentId, ContentSize, LastModifiedDate "
        "FROM ContentVersion "
        f"WHERE Title LIKE '{safe_prefix}%' AND IsLatest = true "
        f"{order} "
        f"LIMIT {int(limit)}"
    )
    result = sf.query_all(soql)
    return [
        {
            "id": r["Id"],
            "title": r["Title"],
            "content_document_id": r["ContentDocumentId"],
            "content_size": r["ContentSize"],
            "last_modified": r["LastModifiedDate"],
        }
        for r in result["records"]
    ]


def upload_content_version(
    sf: Salesforce,
    title: str,
    data: bytes,
    path_on_client: Optional[str] = None,
) -> str:
    """Upload bytes as a new ContentVersion.

    Returns the ContentVersionId of the newly created record.
    Uses the sobjects/ContentVersion REST endpoint via simple_salesforce,
    encoding the binary payload as base64 inline VersionData.
    """
    payload: Dict[str, Any] = {
        "Title": title,
        "PathOnClient": path_on_client or title.split("/")[-1],
        "VersionData": base64.b64encode(data).decode("ascii"),
        "IsMajorVersion": True,
    }
    result = sf.ContentVersion.create(payload)
    return result["id"]


def delete_content_documents_by_title_prefix(
    sf: Salesforce,
    prefix: str,
) -> int:
    """Delete every ContentDocument whose latest CV Title starts with *prefix*.

    Deleting the ContentDocument cascades to all ContentVersions and
    ContentDocumentLinks. Returns the count of documents deleted.
    Idempotent: returns 0 if nothing matches.
    """
    safe_prefix = _soql_escape(prefix)
    soql = (
        "SELECT ContentDocumentId FROM ContentVersion "
        f"WHERE Title LIKE '{safe_prefix}%' AND IsLatest = true"
    )
    result = sf.query_all(soql)
    doc_ids = {
        r["ContentDocumentId"]
        for r in result["records"]
        if r.get("ContentDocumentId")
    }
    for doc_id in doc_ids:
        sf.ContentDocument.delete(doc_id)
    return len(doc_ids)


def share_content_document_with_user(
    sf: Salesforce,
    content_document_id: str,
    user_id: str,
) -> bool:
    """Create a ContentDocumentLink sharing a document with a user.

    Uses ShareType='C' (Collaborator) and Visibility='AllUsers'.
    Idempotent: checks for an existing link before creating.
    Returns True if a new link was created, False if already shared.
    """
    safe_uid = _soql_escape(user_id)
    safe_cdid = _soql_escape(content_document_id)
    existing = sf.query_all(
        "SELECT Id FROM ContentDocumentLink "
        f"WHERE LinkedEntityId = '{safe_uid}' "
        f"AND ContentDocumentId = '{safe_cdid}' "
        "LIMIT 1"
    )
    if existing["totalSize"] > 0:
        return False

    sf.ContentDocumentLink.create(
        {
            "ContentDocumentId": content_document_id,
            "LinkedEntityId": user_id,
            "ShareType": "C",
            "Visibility": "AllUsers",
        }
    )
    return True


def query_agentforce_service_agent_id(
    sf: Salesforce,
) -> Optional[str]:
    """Find the Agentforce Service Agent runtime user ID in the org.

    Primary lookup: PermissionSetAssignment where the assignee name
    contains 'Agentforce' + 'Service' and holds an Agentforce permission set.

    Fallback: automated-process users with 'Einstein' in the name.

    Returns the User.Id (005-prefix) or None if not found.
    """
    try:
        result = sf.query_all(
            "SELECT AssigneeId FROM PermissionSetAssignment "
            "WHERE Assignee.IsActive = true "
            "AND Assignee.Name LIKE '%Agentforce%' "
            "AND Assignee.Name LIKE '%Service%' "
            "AND (PermissionSet.Name LIKE '%Agentforce%' "
            "OR PermissionSet.Label LIKE '%Agentforce%') "
            "LIMIT 1"
        )
        if result["totalSize"] > 0:
            return result["records"][0]["AssigneeId"]

        einstein_result = sf.query_all(
            "SELECT Id FROM User "
            "WHERE IsActive = true "
            "AND Name = 'EinsteinServiceAgent User' "
            "LIMIT 1"
        )
        if einstein_result["totalSize"] > 0:
            return einstein_result["records"][0]["Id"]

        auto_result = sf.query_all(
            "SELECT Id FROM User "
            "WHERE IsActive = true "
            "AND (Name LIKE '%Einstein%' OR Username LIKE '%einstein%') "
            "AND UserType = 'AutomatedProcess' "
            "LIMIT 1"
        )
        if auto_result["totalSize"] > 0:
            return auto_result["records"][0]["Id"]
    except Exception as exc:
        logger.warning(
            "Agent user lookup failed: %s",
            str(exc)[:200],
        )
    return None


def assign_permission_set_to_user(
    sf: Salesforce,
    permission_set_name: str,
    user_id: str,
) -> bool:
    """Assign a permission set to a user.

    Checks for an existing assignment first to avoid duplicate DML errors.
    Returns True if newly assigned, False if already assigned or if the
    permission set was not found.
    """
    safe_name = _soql_escape(permission_set_name)
    safe_uid = _soql_escape(user_id)
    existing = sf.query_all(
        "SELECT Id FROM PermissionSetAssignment "
        f"WHERE AssigneeId = '{safe_uid}' "
        f"AND PermissionSet.Name = '{safe_name}' "
        "LIMIT 1"
    )
    if existing["totalSize"] > 0:
        return False

    permset_result = sf.query_all(
        f"SELECT Id FROM PermissionSet WHERE Name = '{safe_name}' LIMIT 1"
    )
    if not permset_result["records"]:
        return False

    permset_id = permset_result["records"][0]["Id"]
    sf.PermissionSetAssignment.create(
        {"AssigneeId": user_id, "PermissionSetId": permset_id}
    )
    return True


def resolve_user_id_by_username(
    sf: Salesforce,
    username: str,
) -> Optional[str]:
    """Look up a Salesforce User.Id by Username. Returns None if not found."""
    safe_username = _soql_escape(username)
    result = sf.query_all(
        "SELECT Id FROM User "
        f"WHERE Username = '{safe_username}' AND IsActive = true "
        "LIMIT 1"
    )
    if result["totalSize"] > 0:
        return result["records"][0]["Id"]
    return None


def fetch_content_version_data(
    sf: Salesforce,
    content_version_id: str,
) -> Optional[bytes]:
    """Fetch raw VersionData bytes for a ContentVersion.

    Uses the /sobjects/ContentVersion/{id}/VersionData REST endpoint
    which streams the raw blob. Returns None on 404 (deleted/missing CV).
    """
    url = f"{sf.base_url}sobjects/ContentVersion/{content_version_id}/VersionData"
    resp = requests.get(url, headers=sf.headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content
