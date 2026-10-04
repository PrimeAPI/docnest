"""Build a short highlighted excerpt from decrypted text (in memory only)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.search import tokenizer

CONTEXT = 90


@dataclass
class Snippet:
    text: str
    highlights: list[tuple[int, int]]


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Fold text like the tokenizer does, keeping a map from folded to original offsets."""
    out: list[str] = []
    mapping: list[int] = []
    for i, ch in enumerate(text):
        folded = tokenizer.fold(tokenizer.normalize(ch))
        for c in folded:
            out.append(c)
            mapping.append(i)
    return "".join(out), mapping


def make_snippet(text: str, query: str) -> Snippet | None:
    qwords = tokenizer.query_words(query)
    if not text or not qwords:
        return None
    folded, mapping = _fold_with_map(text)
    spans: list[tuple[int, int]] = []
    for word in qwords:
        needles = sorted(
            {tokenizer.fold(n) for n in tokenizer.iter_snippet_needles(word)}, key=len, reverse=True
        )
        for needle in needles:
            if len(needle) < 2:
                continue
            for m in re.finditer(re.escape(needle), folded):
                start, end = mapping[m.start()], mapping[m.end() - 1] + 1
                # extend to the end of the word for stem matches
                while end < len(text) and text[end].isalnum():
                    end += 1
                spans.append((start, end))
            if spans:
                break
    if not spans:
        excerpt = text[: CONTEXT * 2].strip()
        return Snippet(re.sub(r"\s+", " ", excerpt), [])
    spans.sort()
    first = spans[0][0]
    win_start = max(0, first - CONTEXT)
    win_end = min(len(text), first + CONTEXT * 2)
    raw = text[win_start:win_end]
    # collapse whitespace while tracking offsets
    out: list[str] = []
    offs: list[int] = []
    prev_space = False
    for i, ch in enumerate(raw):
        if ch.isspace():
            if prev_space:
                continue
            ch = " "
            prev_space = True
        else:
            prev_space = False
        out.append(ch)
        offs.append(win_start + i)
    snippet = "".join(out)
    highlights: list[tuple[int, int]] = []
    for s, e in spans:
        if s < win_start or e > win_end:
            continue
        try:
            hs = offs.index(s)
            he = max(i for i, o in enumerate(offs) if o < e) + 1
        except ValueError:
            continue
        if not highlights or hs >= highlights[-1][1]:
            highlights.append((hs, he))
    prefix = "… " if win_start > 0 else ""
    suffix = " …" if win_end < len(text) else ""
    lead = len(snippet) - len(snippet.lstrip())
    body = snippet.strip()
    shift = len(prefix) - lead
    return Snippet(
        prefix + body + suffix,
        [(a + shift, b + shift) for a, b in highlights if a >= lead and b - lead <= len(body)],
    )
