"""Memory lifecycle pipeline:  candidate → classify → sensitivity gate → dedupe → importance → store (active|candidate).

Nothing becomes long-term memory without passing this pipeline, whatever the source (explicit "remember", the
model's memory.save tool, background extraction, the admin console or an import).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import structlog

from aiplatform.audit.service import AuditLog
from aiplatform.config import MemorySettings
from aiplatform.db import Database
from aiplatform.memory import classifier
from aiplatform.memory.models import UNTRUSTED_SOURCES, MemoryItem, MemoryKind, Score, SourceType
from aiplatform.memory.repository import LongTermMemoryRepository
from aiplatform.model.embedding import LlamaCppEmbeddingProvider
from aiplatform.model.types import ProviderError
from aiplatform.observability.metrics import Metrics
from aiplatform.security import sensitivity
from aiplatform.shared.events import EventBus, MemoryChanged
from aiplatform.shared.text import content_hash, normalise

log = structlog.get_logger("memory")

Outcome = Literal[
    "stored_active", "stored_candidate", "duplicate", "merged", "superseded", "rejected_sensitive", "rejected_invalid"
]


@dataclass(frozen=True)
class Candidate:
    user_id: UUID
    content: str
    source_type: SourceType
    proposed_kind: str | None = None
    confidence: float = 0.6
    explicit: bool = False  # the user asked to remember it
    conversation_id: UUID | None = None
    message_id: int | None = None
    source_ref: str | None = None
    ttl_days: int | None = None


@dataclass(frozen=True)
class PipelineResult:
    outcome: Outcome
    memory_id: UUID | None
    reason: str = ""


class MemoryPipeline:
    def __init__(
        self,
        db: Database,
        repo: LongTermMemoryRepository,
        embedder: LlamaCppEmbeddingProvider,
        settings: MemorySettings,
        audit: AuditLog,
        metrics: Metrics,
        events: EventBus,
    ) -> None:
        self.db = db
        self.repo = repo
        self.embedder = embedder
        self.s = settings
        self.audit = audit
        self.metrics = metrics
        self.events = events

    async def submit(self, c: Candidate) -> PipelineResult:
        res = await self._submit(c)
        self.metrics.memory_ops.labels(res.outcome).inc()
        await self.audit.append(
            f"memory.{res.outcome}",
            actor_type="user" if c.source_type in ("user_stated", "manual") else "model",
            target=str(res.memory_id) if res.memory_id else None,
            outcome="success" if res.memory_id else "denied",
            detail={"source": c.source_type, "reason": res.reason, "chars": len(c.content)},
        )
        if res.memory_id:
            self.events.publish(MemoryChanged(c.user_id, res.memory_id, res.outcome))
        return res

    async def _submit(self, c: Candidate) -> PipelineResult:
        text = normalise(c.content)
        if not 8 <= len(text) <= 1000:
            return PipelineResult("rejected_invalid", None, "memories must be 8..1000 characters")
        # 1. classify
        kind: MemoryKind = classifier.guess_kind(text, c.proposed_kind)
        flags = ["instruction_like"] if classifier.instruction_like(text) else []
        # 2. sensitivity gate
        level, kinds = sensitivity.classify(text)
        if level == "secret" and not self.s.allow_sensitive:
            return PipelineResult("rejected_sensitive", None, f"looks like a secret ({', '.join(kinds)}); not stored")
        if level == "personal" and not (self.s.store_personal or c.explicit):
            return PipelineResult("rejected_sensitive", None, f"personal data ({', '.join(kinds)}) is only stored when you ask")
        # 3. exact dedupe
        existing = await self.repo.find_live_by_hash(c.user_id, content_hash(text))
        if existing is not None:
            await self._reinforce(existing, c)
            return PipelineResult("duplicate", existing.id, "already known")
        # 4. near-duplicate (semantic) dedupe → merge or supersede
        vec: list[float] | None
        try:
            vec = (await self.embedder.embed([text]))[0]
        except ProviderError as e:
            log.warning("memory_embed_failed", error=str(e))
            vec = None  # stored without a vector; the sweeper embeds it later
        supersedes: MemoryItem | None = None
        if vec is not None:
            for other, cos in await self.repo.nearest_live(c.user_id, vec, 3):
                if cos < self.s.dedupe_cosine:
                    break
                if other.kind == kind and (
                    c.explicit or c.source_type in ("user_stated", "manual") or c.confidence > other.confidence.value
                ):
                    supersedes = other  # newer statement of the same thing replaces the old one
                else:
                    await self._reinforce(other, c)
                    return PipelineResult("merged", other.id, f"near-duplicate of an existing memory (cos {cos:.2f})")
                break
        # 5. importance + confidence + status
        confidence = min(1.0, 0.95 if c.explicit or c.source_type in ("user_stated", "manual") else c.confidence)
        if flags:
            confidence *= 0.3
        trusted_source = c.source_type not in UNTRUSTED_SOURCES
        active = (
            trusted_source
            and not flags
            and (
                c.explicit
                or c.source_type in ("user_stated", "manual", "import")
                or confidence >= self.s.auto_activate_confidence
            )
        )
        ttl = c.ttl_days or self.s.default_ttl_days.get(kind)
        item = MemoryItem(
            id=None,
            user_id=c.user_id,
            kind=kind,
            content=text,
            importance=Score.clamp(classifier.importance(kind, explicit=c.explicit, text=text)),
            confidence=Score.clamp(confidence),
            sensitivity=level,
            status="active" if active else "candidate",
            source_type=c.source_type,
            source_conversation_id=c.conversation_id,
            source_message_id=c.message_id,
            source_ref=c.source_ref,
            supersedes_id=supersedes.id if supersedes else None,
            flags=flags,
            expires_at=datetime.now(UTC) + timedelta(days=ttl) if ttl else None,
        )
        async with self.db.transaction() as tx:
            if supersedes is not None and active:
                supersedes.transition("superseded")
                await self.repo.save_state(supersedes, tx)
            mid = await self.repo.insert(item, vec, tx)
        if supersedes is not None and active:
            return PipelineResult("superseded", mid, f"replaced an older memory ({supersedes.id})")
        return PipelineResult(
            "stored_active" if active else "stored_candidate",
            mid,
            ""
            if active
            else (
                "needs review: "
                + ("instruction-like text" if flags else "untrusted source" if not trusted_source else "low confidence")
            ),
        )

    async def _reinforce(self, m: MemoryItem, c: Candidate) -> None:
        m.confidence = Score.clamp(max(m.confidence.value, min(0.95, c.confidence + 0.1)))
        if c.explicit and m.status == "candidate" and "instruction_like" not in m.flags:
            m.transition("active")
        await self.repo.save_state(m)

    # ── human actions (admin console / API) ──
    async def approve(self, memory_id: UUID, user_id: UUID) -> None:
        m = await self._get(memory_id, user_id)
        m.transition("active")
        m.flags = [f for f in m.flags if f != "instruction_like"]
        m.confidence = Score.clamp(max(m.confidence.value, 0.9))
        await self.repo.save_state(m)
        vecs = await self.embedder.embed([m.content])
        await self.repo.replace_embedding(memory_id, vecs[0])
        await self.audit.append(
            "memory.approve", actor_type="user", actor_id=str(user_id), target=str(memory_id), outcome="success"
        )

    async def reject(self, memory_id: UUID, user_id: UUID) -> None:
        m = await self._get(memory_id, user_id)
        m.transition("rejected")
        await self.repo.save_state(m)
        await self.audit.append(
            "memory.reject", actor_type="user", actor_id=str(user_id), target=str(memory_id), outcome="success"
        )

    async def update(
        self,
        memory_id: UUID,
        user_id: UUID,
        *,
        content: str | None,
        kind: str | None,
        importance: float | None,
        expires_at: datetime | None,
        clear_expiry: bool = False,
    ) -> None:
        m = await self._get(memory_id, user_id)
        if content is not None:
            text = normalise(content)
            if not 8 <= len(text) <= 1000:
                from aiplatform.shared.errors import ValidationFailed

                raise ValidationFailed("memories must be 8..1000 characters")
            m.content = text
            m.sensitivity = sensitivity.classify(text)[0]
        if kind is not None:
            m.kind = classifier.guess_kind(m.content, kind)
        if importance is not None:
            m.importance = Score.clamp(importance)
        if expires_at is not None or clear_expiry:
            m.expires_at = expires_at
        async with self.db.transaction() as tx:
            await self.repo.save_state(m, tx)
            if content is not None and m.status in ("active", "candidate"):
                vecs = await self.embedder.embed([m.content])
                await self.repo.replace_embedding(memory_id, vecs[0], tx)
        await self.audit.append(
            "memory.update", actor_type="user", actor_id=str(user_id), target=str(memory_id), outcome="success"
        )

    async def delete(self, memory_id: UUID, user_id: UUID, *, actor: Literal["user", "model"] = "user") -> None:
        m = await self._get(memory_id, user_id)
        m.transition("deleted")
        await self.repo.save_state(m)
        await self.audit.append(
            "memory.delete",
            actor_type=actor,
            actor_id=str(user_id) if actor == "user" else None,
            target=str(memory_id),
            outcome="success",
        )

    async def _get(self, memory_id: UUID, user_id: UUID) -> MemoryItem:
        m = await self.repo.get(memory_id, user_id)
        if m is None:
            from aiplatform.shared.errors import NotFound

            raise NotFound("memory not found")
        return m
