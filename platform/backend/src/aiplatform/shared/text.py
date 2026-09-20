"""Small text helpers shared across contexts (normalisation, hashing, token estimates)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[0-9a-zA-ZÀ-ɏ]+")

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yes",
        "ok",
        "okay",
        "hi",
        "hello",
        "thanks",
        "thank",
    ]
)


def normalise(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def content_hash(text: str) -> bytes:
    return hashlib.sha256(normalise(text).lower().encode("utf-8")).digest()


def keywords(text: str, limit: int = 12) -> list[str]:
    """Significant lowercase terms, de-duplicated, in order. Output only contains [0-9a-z...] word characters."""
    seen: dict[str, None] = {}
    for w in _WORD.findall(text.lower()):
        if len(w) >= 3 and w not in STOPWORDS and w not in seen:
            seen[w] = None
            if len(seen) >= limit:
                break
    return list(seen)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(keywords(a, 64)), set(keywords(b, 64))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def truncate(text: str, max_chars: int, marker: str = "\n…[truncated]") -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - len(marker))] + marker


class TokenEstimator:
    """Fast token estimate (chars / ratio) calibrated from the provider's real prompt token counts.

    Counting through a tokenizer endpoint on every message would cost milliseconds per request; the calibrated
    estimate is within a few percent for the same model, and the context builder keeps a safety margin.
    """

    def __init__(self, chars_per_token: float = 3.6, margin: float = 1.08) -> None:
        self.chars_per_token = chars_per_token
        self.margin = margin

    def count(self, text: str) -> int:
        return int(len(text) / self.chars_per_token * self.margin) + 4

    def calibrate(self, prompt_chars: int, real_prompt_tokens: int) -> None:
        if real_prompt_tokens <= 50 or prompt_chars <= 200:
            return
        observed = prompt_chars / real_prompt_tokens
        if 1.0 <= observed <= 8.0:
            self.chars_per_token = 0.8 * self.chars_per_token + 0.2 * observed
