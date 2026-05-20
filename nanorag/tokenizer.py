# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Shared tokenizer with stemming and bigrams for BM25 indexing and querying."""
from __future__ import annotations

import re

STOPWORDS = frozenset(
    "a an the and or but in on at to for of is it its this that with from by as be are was "
    "were been has have had do does did not no nor so if all any each every how what which who "
    "will can may shall would could should than then more most very also about above after "
    "before between into through during under again further once here there when where why "
    "own same up down out off over such few other some only just you your".split()
)

SUFFIX_RULES = [
    ("ies", "y"),
    ("ves", "f"),
    ("ses", "s"),
    ("ches", "ch"),
    ("shes", "sh"),
    ("xes", "x"),
    ("zing", "z"),
    ("ting", "t"),
    ("ning", "n"),
    ("ring", "r"),
    ("ling", "l"),
    ("ping", "p"),
    ("bing", "b"),
    ("ding", "d"),
    ("ging", "g"),
    ("ming", "m"),
    ("ness", ""),
    ("ment", ""),
    ("tion", ""),
    ("sion", ""),
    ("able", ""),
    ("ible", ""),
    ("ally", ""),
    ("ical", ""),
    ("ated", ""),
    ("ized", ""),
    ("iser", ""),
    ("izer", ""),
    ("eful", ""),
    ("less", ""),
    ("ings", ""),
    ("ment", ""),
    ("ing", ""),
    ("eds", ""),
    ("ers", ""),
    ("est", ""),
    ("ful", ""),
    ("ous", ""),
    ("ive", ""),
    ("ity", ""),
    ("ing", ""),
    ("ed", ""),
    ("er", ""),
    ("ly", ""),
    ("es", ""),
    ("s", ""),
]


def stem(word: str) -> str:
    if len(word) <= 3:
        return word
    for suffix, replacement in SUFFIX_RULES:
        if word.endswith(suffix) and len(word) - len(suffix) + len(replacement) >= 3:
            return word[:-len(suffix)] + replacement
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9]*", text.lower())
    filtered = [w for w in words if w not in STOPWORDS and len(w) >= 2]

    stemmed = [stem(w) for w in filtered]

    bigrams = []
    for i in range(len(stemmed) - 1):
        bigrams.append(f"{stemmed[i]}_{stemmed[i+1]}")

    return stemmed + bigrams
