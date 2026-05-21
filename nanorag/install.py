# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Deploy the nanoRag Apex foundation into a user's org.

Packages 4 Apex classes + NanoRag_User permission set as a Metadata API
zip, deploys to the target org, and assigns the permission set to the
agent runtime user.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from typing import Any, Dict, Optional

from ._foundation import (
    APEX_CLASS_META_XML,
    FOUNDATION_VERSION,
    NANORAG_BM25_SCORER_CLS,
    NANORAG_QUERY_SERVICE_CLS,
    NANORAG_QUERY_SERVICE_TEST_CLS,
    NANORAG_TOKENIZER_CLS,
    NANORAG_USER_PERMSET_XML,
)
from .sf_files import _get_sf_client, query_agentforce_service_agent_id, assign_permission_set_to_user

logger = logging.getLogger("nanorag.install")

APEX_API_VERSION = "63.0"
PERMSET_NAME = "NanoRag_User"
APEX_CLASS_NAMES = [
    "NanoRagTokenizer",
    "NanoRagBM25Scorer",
    "NanoRagQueryService",
    "NanoRagQueryServiceTest",
]
_APEX_CLASS_BODIES = {
    "NanoRagTokenizer": NANORAG_TOKENIZER_CLS,
    "NanoRagBM25Scorer": NANORAG_BM25_SCORER_CLS,
    "NanoRagQueryService": NANORAG_QUERY_SERVICE_CLS,
    "NanoRagQueryServiceTest": NANORAG_QUERY_SERVICE_TEST_CLS,
}

_PACKAGE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
    "    <types>\n"
    "        <members>NanoRagTokenizer</members>\n"
    "        <members>NanoRagBM25Scorer</members>\n"
    "        <members>NanoRagQueryService</members>\n"
    "        <members>NanoRagQueryServiceTest</members>\n"
    "        <name>ApexClass</name>\n"
    "    </types>\n"
    "    <types>\n"
    "        <members>NanoRag_User</members>\n"
    "        <name>PermissionSet</name>\n"
    "    </types>\n"
    f"    <version>{APEX_API_VERSION}</version>\n"
    "</Package>\n"
)

DEPLOY_TIMEOUT_SECONDS = 600
DEPLOY_POLL_INTERVAL = 5


def _build_foundation_zip() -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("package.xml", _PACKAGE_XML)
        for name, body in _APEX_CLASS_BODIES.items():
            zf.writestr(f"classes/{name}.cls", body)
            zf.writestr(f"classes/{name}.cls-meta.xml", APEX_CLASS_META_XML)
        zf.writestr(f"permissionsets/{PERMSET_NAME}.permissionset", NANORAG_USER_PERMSET_XML)
    buf.seek(0)
    return buf


def _deploy_and_poll(sf, zip_buffer: io.BytesIO, timeout: int = DEPLOY_TIMEOUT_SECONDS) -> tuple[bool, Optional[str]]:
    """Deploy a zip via Metadata API and poll until complete."""
    try:
        is_sandbox = sf.query("SELECT IsSandbox FROM Organization LIMIT 1")["records"][0]["IsSandbox"]
        deploy_id, deploy_state = sf.mdapi.deploy(zip_buffer, sandbox=is_sandbox)

        if not deploy_id:
            return False, f"No deployment ID returned (got: {deploy_id}, {deploy_state})"

        logger.info("Deploy started: %s (state: %s)", deploy_id, deploy_state)

        elapsed = 0
        state = deploy_state or "Unknown"

        while elapsed < timeout:
            state, state_detail, deployment_detail, unit_test_detail = sf.mdapi.check_deploy_status(deploy_id)

            if state == "Succeeded":
                return True, None

            if state == "Failed":
                errors = []
                if deployment_detail:
                    for failure in deployment_detail.get("errors", []):
                        errors.append(failure.get("message", "Unknown error"))
                if unit_test_detail:
                    for failure in unit_test_detail.get("errors", []):
                        errors.append(failure.get("message", "Test failure"))
                error_msg = "; ".join(errors) if errors else f"Deployment failed (detail: {state_detail})"
                return False, error_msg

            time.sleep(DEPLOY_POLL_INTERVAL)
            elapsed += DEPLOY_POLL_INTERVAL

        return False, f"Deployment timeout after {timeout}s (state: {state})"

    except Exception as e:
        return False, str(e)


def install_foundation() -> Dict[str, Any]:
    """Deploy the nanoRag Apex foundation + permset into the target org.

    Returns dict with status, deployed classes, and permset info.
    """
    sf = _get_sf_client()

    logger.info("Building foundation zip (SHA: %s)", FOUNDATION_VERSION)
    zip_buf = _build_foundation_zip()

    logger.info("Deploying Apex classes + permission set...")
    success, error = _deploy_and_poll(sf, zip_buf)

    if not success:
        return {
            "status": "failed",
            "error": error,
            "apex_classes": APEX_CLASS_NAMES,
            "permset": PERMSET_NAME,
            "foundation_version": FOUNDATION_VERSION,
        }

    # Assign permset to agent runtime user
    agent_user_id = None
    permset_assigned = False
    try:
        agent_user_id = query_agentforce_service_agent_id(sf)
        if agent_user_id:
            permset_assigned = assign_permission_set_to_user(sf, PERMSET_NAME, agent_user_id)
            logger.info("Permset assigned to agent user %s (new: %s)", agent_user_id, permset_assigned)
        else:
            logger.warning("Agent runtime user not found — permset not assigned")
    except Exception as exc:
        logger.warning("Permset assignment failed: %s", exc)

    return {
        "status": "deployed",
        "deploy_success": True,
        "apex_classes": APEX_CLASS_NAMES,
        "permset": PERMSET_NAME,
        "foundation_version": FOUNDATION_VERSION,
        "agent_user_id": agent_user_id,
        "permset_assigned": permset_assigned,
    }
