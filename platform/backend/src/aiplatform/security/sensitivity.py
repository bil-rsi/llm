"""Detects secrets and personal data in text (memory gating, log/tool-output redaction, URL exfiltration checks)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

Sensitivity = Literal["none", "personal", "secret"]

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("openai_like_key", re.compile(r"\bsk-(proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("platform_token", re.compile(r"\baip_[0-9a-f]{32,}\b")),
    ("password_assignment", re.compile(r"(?i)\b(pass(word|wd)?|pwd|secret|api[_-]?key|token|auth)\s*[:=]\s*[\"']?[^\s\"']{6,}")),
    ("connection_string", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)")
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


@dataclass(frozen=True)
class Finding:
    kind: str
    level: Sensitivity
    start: int
    end: int


def scan(text: str) -> list[Finding]:
    out: list[Finding] = []
    for name, rx in _SECRET_PATTERNS:
        out += [Finding(name, "secret", m.start(), m.end()) for m in rx.finditer(text)]
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            out.append(Finding("card_number", "secret", m.start(), m.end()))
    out += [Finding("iban", "secret", m.start(), m.end()) for m in _IBAN.finditer(text)]
    for m in _HIGH_ENTROPY.finditer(text):
        tok = m.group()
        if (
            any(c.isdigit() for c in tok)
            and any(c.isalpha() for c in tok)
            and _entropy(tok) >= 4.0
            and not re.fullmatch(r"[0-9a-f]{32,64}", tok)
        ):  # plain hex digests (git SHAs, checksums) are not secrets
            out.append(Finding("high_entropy", "secret", m.start(), m.end()))
    out += [Finding("email", "personal", m.start(), m.end()) for m in _EMAIL.finditer(text)]
    out += [
        Finding("phone", "personal", m.start(), m.end()) for m in _PHONE.finditer(text) if len(re.sub(r"\D", "", m.group())) >= 9
    ]
    return out


def classify(text: str) -> tuple[Sensitivity, list[str]]:
    findings = scan(text)
    kinds = sorted({f.kind for f in findings})
    if any(f.level == "secret" for f in findings):
        return "secret", kinds
    if findings:
        return "personal", kinds
    return "none", kinds


def redact(text: str, *, personal: bool = False) -> str:
    """Replace secret spans (and personal spans if requested) with [REDACTED:kind]. Overlaps are merged."""
    spans = sorted((f for f in scan(text) if f.level == "secret" or personal), key=lambda f: (f.start, -f.end))
    if not spans:
        return text
    out, pos = [], 0
    for f in spans:
        if f.start < pos:
            continue
        out.append(text[pos : f.start])
        out.append(f"[REDACTED:{f.kind}]")
        pos = f.end
    out.append(text[pos:])
    return "".join(out)
