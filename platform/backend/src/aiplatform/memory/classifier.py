"""Heuristic memory classification: kind, importance, instruction-likeness and "is this worth remembering".

Deliberately cheap and deterministic (runs on every candidate). The LLM extractor proposes kind/confidence; these
heuristics validate and adjust them so a model can't, for example, smuggle an instruction in as a "fact".
"""

from __future__ import annotations

import re

from aiplatform.memory.models import MemoryKind

BASE_IMPORTANCE: dict[str, float] = {
    "instruction": 0.8,
    "preference": 0.7,
    "decision": 0.7,
    "project": 0.6,
    "fact": 0.5,
    "context": 0.35,
}

_KIND_HINTS: list[tuple[MemoryKind, re.Pattern[str]]] = [
    (
        "preference",
        re.compile(r"\b(prefer|prefers|like|likes|love|hate|dislike|favou?rite|rather|always use|use .* instead)\b", re.I),
    ),
    ("decision", re.compile(r"\b(decided|decision|we will|we'll|chose|chosen|agreed|going with|settled on)\b", re.I)),
    (
        "project",
        re.compile(r"\b(project|repo|repository|codebase|module|service|app|laravel|django|api|database|schema)\b", re.I),
    ),
    ("instruction", re.compile(r"\b(always|never|don't|do not|make sure|remember to|when you)\b", re.I)),
]
# Text that tries to steer the assistant's behaviour or permissions (memory-poisoning signal).
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore (all |any |the )?(previous|prior|above)|disregard|system prompt|you (must|should|are allowed|may now)|"
    r"without (asking|confirmation|permission)|grant|bypass|autonomous mode|run (the )?(shell|command)|"
    r"delete (all|every)|exfiltrat|send (it|this|the \w+) to|api[_ ]?key|password|token)\b",
    re.I,
)
_REMEMBER = re.compile(
    r"^\s*(please\s+)?(remember|note|keep in mind|don't forget|for future reference)\b[:,]?\s*(that\s+)?(?P<body>.+)$",
    re.I | re.S,
)
_FORGET = re.compile(r"^\s*(please\s+)?forget\b[:,]?\s*(that\s+|about\s+)?(?P<body>.+)$", re.I | re.S)
_TRIVIAL = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|sure|cool|great|nice|bye|good (morning|night))\W*$", re.I
)


def guess_kind(text: str, proposed: str | None = None) -> MemoryKind:
    # Accept the proposal unless it claims to be an instruction without looking like one.
    if proposed in BASE_IMPORTANCE and (proposed != "instruction" or _KIND_HINTS[3][1].search(text)):
        return proposed  # type: ignore[return-value]
    for kind, rx in _KIND_HINTS:
        if rx.search(text):
            return kind
    return "fact"


def importance(kind: str, *, explicit: bool, text: str) -> float:
    v = BASE_IMPORTANCE.get(kind, 0.5)
    if explicit:
        v += 0.15
    if len(text) < 25:
        v -= 0.05
    return max(0.05, min(1.0, v))


def instruction_like(text: str) -> bool:
    return bool(_INSTRUCTION_LIKE.search(text))


def explicit_remember(user_text: str) -> str | None:
    m = _REMEMBER.match(user_text)
    if not m:
        return None
    body = m.group("body").strip().rstrip(".!") + "."
    return body if len(body) >= 8 else None


def explicit_forget(user_text: str) -> str | None:
    m = _FORGET.match(user_text)
    return m.group("body").strip() if m else None


def trivial(text: str) -> bool:
    return bool(_TRIVIAL.match(text)) or len(text.strip()) < 4
