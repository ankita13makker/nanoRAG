# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""JSON subprocess bridge for the SF CLI plugin.

Protocol:
  - Input (stdin):  {"command": "build", "args": {"library_name": "...", ...}}
  - Output (stdout): {"status": "ok", "result": {...}}
  - Errors (stdout): {"status": "error", "error": "error_code", "message": "..."}
  - Progress (stderr): human-readable log lines

Entry point: python -m nanorag.cli_runner
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("nanorag")


def _setup_logging():
    """Configure logging to stderr (stdout is reserved for JSON responses)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _success(result: Any) -> Dict:
    return {"status": "ok", "result": result}


def _error(error_code: str, message: str) -> Dict:
    return {"status": "error", "error": error_code, "message": message}


def _handle_build(args: Dict) -> Dict:
    """Build a library from local files and upload to org."""
    from .build import build_library, sync_library
    from .salesforce import upload_nanorag
    from .constants import OUTPUT_DIR

    library_name = args.get("library_name")
    files = args.get("files", [])
    knowledge_dir = args.get("knowledge_dir")

    if not library_name:
        return _error("missing_arg", "library_name is required")

    # If files are provided, copy them to a temp knowledge directory structure
    if files:
        import shutil
        import tempfile
        temp_dir = Path(tempfile.mkdtemp())
        lib_dir = temp_dir / library_name
        lib_dir.mkdir(parents=True)
        for f in files:
            src = Path(f)
            if not src.exists():
                return _error("file_not_found", f"File not found: {f}")
            shutil.copy2(src, lib_dir / src.name)
        knowledge_dir = str(temp_dir)

    if not knowledge_dir:
        return _error("missing_arg", "Either files or knowledge_dir is required")

    output_dir = OUTPUT_DIR
    count = build_library(library_name, knowledge_dir, output_dir, upload_sf=True)

    return _success({
        "library_name": library_name,
        "files_indexed": count,
    })


def _handle_install(args: Dict) -> Dict:
    """Deploy Apex foundation to the target org."""
    from .install import install_foundation
    return _success(install_foundation())


def _handle_attach(args: Dict) -> Dict:
    """Attach a library to an agent."""
    from .attach import attach_library

    library_name = args.get("library_name")
    agent_developer_name = args.get("agent_developer_name")
    if not library_name:
        return _error("missing_arg", "library_name is required")
    if not agent_developer_name:
        return _error("missing_arg", "agent_developer_name is required")

    return _success(attach_library(library_name, agent_developer_name))


def _handle_detach(args: Dict) -> Dict:
    """Detach a library from an agent."""
    from .attach import detach_library

    library_name = args.get("library_name")
    agent_developer_name = args.get("agent_developer_name")
    if not library_name:
        return _error("missing_arg", "library_name is required")
    if not agent_developer_name:
        return _error("missing_arg", "agent_developer_name is required")

    return _success(detach_library(library_name, agent_developer_name))


def _handle_library_list(args: Dict) -> Dict:
    """List all libraries in the org."""
    from .sf_files import _get_sf_client, query_content_versions_by_title_prefix

    sf = _get_sf_client()
    cvs = query_content_versions_by_title_prefix(sf, "nanorag/")

    # Extract unique library names from titles like "nanorag/{lib}/manifest.yaml"
    libraries = {}
    for cv in cvs:
        title = cv["title"]
        parts = title.split("/")
        if len(parts) >= 3 and parts[0] == "nanorag":
            lib_name = parts[1]
            if lib_name not in libraries:
                libraries[lib_name] = {"name": lib_name, "file_count": 0, "has_index": False}
            if "/doc/" in title or "/raw/" in title:
                libraries[lib_name]["file_count"] += 1
            if title.endswith("bm25.json"):
                libraries[lib_name]["has_index"] = True

    return _success({"libraries": list(libraries.values())})


def _handle_library_delete(args: Dict) -> Dict:
    """Delete a library and all its files from the org.

    Auto-detaches from any agents the library is attached to before deleting.
    """
    from .sf_files import _get_sf_client, delete_content_documents_by_title_prefix, query_content_versions_by_title_prefix, fetch_content_version_data
    from .attach import detach_library
    from . import manifest as manifest_mod

    library_name = args.get("library_name")
    if not library_name:
        return _error("missing_arg", "library_name is required")

    sf = _get_sf_client()

    # Load manifest to find attached agents
    detached_agents = []
    try:
        manifest_rows = query_content_versions_by_title_prefix(
            sf=sf, prefix=f"nanorag/{library_name}/manifest.json", limit=1, newest_first=True
        )
        if manifest_rows:
            raw = fetch_content_version_data(sf, manifest_rows[0]["id"])
            if raw:
                manifest = manifest_mod.load_manifest(raw.decode("utf-8"))
                for att in manifest.get("attachments") or []:
                    agent_name = att.get("agent_developer_name")
                    if agent_name:
                        logger.info("library_delete: detaching from %s", agent_name)
                        detach_library(library_name, agent_name)
                        detached_agents.append(agent_name)
    except Exception as exc:
        logger.warning("library_delete: could not auto-detach: %s", str(exc)[:200])

    prefix = f"nanorag/{library_name}/"
    count = delete_content_documents_by_title_prefix(sf, prefix)

    return _success({
        "library_name": library_name,
        "files_deleted": count,
        "detached_agents": detached_agents,
    })


def _handle_file_list(args: Dict) -> Dict:
    """List files in a library."""
    from .sf_files import _get_sf_client, query_content_versions_by_title_prefix

    library_name = args.get("library_name")
    if not library_name:
        return _error("missing_arg", "library_name is required")

    sf = _get_sf_client()
    prefix = f"nanorag/{library_name}/raw/"
    cvs = query_content_versions_by_title_prefix(sf, prefix)

    files = []
    for cv in cvs:
        filename = cv["title"].replace(prefix, "")
        files.append({
            "filename": filename,
            "size_bytes": cv["content_size"],
            "content_version_id": cv["id"],
        })

    return _success({"library_name": library_name, "files": files})


def _handle_file_add(args: Dict) -> Dict:
    """Add files to an existing library.

    Downloads existing extracted text from the org, merges with new files,
    rebuilds BM25 index and memory.md, re-uploads artifacts, and updates
    topic metadata on any attached agents.
    """
    from .sf_files import (
        _get_sf_client, query_content_versions_by_title_prefix,
        fetch_content_version_data, upload_content_version,
        delete_content_documents_by_title_prefix,
    )
    from .scorer import build_index, build_memory_md, enrich_text, generate_topic_metadata, generate_topic_metadata_llm
    from .extractors import extract_text
    from . import manifest as manifest_mod

    library_name = args.get("library_name")
    files = args.get("files", [])
    if not library_name:
        return _error("missing_arg", "library_name is required")
    if not files:
        return _error("missing_arg", "files is required")

    sf = _get_sf_client()

    # Gather existing content from org (extracted/ for binary files, raw/ for text)
    corpus: list[tuple[str, str]] = []
    existing_files: set = set()

    # First check extracted/ (binary formats: PDF, DOCX, etc.)
    extracted_prefix = f"nanorag/{library_name}/extracted/"
    extracted_cvs = query_content_versions_by_title_prefix(sf=sf, prefix=extracted_prefix, limit=200)
    for cv in extracted_cvs:
        raw_bytes = fetch_content_version_data(sf, cv["id"])
        if raw_bytes:
            filename = cv["title"].replace(extracted_prefix, "").removesuffix(".txt")
            corpus.append((filename, raw_bytes.decode("utf-8", errors="replace")))
            existing_files.add(filename)

    # Then check raw/ for text files not in extracted/
    raw_prefix = f"nanorag/{library_name}/raw/"
    raw_cvs = query_content_versions_by_title_prefix(sf=sf, prefix=raw_prefix, limit=200)
    text_extensions = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".py", ".sql", ".cls"}
    for cv in raw_cvs:
        filename = cv["title"].replace(raw_prefix, "")
        if filename in existing_files:
            continue
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in text_extensions:
            raw_bytes = fetch_content_version_data(sf, cv["id"])
            if raw_bytes:
                corpus.append((filename, raw_bytes.decode("utf-8", errors="replace")))
                existing_files.add(filename)

    # Extract and upload new files (skip if already in library)
    new_files_added = []
    for f in files:
        src = Path(f)
        if not src.exists():
            return _error("file_not_found", f"File not found: {f}")
        if src.name in existing_files:
            logger.info("file_add: skipping %s (already in library)", src.name)
            continue
        text = extract_text(src)
        if not text:
            continue
        corpus.append((src.name, text))
        new_files_added.append(src.name)

        # Upload raw file
        upload_content_version(sf=sf, title=f"nanorag/{library_name}/raw/{src.name}",
                              data=src.read_bytes(), path_on_client=src.name)
        # Upload extracted text
        upload_content_version(sf=sf, title=f"nanorag/{library_name}/extracted/{src.name}.txt",
                              data=text.encode("utf-8"), path_on_client=f"{src.name}.txt")

    if not corpus:
        return _error("no_content", "No extractable content found")

    # Rebuild BM25 index from full corpus
    enriched_corpus = [(src, enrich_text(text, title=src)) for src, text in corpus]
    index = build_index(enriched_corpus)
    memory = build_memory_md(corpus)

    import json as _json
    # Delete old index/memory and re-upload
    delete_content_documents_by_title_prefix(sf, f"nanorag/{library_name}/index/bm25.json")
    delete_content_documents_by_title_prefix(sf, f"nanorag/{library_name}/memory.md")
    upload_content_version(sf=sf, title=f"nanorag/{library_name}/index/bm25.json",
                          data=_json.dumps(index).encode("utf-8"), path_on_client="bm25.json")
    upload_content_version(sf=sf, title=f"nanorag/{library_name}/memory.md",
                          data=memory.encode("utf-8"), path_on_client="memory.md")

    # Update topic metadata and re-attach to agents
    _rebuild_topic_and_update_agents(sf, library_name, corpus)

    return _success({
        "library_name": library_name,
        "files_added": new_files_added,
        "total_files": len(corpus),
        "files_indexed": len(corpus),
    })


def _handle_file_delete(args: Dict) -> Dict:
    """Delete specific files from a library.

    After deletion, rebuilds BM25 index from remaining files and updates
    topic metadata on any attached agents.
    """
    from .sf_files import (
        _get_sf_client, query_content_versions_by_title_prefix,
        fetch_content_version_data, upload_content_version,
        delete_content_documents_by_title_prefix,
    )
    from .scorer import build_index, build_memory_md, enrich_text

    library_name = args.get("library_name")
    filename = args.get("filename")
    delete_all = args.get("all", False)

    if not library_name:
        return _error("missing_arg", "library_name is required")
    if not filename and not delete_all:
        return _error("missing_arg", "Either filename or all=true is required")

    sf = _get_sf_client()

    # Delete the file(s)
    if delete_all:
        prefix = f"nanorag/{library_name}/raw/"
        count = delete_content_documents_by_title_prefix(sf, prefix)
        ext_prefix = f"nanorag/{library_name}/extracted/"
        count += delete_content_documents_by_title_prefix(sf, ext_prefix)
    else:
        raw_prefix = f"nanorag/{library_name}/raw/{filename}"
        ext_prefix = f"nanorag/{library_name}/extracted/{filename}"
        count = delete_content_documents_by_title_prefix(sf, raw_prefix)
        count += delete_content_documents_by_title_prefix(sf, ext_prefix)

    # Rebuild index from remaining files (extracted/ for binary, raw/ for text)
    corpus: list[tuple[str, str]] = []
    seen_files: set = set()

    extracted_prefix = f"nanorag/{library_name}/extracted/"
    remaining_extracted = query_content_versions_by_title_prefix(sf=sf, prefix=extracted_prefix, limit=200)
    for cv in remaining_extracted:
        raw_bytes = fetch_content_version_data(sf, cv["id"])
        if raw_bytes:
            fname = cv["title"].replace(extracted_prefix, "").removesuffix(".txt")
            corpus.append((fname, raw_bytes.decode("utf-8", errors="replace")))
            seen_files.add(fname)

    raw_prefix = f"nanorag/{library_name}/raw/"
    remaining_raw = query_content_versions_by_title_prefix(sf=sf, prefix=raw_prefix, limit=200)
    text_extensions = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".py", ".sql", ".cls"}
    for cv in remaining_raw:
        fname = cv["title"].replace(raw_prefix, "")
        if fname in seen_files:
            continue
        ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext in text_extensions:
            raw_bytes = fetch_content_version_data(sf, cv["id"])
            if raw_bytes:
                corpus.append((fname, raw_bytes.decode("utf-8", errors="replace")))
                seen_files.add(fname)

    import json as _json
    # Delete old index/memory and re-upload from remaining corpus
    delete_content_documents_by_title_prefix(sf, f"nanorag/{library_name}/index/bm25.json")
    delete_content_documents_by_title_prefix(sf, f"nanorag/{library_name}/memory.md")

    if corpus:
        enriched_corpus = [(src, enrich_text(text, title=src)) for src, text in corpus]
        index = build_index(enriched_corpus)
        memory = build_memory_md(corpus)
        upload_content_version(sf=sf, title=f"nanorag/{library_name}/index/bm25.json",
                              data=_json.dumps(index).encode("utf-8"), path_on_client="bm25.json")
        upload_content_version(sf=sf, title=f"nanorag/{library_name}/memory.md",
                              data=memory.encode("utf-8"), path_on_client="memory.md")

    # Update topic metadata and re-attach to agents
    _rebuild_topic_and_update_agents(sf, library_name, corpus)

    return _success({
        "library_name": library_name,
        "files_deleted": count,
        "remaining_files": len(corpus),
    })


def _rebuild_topic_and_update_agents(sf, library_name: str, corpus: list) -> None:
    """Regenerate topic metadata and re-attach to all linked agents.

    Reads the manifest to find attached agents, regenerates topic metadata
    from the updated corpus, then re-runs attach for each agent so the
    AgentScript reflects the current library content.
    """
    from .sf_files import query_content_versions_by_title_prefix, fetch_content_version_data
    from .scorer import generate_topic_metadata, generate_topic_metadata_llm
    from . import manifest as manifest_mod
    from .attach import attach_library

    # Load manifest to find attached agents
    try:
        manifest_rows = query_content_versions_by_title_prefix(
            sf=sf, prefix=f"nanorag/{library_name}/manifest.json", limit=1, newest_first=True
        )
        if not manifest_rows:
            return
        raw = fetch_content_version_data(sf, manifest_rows[0]["id"])
        if not raw:
            return
        manifest = manifest_mod.load_manifest(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("_rebuild_topic: could not load manifest: %s", str(exc)[:200])
        return

    attachments = manifest.get("attachments") or []
    if not attachments:
        return

    # Generate new topic metadata
    topic_metadata = None
    if corpus:
        llm_result = generate_topic_metadata_llm(corpus, sf=sf)
        if llm_result:
            topic_name, description, instructions = llm_result
            manifest["topic_name"] = topic_name
            manifest["topic_description"] = description
            manifest["topic_instructions"] = instructions
            manifest["topic_source"] = "llm"
        else:
            topic_name, description, instructions = generate_topic_metadata(corpus)
            manifest["topic_name"] = topic_name
            manifest["topic_description"] = description
            manifest["topic_instructions"] = instructions
            manifest["topic_source"] = "deterministic"

    # Re-upload updated manifest
    from .sf_files import upload_content_version, delete_content_documents_by_title_prefix
    try:
        manifest_yaml = manifest_mod.dump_manifest(manifest)
        delete_content_documents_by_title_prefix(sf, f"nanorag/{library_name}/manifest.json")
        upload_content_version(sf=sf, title=f"nanorag/{library_name}/manifest.json",
                              data=manifest_yaml.encode("utf-8"), path_on_client="manifest.json")
    except Exception as exc:
        logger.warning("_rebuild_topic: manifest upload failed: %s", str(exc)[:200])

    # Re-attach to each agent (attach_library handles idempotent topic injection)
    for att in attachments:
        agent_name = att.get("agent_developer_name")
        if agent_name:
            logger.info("_rebuild_topic: re-attaching to %s", agent_name)
            try:
                attach_library(library_name, agent_name)
            except Exception as exc:
                logger.warning("_rebuild_topic: re-attach to %s failed: %s", agent_name, str(exc)[:200])


def _handle_search(args: Dict) -> Dict:
    """Search a library with a query. Returns BM25-ranked hits + file content."""
    from .sf_files import _get_sf_client, query_content_versions_by_title_prefix, fetch_content_version_data
    from .scorer import BM25Index

    library_name = args.get("library_name")
    query = args.get("query")
    top_k = args.get("top_k", 3)

    if not library_name:
        return _error("missing_arg", "library_name is required")
    if not query:
        return _error("missing_arg", "query is required")

    sf = _get_sf_client()

    # Load bm25.json from org
    index_title = f"nanorag/{library_name}/index/bm25.json"
    rows = query_content_versions_by_title_prefix(sf=sf, prefix=index_title, limit=1, newest_first=True)
    if not rows:
        return _error("index_not_found", f"No bm25.json found for library '{library_name}'")

    index_data = fetch_content_version_data(sf, rows[0]["id"])
    if not index_data:
        return _error("index_not_found", f"Could not read bm25.json for library '{library_name}'")

    import time
    t1 = time.time()
    index = BM25Index.from_string(index_data.decode("utf-8"))
    hits = index.query(query, top_k=top_k)
    bm25_ms = int((time.time() - t1) * 1000)

    # Read file content for top hits
    file_content = ""
    sources = []
    for hit in hits[:2]:
        src = hit["source"]
        # Try extracted/ first, then raw/
        text = None
        ext_rows = query_content_versions_by_title_prefix(
            sf=sf, prefix=f"nanorag/{library_name}/extracted/{src}.txt", limit=1
        )
        if ext_rows:
            raw = fetch_content_version_data(sf, ext_rows[0]["id"])
            if raw:
                text = raw.decode("utf-8", errors="replace")

        if not text:
            raw_rows = query_content_versions_by_title_prefix(
                sf=sf, prefix=f"nanorag/{library_name}/raw/{src}", limit=1
            )
            if raw_rows:
                raw = fetch_content_version_data(sf, raw_rows[0]["id"])
                if raw:
                    text = raw.decode("utf-8", errors="replace")

        if text:
            file_content += f"\n\n--- Source: {src} ---\n\n{text[:5000]}"
            sources.append(src)

    return _success({
        "library_name": library_name,
        "query": query,
        "hits": hits,
        "sources": sources,
        "file_content": file_content.strip() if file_content else None,
        "bm25_ms": bm25_ms,
    })


_COMMANDS = {
    "build": _handle_build,
    "install": _handle_install,
    "attach": _handle_attach,
    "detach": _handle_detach,
    "search": _handle_search,
    "library_list": _handle_library_list,
    "library_delete": _handle_library_delete,
    "file_list": _handle_file_list,
    "file_add": _handle_file_add,
    "file_delete": _handle_file_delete,
}


def main():
    _setup_logging()

    # Capture the real stdout for JSON output, redirect print() to stderr
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    raw = sys.stdin.read()
    if not raw.strip():
        json.dump(_error("no_input", "No JSON input received on stdin"), _real_stdout)
        sys.exit(1)

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as e:
        json.dump(_error("invalid_json", f"Failed to parse input: {e}"), _real_stdout)
        sys.exit(1)

    command = request.get("command")
    args = request.get("args", {})

    if command not in _COMMANDS:
        json.dump(_error("unknown_command", f"Unknown command: {command}. Available: {list(_COMMANDS.keys())}"), _real_stdout)
        sys.exit(1)

    try:
        result = _COMMANDS[command](args)
        json.dump(result, _real_stdout)
    except Exception as e:
        logger.exception("Command %s failed", command)
        json.dump(_error("internal_error", str(e)), _real_stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
