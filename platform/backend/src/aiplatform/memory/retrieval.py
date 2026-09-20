"""Memory retrieval: gate → embed query → hybrid SQL (vector + keyword) → RRF fusion with importance/confidence/recency
weighting → threshold → near-duplicate removal → top-N. Strategies are pluggable (hybrid, keyword, semantic)."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from aiplatform.config import RetrievalSettings
from aiplatform.memory import classifier
from aiplatform.memory.models import RetrievedMemory
from aiplatform.memory.repository import LongTermMemoryRepository
from aiplatform.model.embedding import LlamaCppEmbeddingProvider
from aiplatform.model.types import ProviderError
from aiplatform.shared import timing
from aiplatform.shared.text import jaccard, keywords

log = structlog.get_logger("retrieval")
Mode = Literal["hybrid", "keyword", "semantic"]
_SAFE_TERM = re.compile(r"^[0-9a-zÀ-ɏ]+$")


def to_tsquery(text: str) -> str:
    """OR-query of significant terms with prefix matching. Terms are strictly [0-9a-z...] so no tsquery syntax injection."""
    terms = [t for t in keywords(text, 12) if _SAFE_TERM.match(t)]
    return " | ".join(f"{t}:*" if len(t) >= 4 else t for t in terms)


@dataclass(frozen=True)
class RetrievalResult:
    memories: list[RetrievedMemory]
    skipped: str | None
    candidates: int
    ms: float


def fuse(rows: Sequence[Mapping[str, Any]], s: RetrievalSettings, now: datetime) -> list[RetrievedMemory]:
    out: list[RetrievedMemory] = []
    for r in rows:
        vr, kr, cos = r["vector_rank"], r["keyword_rank"], r["cosine"]
        vec_ok = vr is not None and (cos is None or float(cos) >= s.min_cosine)
        rrf = (1.0 / (s.rrf_k + int(vr)) if vec_ok else 0.0) + (1.0 / (s.rrf_k + int(kr)) if kr is not None else 0.0)
        if rrf == 0.0:
            continue
        updated: datetime = r["updated_at"]
        age_days = max(0.0, (now - updated).total_seconds() / 86400)
        recency = 0.7 + 0.3 * math.pow(0.5, age_days / max(1.0, s.recency_half_life_days))
        imp, conf = float(r["importance"]), float(r["confidence"])
        score = rrf * (0.5 + 0.5 * imp) * (0.6 + 0.4 * conf) * recency
        out.append(
            RetrievedMemory(
                id=r["id"],
                kind=str(r["kind"]),
                content=str(r["content"]),
                importance=imp,
                confidence=conf,
                source_type=str(r["source_type"]),
                created_at=r["created_at"],
                updated_at=updated,
                score=score,
                cosine=float(cos) if cos is not None else None,
                vector_rank=int(vr) if vr is not None else None,
                keyword_rank=int(kr) if kr is not None else None,
                flags=tuple(r["flags"] or ()),
            )
        )
    out.sort(key=lambda m: m.score, reverse=True)
    return out


def drop_near_duplicates(ms: list[RetrievedMemory], threshold: float = 0.8) -> list[RetrievedMemory]:
    kept: list[RetrievedMemory] = []
    for m in ms:
        if all(jaccard(m.content, k.content) < threshold for k in kept):
            kept.append(m)
    return kept


class MemoryRetriever:
    def __init__(self, repo: LongTermMemoryRepository, embedder: LlamaCppEmbeddingProvider, s: RetrievalSettings) -> None:
        self.repo = repo
        self.embedder = embedder
        self.s = s
        self._active_count: dict[UUID, int] = {}
        self._touch_tasks: set[asyncio.Task[None]] = set()

    def invalidate(self, user_id: UUID) -> None:
        self._active_count.pop(user_id, None)

    async def retrieve(
        self, user_id: UUID, query: str, *, mode: Mode = "hybrid", limit: int | None = None, force: bool = False
    ) -> RetrievalResult:
        import time

        t0 = time.perf_counter()
        with timing.stage("memory_retrieval"):
            if not force and classifier.trivial(query):
                return RetrievalResult([], "trivial message", 0, 0.0)
            if user_id not in self._active_count:
                self._active_count[user_id] = await self.repo.count_active(user_id)
            if self._active_count[user_id] == 0:
                return RetrievalResult([], "no memories yet", 0, (time.perf_counter() - t0) * 1000)
            tsq = to_tsquery(query)
            rows: list[asyncpg.Record]
            if mode == "keyword":
                rows = await self.repo.keyword(user_id, tsq, self.s.top_k)
            else:
                try:
                    vec = await self.embedder.embed_query(query)
                except ProviderError as e:
                    log.warning("retrieval_embed_failed_fallback_keyword", error=str(e))
                    rows = await self.repo.keyword(user_id, tsq, self.s.top_k)
                else:
                    with timing.stage("hybrid_sql"):
                        rows = await self.repo.hybrid(user_id, vec, "" if mode == "semantic" else tsq, self.s.top_k)
            fused = [m for m in fuse(rows, self.s, datetime.now(UTC)) if m.score >= self.s.min_score]
            picked = drop_near_duplicates(fused)[: limit or self.s.max_injected]
        if picked:
            t = asyncio.create_task(self.repo.touch([m.id for m in picked]))
            self._touch_tasks.add(t)
            t.add_done_callback(self._touch_tasks.discard)
        return RetrievalResult(picked, None, len(rows), (time.perf_counter() - t0) * 1000)
