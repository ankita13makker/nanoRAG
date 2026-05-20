# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""5-level recursive delimiter-aware text chunker.

Delimiter hierarchy:
  0. Paragraphs (\\n\\n)
  1. Lines (\\n)
  2. Sentences (. ! ? followed by space or newline)
  3. Clauses (; : ,)
  4. Words (whitespace)

Config: 300-word chunks with 50-word sentence-aware overlap.
"""

from __future__ import annotations
import re
from dataclasses import dataclass

DELIMITERS: list[list[str]] = [
    ["\n\n"],
    ["\n"],
    [". ", "! ", "? ", ".\n", "!\n", "?\n"],
    ["; ", ": ", ", "],
    [],
]


@dataclass
class TextChunk:
    text: str
    index: int


def chunk_text(
    text: str, chunk_size: int = 300, chunk_overlap: int = 50
) -> list[TextChunk]:
    if not text or not text.strip():
        return []

    if _count_words(text) <= chunk_size:
        return [TextChunk(text=text.strip(), index=0)]

    pieces = _recursive_split(text, 0, chunk_size)
    merged = _greedy_merge(pieces, chunk_size)
    with_overlap = _apply_overlap(merged, chunk_overlap)

    return [TextChunk(text=t.strip(), index=i) for i, t in enumerate(with_overlap) if t.strip()]


def _recursive_split(text: str, level: int, target: int) -> list[str]:
    if level >= len(DELIMITERS):
        return _split_on_whitespace(text, target)

    delimiters = DELIMITERS[level]
    if not delimiters:
        return _split_on_whitespace(text, target)

    pieces = _split_at_delimiters(text, delimiters)

    if len(pieces) <= 1:
        return _recursive_split(text, level + 1, target)

    result: list[str] = []
    for piece in pieces:
        if _count_words(piece) > target:
            result.extend(_recursive_split(piece, level + 1, target))
        else:
            result.append(piece)

    return result


def _split_at_delimiters(text: str, delimiters: list[str]) -> list[str]:
    pieces: list[str] = []
    remaining = text

    while remaining:
        earliest = -1
        earliest_delim = ""

        for delim in delimiters:
            idx = remaining.find(delim)
            if idx != -1 and (earliest == -1 or idx < earliest):
                earliest = idx
                earliest_delim = delim

        if earliest == -1:
            if remaining.strip():
                pieces.append(remaining)
            break

        piece = remaining[: earliest + len(earliest_delim)]
        if piece.strip():
            pieces.append(piece)
        remaining = remaining[earliest + len(earliest_delim) :]

    return [p for p in pieces if p.strip()]


def _split_on_whitespace(text: str, target: int) -> list[str]:
    words = re.findall(r"\S+\s*", text)
    if not words:
        return []

    pieces: list[str] = []
    for i in range(0, len(words), target):
        chunk = "".join(words[i : i + target])
        if chunk.strip():
            pieces.append(chunk)
    return pieces


def _greedy_merge(pieces: list[str], target: int) -> list[str]:
    if not pieces:
        return []

    result: list[str] = []
    current = pieces[0]

    for i in range(1, len(pieces)):
        combined = current + pieces[i]
        if _count_words(combined) <= int(target * 1.5):
            current = combined
        else:
            result.append(current)
            current = pieces[i]

    if current.strip():
        result.append(current)

    return result


def _apply_overlap(chunks: list[str], overlap_words: int) -> list[str]:
    if len(chunks) <= 1 or overlap_words <= 0:
        return chunks

    result = [chunks[0]]
    for i in range(1, len(chunks)):
        trailing = _extract_trailing_context(chunks[i - 1], overlap_words)
        result.append(trailing + chunks[i])

    return result


def _extract_trailing_context(text: str, target_words: int) -> str:
    words = re.findall(r"\S+\s*", text)
    if len(words) <= target_words:
        return ""

    trailing = "".join(words[-target_words:])

    sentence_match = re.search(r"[.!?]\s+", trailing)
    if sentence_match and sentence_match.start() < len(trailing) / 2:
        after = re.sub(r"^[.!?]\s+", "", trailing[sentence_match.start() :])
        if after.strip():
            return after

    return trailing


def _count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))
