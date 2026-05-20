# Copyright (c) 2024, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Build script: scans knowledge directories, generates nanorag artifacts.

Each library has:
  - manifest.json — file inventory with hashes, tracks add/update/remove
  - memory.md    — per-file summary (sections, topics, tags, related files) for agent fallback
  - bm25.json    — keyword index over full document content (one entry per file)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("nanorag")

from .chunker import chunk_text
from .constants import OUTPUT_DIR, KNOWLEDGE_DIR as DEFAULT_KNOWLEDGE_DIR, ARTIFACTS, SF_PATH_PREFIX
from .extractors import extract_text, is_supported
from .scorer import build_index, build_memory_md, enrich_text, generate_topic_metadata

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB per file
MAX_LIBRARY_SIZE_BYTES = 150 * 1024 * 1024  # 150 MB per library
MAX_FILE_COUNT = 1000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(out_dir: Path) -> dict:
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {}


def _save_manifest(out_dir: Path, manifest: dict) -> None:
    manifest["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def scan_files(directory: Path) -> list[Path]:
    """Scan for supported files, enforcing per-file and per-library limits."""
    files = []
    total_size = 0
    for f in sorted(directory.rglob("*")):
        if not f.is_file() or not is_supported(f):
            continue
        size = f.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            logger.warning("Skipping %s (%.1f MB exceeds limit)", f.name, size / 1024 / 1024)
            continue
        if total_size + size > MAX_LIBRARY_SIZE_BYTES:
            logger.warning("Library size limit reached (150 MB). Skipping remaining files.")
            break
        if len(files) >= MAX_FILE_COUNT:
            logger.warning("File count limit reached (%d). Skipping remaining files.", MAX_FILE_COUNT)
            break
        files.append(f)
        total_size += size
    return files


def parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    yaml_block = content[3:end].strip()
    body = content[end + 4:].strip()
    meta: dict = {}
    for line in yaml_block.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        meta[key] = val
    return meta, body


def infer_title(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()




def sync_library(
    library_id: str,
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    output_dir: str | Path = OUTPUT_DIR,
    upload_sf: bool = False,
) -> dict:
    """Incremental sync: compares file hashes against manifest,
    extracts only changed files, rebuilds artifacts.

    Returns {"added": [...], "updated": [...], "removed": [...], "unchanged": [...], "total": int}
    """
    knowledge_dir = Path(knowledge_dir)
    output_dir = Path(output_dir)
    source = knowledge_dir / library_id
    if not source.exists():
        return {"added": [], "updated": [], "removed": [], "unchanged": [], "total": 0}

    out = output_dir / library_id
    out.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(out)
    old_files = manifest.get("files", {})

    current_files = scan_files(source)
    current_by_name = {f.name: f for f in current_files}

    added, updated, unchanged, removed = [], [], [], []

    for name, path in current_by_name.items():
        file_hash = _sha256(path)
        file_size = path.stat().st_size
        old_entry = old_files.get(name)
        if old_entry is None:
            added.append(name)
        elif old_entry.get("sha256") != file_hash:
            updated.append(name)
        else:
            unchanged.append(name)
        old_files[name] = {
            "sha256": file_hash,
            "size": file_size,
            "status": "indexed",
        }

    for name in list(old_files.keys()):
        if name not in current_by_name:
            removed.append(name)
            del old_files[name]

    delta = {"added": added, "updated": updated, "removed": removed, "unchanged": unchanged}

    if not added and not updated and not removed:
        delta["total"] = len(unchanged)
        return delta

    if added:
        print(f"  + {len(added)} new: {', '.join(added[:5])}")
    if updated:
        print(f"  ~ {len(updated)} updated: {', '.join(updated[:5])}")
    if removed:
        print(f"  - {len(removed)} removed: {', '.join(removed[:5])}")

    manifest.setdefault("library_id", str(uuid.uuid4())[:8])
    manifest.setdefault("label", library_id)
    manifest.setdefault("created", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    manifest["files"] = old_files

    _save_manifest(out, manifest)

    count = _build_nanorag(source, out)
    delta["total"] = count

    if upload_sf and count > 0:
        _upload_to_salesforce_async(out, source, library_id)

    return delta


def build_library(
    library_id: str,
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    output_dir: str | Path = OUTPUT_DIR,
    upload_sf: bool = False,
) -> int:
    """Full rebuild of a single library (no incremental diff)."""
    knowledge_dir = Path(knowledge_dir)
    output_dir = Path(output_dir)
    source = knowledge_dir / library_id
    if not source.exists():
        return 0
    out = output_dir / library_id
    out.mkdir(parents=True, exist_ok=True)

    current_files = scan_files(source)
    files_dict = {}
    for f in current_files:
        files_dict[f.name] = {
            "sha256": _sha256(f),
            "size": f.stat().st_size,
            "status": "indexed",
        }

    manifest = _load_manifest(out)
    manifest.setdefault("library_id", str(uuid.uuid4())[:8])
    manifest.setdefault("label", library_id)
    manifest.setdefault("created", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    manifest["files"] = files_dict
    _save_manifest(out, manifest)

    count = _build_nanorag(source, out)
    if upload_sf and count > 0:
        _upload_to_salesforce_async(out, source, library_id)
    return count


def build_all(
    knowledge_dir: str | Path,
    output_dir: str | Path,
    upload_sf: bool = False,
) -> None:
    """Build all libraries found in knowledge_dir."""
    knowledge_dir = Path(knowledge_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    library_dirs = sorted(
        [d for d in knowledge_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=lambda d: d.name,
    )

    if not library_dirs:
        files = scan_files(knowledge_dir)
        if files:
            _build_nanorag(knowledge_dir, output_dir)
            if upload_sf:
                _upload_to_salesforce(output_dir, knowledge_dir, knowledge_dir.name)
        else:
            print("No files found in knowledge/.")
        return

    total_libraries = 0
    total_files = 0

    for lib_dir in library_dirs:
        files = scan_files(lib_dir)
        if not files:
            continue

        library_id = lib_dir.name
        count = build_library(library_id, knowledge_dir, output_dir, upload_sf=upload_sf)
        total_libraries += 1
        total_files += count

    print(f"\nBuilt {total_libraries} libraries, {total_files} total files indexed.")
    print(f"  -> {output_dir}/")


def _upload_to_salesforce(output_dir: Path, knowledge_dir: Path, label: str) -> None:
    from .salesforce import upload_nanorag
    print(f"\n  Uploading {label} to Salesforce Files...")
    result = upload_nanorag(output_dir, knowledge_dir, label)
    print(f"  Done: {result['artifacts']} artifacts, {result['raw_files']} raw files, {result['extracted']} extracted")


_pending_uploads: list[threading.Thread] = []
_pending_lock = threading.Lock()


def _upload_to_salesforce_async(output_dir: Path, knowledge_dir: Path, label: str) -> None:
    def _run():
        try:
            _upload_to_salesforce(output_dir, knowledge_dir, label)
        except Exception:
            logger.exception("Background SF upload failed for %s", label)
        finally:
            with _pending_lock:
                _pending_uploads.remove(t)

    t = threading.Thread(target=_run, daemon=False)
    with _pending_lock:
        _pending_uploads.append(t)
    t.start()


import atexit

def _wait_for_pending_uploads() -> None:
    with _pending_lock:
        threads = list(_pending_uploads)
    for t in threads:
        t.join(timeout=300)

atexit.register(_wait_for_pending_uploads)


def _build_nanorag(source_dir: Path, out_dir: Path) -> int:
    files = scan_files(source_dir)
    if not files:
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    file_entries: list[dict] = []
    for f in files:
        raw = extract_text(f)
        if not raw.strip():
            print(f"  Skipping {f.name} (no extractable text)")
            continue
        meta, body = parse_frontmatter(raw)
        title = meta.get("title", infer_title(f))
        chunks = chunk_text(body, chunk_size=300, chunk_overlap=50)
        file_entries.append({
            "path": f,
            "title": title,
            "meta": meta,
            "body": body,
            "chunks": chunks,
        })

    if not file_entries:
        return 0

    raw_corpus = [(entry["path"].name, entry["body"]) for entry in file_entries]
    memory_md = build_memory_md(raw_corpus)
    (out_dir / "memory.md").write_text(memory_md, encoding="utf-8")

    # Generate topic metadata from corpus and persist to manifest
    topic_name, topic_desc, topic_instructions = generate_topic_metadata(raw_corpus)
    manifest = _load_manifest(out_dir)
    manifest["topic_name"] = topic_name
    manifest["topic_description"] = topic_desc
    manifest["topic_instructions"] = topic_instructions
    manifest["topic_source"] = "deterministic"
    _save_manifest(out_dir, manifest)

    _build_bm25_index(file_entries, out_dir)

    print(f"  {out_dir.name}: {len(file_entries)} files indexed")
    return len(file_entries)


def _build_bm25_index(file_entries: list[dict], out_dir: Path) -> None:
    corpus: list[tuple[str, str]] = []
    for entry in file_entries:
        chunk_text_joined = " ".join(c.text for c in entry.get("chunks", []))
        full_text = entry["body"] + " " + chunk_text_joined
        tags = entry["meta"].get("tags", [])
        enriched = enrich_text(full_text, title=entry["title"], tags=tags)
        corpus.append((entry["path"].name, enriched))

    if not corpus:
        return

    bm25_data = build_index(corpus)

    (out_dir / "bm25.json").write_text(
        json.dumps(bm25_data, separators=(",", ":")),
        encoding="utf-8",
    )
