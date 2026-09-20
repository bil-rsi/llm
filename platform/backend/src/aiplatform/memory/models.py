"""Memory domain: long-term MemoryItem aggregate (status state machine + value objects) and short-term items."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from aiplatform.shared.errors import ValidationFailed

MemoryKind = Literal["preference", "fact", "project", "instruction", "decision", "context"]
MemoryStatus = Literal["candidate", "active", "superseded", "rejected", "expired", "deleted"]
SourceType = Literal["user_stated", "model_extracted", "tool_output", "web", "manual", "import"]
Sensitivity = Literal["none", "personal", "secret"]
ShortTermKind = Literal["task_state", "tool_state", "note", "summary", "fact"]

KINDS: tuple[MemoryKind, ...] = ("preference", "fact", "project", "instruction", "decision", "context")
UNTRUSTED_SOURCES: frozenset[str] = frozenset({"tool_output", "web"})

_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"active", "rejected", "deleted", "expired", "superseded"},
    "active": {"superseded", "expired", "deleted"},
    "superseded": {"deleted"},
    "rejected": {"deleted"},
    "expired": {"active", "deleted"},
    "deleted": set(),
}


@dataclass(frozen=True)
class Score:
    """0..1 value object used for importance and confidence."""

    value: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValidationFailed("scores must be between 0 and 1")

    @classmethod
    def clamp(cls, v: float) -> Score:
        return cls(round(max(0.0, min(1.0, v)), 3))


@dataclass
class MemoryItem:
    id: UUID | None
    user_id: UUID
    kind: MemoryKind
    content: str
    importance: Score
    confidence: Score
    sensitivity: Sensitivity
    status: MemoryStatus
    source_type: SourceType
    source_conversation_id: UUID | None = None
    source_message_id: int | None = None
    source_ref: str | None = None
    supersedes_id: UUID | None = None
    flags: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    access_count: int = 0

    def transition(self, to: MemoryStatus) -> None:
        if to != self.status and to not in _TRANSITIONS[self.status]:
            raise ValidationFailed(f"memory cannot go from {self.status} to {to}")
        self.status = to

    @property
    def trusted(self) -> bool:
        return self.source_type not in UNTRUSTED_SOURCES and "instruction_like" not in self.flags


@dataclass(frozen=True)
class RetrievedMemory:
    id: UUID
    kind: str
    content: str
    importance: float
    confidence: float
    source_type: str
    created_at: datetime
    updated_at: datetime
    score: float
    cosine: float | None
    vector_rank: int | None
    keyword_rank: int | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShortTermItem:
    kind: ShortTermKind
    key: str
    content: str
    importance: float
    expires_at: datetime
    updated_at: datetime
