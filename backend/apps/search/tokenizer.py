"""Text normalization and term generation for the blind index.

Each word yields several term kinds, all of which are later turned into keyed
hashes (no plaintext is stored):

    W  exact (folded) word            "fahrzeugversicherung"
    S  stem (German and English)      "fahrzeugversicher"
    P  prefixes (3..15 chars)         "fah", "fahr", ... (prefix search)
    X  compound tails (>= 4 chars)    "versicherung", "sicherung", ... (German compounds)
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "å": "aa"})

MIN_WORD = 2
MAX_WORD = 40
PREFIX_MIN = 3
PREFIX_MAX = 15
TAIL_MIN_WORD = 8
TAIL_MIN = 4

STOPWORDS = frozenset(
    [
        "der",
        "die",
        "das",
        "und",
        "oder",
        "ein",
        "eine",
        "einer",
        "eines",
        "einem",
        "einen",
        "ist",
        "sind",
        "war",
        "wird",
        "werden",
        "wurde",
        "mit",
        "von",
        "vom",
        "zu",
        "zum",
        "zur",
        "im",
        "in",
        "an",
        "am",
        "auf",
        "aus",
        "bei",
        "fuer",
        "für",
        "nicht",
        "auch",
        "als",
        "wie",
        "es",
        "sie",
        "er",
        "wir",
        "ihr",
        "ich",
        "du",
        "sich",
        "dem",
        "den",
        "des",
        "so",
        "dass",
        "daß",
        "nach",
        "ueber",
        "über",
        "unter",
        "vor",
        "bis",
        "durch",
        "ohne",
        "gegen",
        "um",
        "noch",
        "nur",
        "sehr",
        "hier",
        "dort",
        "ja",
        "nein",
        "kein",
        "keine",
        "mehr",
        "ihre",
        "ihrer",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "at",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "not",
        "your",
        "you",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
        "his",
        "her",
        "i",
        "me",
        "my",
    ]
)

_stemmers = {
    "de": snowballstemmer.stemmer("german"),
    "en": snowballstemmer.stemmer("english"),
}


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def fold(word: str) -> str:
    return word.translate(_FOLD)


def words(text: str) -> list[str]:
    """Normalized (casefolded, NOT umlaut-folded) words."""
    return [w for w in _WORD_RE.findall(normalize(text)) if MIN_WORD <= len(w) <= MAX_WORD]


@lru_cache(maxsize=50_000)
def stems(word: str) -> tuple[str, ...]:
    out = {fold(_stemmers["de"].stemWord(word)), fold(_stemmers["en"].stemWord(word))}
    return tuple(sorted(s for s in out if len(s) >= MIN_WORD))


@dataclass(frozen=True)
class Term:
    kind: str  # W, S, P, X
    value: str


def word_terms(word: str) -> set[Term]:
    folded = fold(word)
    terms = {Term("W", folded)}
    terms.update(Term("S", s) for s in stems(word))
    if folded in STOPWORDS or word in STOPWORDS or folded.isdigit():
        return terms
    for n in range(PREFIX_MIN, min(PREFIX_MAX, len(folded) - 1) + 1):
        terms.add(Term("P", folded[:n]))
    if len(folded) >= TAIL_MIN_WORD:
        for start in range(3, len(folded) - TAIL_MIN + 1):
            terms.add(Term("X", folded[start:]))
    return terms


def document_terms(text: str) -> tuple[Counter[Term], int]:
    """Term frequencies for a text, plus the number of words."""
    counts: Counter[Term] = Counter()
    ws = words(text)
    word_counts = Counter(ws)
    for word, n in word_counts.items():
        for term in word_terms(word):
            counts[term] += n
    return counts, len(ws)


# Query side ------------------------------------------------------------------

WEIGHTS = {"W": 1.0, "S": 0.9, "X": 0.7, "P": 0.5}


def query_terms(word: str) -> dict[Term, float]:
    """Terms a single query word may match, with match-quality weights."""
    folded = fold(word)
    out: dict[Term, float] = {Term("W", folded): WEIGHTS["W"], Term("P", folded): WEIGHTS["P"]}
    for s in stems(word):
        out[Term("S", s)] = WEIGHTS["S"]
        if len(s) >= TAIL_MIN:
            out.setdefault(Term("X", s), WEIGHTS["X"] * 0.9)  # inflected compound tail
    if len(folded) >= TAIL_MIN:
        out[Term("X", folded)] = WEIGHTS["X"]
    return out


def query_words(query: str, limit: int = 12) -> list[str]:
    seen: list[str] = []
    for w in words(query):
        if w not in seen and fold(w) not in STOPWORDS:
            seen.append(w)
    if not seen:  # query consisting only of stopwords: still search them
        seen = list(dict.fromkeys(words(query)))
    return seen[:limit]


def iter_snippet_needles(word: str) -> Iterable[str]:
    yield normalize(word)
    yield from stems(word)
