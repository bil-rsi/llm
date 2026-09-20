"""Repositories for long-term memories (+ embeddings) and short-term memories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from aiplatform.db import Conn, Database
from aiplatform.memory.models import MemoryItem, Score, ShortTermItem
from aiplatform.shared.text import content_hash

# Hybrid candidate query: vector top-k (HNSW via app.nearest_memories, iterative scan) and keyword
# top-k (GIN) in ONE round trip. Fusion/weighting happens in Python where it is cheap and testable.
HYBRID_SQL = """
WITH v AS (
  SELECT id, dist FROM app.nearest_memories($1, $2, $3, $4)   -- HNSW pinned (migration 0002)
), vr AS (SELECT id, dist, row_number() OVER (ORDER BY dist) AS r FROM v),
k AS (
  SELECT id, ts_rank_cd(tsv, q) AS rank
  FROM app.long_term_memories, to_tsquery('simple', $5) q
  WHERE $5 <> '' AND user_id = $3 AND status = 'active' AND tsv @@ q AND (expires_at IS NULL OR expires_at > now())
  ORDER BY rank DESC LIMIT $4
), kr AS (SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM k
), ids AS (SELECT id FROM vr UNION SELECT id FROM kr)
SELECT m.id, m.kind, m.content, m.importance, m.confidence, m.source_type, m.created_at, m.updated_at, m.flags,
       vr.r AS vector_rank, 1 - vr.dist AS cosine, kr.r AS keyword_rank
FROM ids JOIN app.long_term_memories m ON m.id = ids.id      -- drive from <= 2k candidates, not the whole table
LEFT JOIN vr ON vr.id = ids.id LEFT JOIN kr ON kr.id = ids.id
"""

KEYWORD_SQL = """
SELECT m.id, m.kind, m.content, m.importance, m.confidence, m.source_type, m.created_at, m.updated_at, m.flags,
       NULL::bigint AS vector_rank, NULL::float8 AS cosine,
       row_number() OVER (ORDER BY ts_rank_cd(m.tsv, q) DESC) AS keyword_rank
FROM app.long_term_memories m, to_tsquery('simple', $2) q
WHERE $2 <> '' AND m.user_id = $1 AND m.status = 'active' AND m.tsv @@ q AND (m.expires_at IS NULL OR m.expires_at > now())
ORDER BY ts_rank_cd(m.tsv, q) DESC LIMIT $3
"""


def _row_to_item(r: asyncpg.Record) -> MemoryItem:
    return MemoryItem(
        id=r["id"],
        user_id=r["user_id"],
        kind=r["kind"],
        content=r["content"],
        importance=Score(float(r["importance"])),
        confidence=Score(float(r["confidence"])),
        sensitivity=r["sensitivity"],
        status=r["status"],
        source_type=r["source_type"],
        source_conversation_id=r["source_conversation_id"],
        source_message_id=r["source_message_id"],
        source_ref=r["source_ref"],
        supersedes_id=r["supersedes_id"],
        flags=list(r["flags"]),
        expires_at=r["expires_at"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        access_count=r["access_count"],
    )


class LongTermMemoryRepository:
    def __init__(self, db: Database, embedding_model: str) -> None:
        self.db = db
        self.embedding_model = embedding_model

    async def get(self, memory_id: UUID, user_id: UUID) -> MemoryItem | None:
        r = await self.db.fetchrow("SELECT * FROM app.long_term_memories WHERE id=$1 AND user_id=$2", memory_id, user_id)
        return _row_to_item(r) if r else None

    async def find_live_by_hash(self, user_id: UUID, h: bytes) -> MemoryItem | None:
        r = await self.db.fetchrow(
            "SELECT * FROM app.long_term_memories WHERE user_id=$1 AND content_hash=$2 AND status IN ('candidate','active')",
            user_id,
            h,
        )
        return _row_to_item(r) if r else None

    async def nearest_live(self, user_id: UUID, vec: list[float], limit: int = 3) -> list[tuple[MemoryItem, float]]:
        rows = await self.db.fetch(
            "SELECT m.*, 1 - (e.embedding <=> $1) AS cosine FROM app.memory_embeddings e "
            "JOIN app.long_term_memories m ON m.id = e.memory_id WHERE e.model=$2 AND m.user_id=$3 "
            "AND m.status IN ('candidate','active') ORDER BY e.embedding <=> $1 LIMIT $4",
            vec,
            self.embedding_model,
            user_id,
            limit,
        )
        return [(_row_to_item(r), float(r["cosine"])) for r in rows]

    async def insert(self, m: MemoryItem, vec: list[float] | None, conn: Conn) -> UUID:
        mid: UUID = await conn.fetchval(
            "INSERT INTO app.long_term_memories (user_id, kind, content, content_hash, importance, confidence, sensitivity, "
            "status, source_type, source_conversation_id, source_message_id, source_ref, supersedes_id, flags, expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id",
            m.user_id,
            m.kind,
            m.content,
            content_hash(m.content),
            m.importance.value,
            m.confidence.value,
            m.sensitivity,
            m.status,
            m.source_type,
            m.source_conversation_id,
            m.source_message_id,
            m.source_ref,
            m.supersedes_id,
            m.flags,
            m.expires_at,
        )
        if vec is not None:
            await conn.execute(
                "INSERT INTO app.memory_embeddings (memory_id, model, dims, embedding) VALUES ($1,$2,$3,$4)",
                mid,
                self.embedding_model,
                len(vec),
                vec,
            )
        return mid

    async def save_state(self, m: MemoryItem, conn: Conn | None = None) -> None:
        q = (
            "UPDATE app.long_term_memories SET kind=$2, content=$3, content_hash=$4, importance=$5, confidence=$6, "
            "status=$7, flags=$8, expires_at=$9, sensitivity=$10 WHERE id=$1"
        )
        args = (
            m.id,
            m.kind,
            m.content,
            content_hash(m.content),
            m.importance.value,
            m.confidence.value,
            m.status,
            m.flags,
            m.expires_at,
            m.sensitivity,
        )
        await (conn.execute(q, *args) if conn else self.db.execute(q, *args))
        if m.status not in ("active", "candidate"):
            dq = "DELETE FROM app.memory_embeddings WHERE memory_id=$1"
            await (conn.execute(dq, m.id) if conn else self.db.execute(dq, m.id))

    async def replace_embedding(self, memory_id: UUID, vec: list[float], conn: Conn | None = None) -> None:
        q = (
            "INSERT INTO app.memory_embeddings (memory_id, model, dims, embedding) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (memory_id, model) DO UPDATE SET embedding=EXCLUDED.embedding, created_at=now()"
        )
        await (
            conn.execute(q, memory_id, self.embedding_model, len(vec), vec)
            if conn
            else self.db.execute(q, memory_id, self.embedding_model, len(vec), vec)
        )

    async def hybrid(self, user_id: UUID, vec: list[float], tsquery: str, top_k: int) -> list[asyncpg.Record]:
        # hnsw.ef_search / hnsw.iterative_scan are set once per pooled connection (Database.connect), so this is a
        # single round trip.
        return await self.db.fetch(HYBRID_SQL, vec, self.embedding_model, user_id, top_k, tsquery)

    async def keyword(self, user_id: UUID, tsquery: str, top_k: int) -> list[asyncpg.Record]:
        return await self.db.fetch(KEYWORD_SQL, user_id, tsquery, top_k)

    async def touch(self, ids: list[UUID]) -> None:
        if ids:
            await self.db.execute(
                "UPDATE app.long_term_memories SET last_accessed_at=now(), access_count=access_count+1 "
                "WHERE id = ANY($1::uuid[])",
                ids,
            )

    async def count_active(self, user_id: UUID) -> int:
        return int(
            await self.db.fetchval("SELECT count(*) FROM app.long_term_memories WHERE user_id=$1 AND status='active'", user_id)
        )

    async def list_items(
        self, user_id: UUID, *, status: str | None, kind: str | None, q: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT id, kind, content, importance, confidence, sensitivity, status, source_type, source_conversation_id, "
            "source_ref, supersedes_id, flags, expires_at, last_accessed_at, access_count, created_at, updated_at, "
            "EXISTS (SELECT 1 FROM app.memory_embeddings e WHERE e.memory_id = m.id) AS embedded "
            "FROM app.long_term_memories m WHERE user_id=$1 AND ($2::text IS NULL OR status=$2) "
            "AND ($3::text IS NULL OR kind=$3) AND ($4::text IS NULL OR content ILIKE '%' || $4 || '%') "
            "ORDER BY updated_at DESC LIMIT $5 OFFSET $6",
            user_id,
            status,
            kind,
            q,
            limit,
            offset,
        )
        return [dict(r) for r in rows]

    async def stats(self, user_id: UUID) -> dict[str, int]:
        rows = await self.db.fetch(
            "SELECT status, count(*) AS n FROM app.long_term_memories WHERE user_id=$1 GROUP BY status", user_id
        )
        return {r["status"]: r["n"] for r in rows}

    async def expire_due(self) -> int:
        r = await self.db.execute(
            "UPDATE app.long_term_memories SET status='expired' WHERE status IN ('active','candidate') "
            "AND expires_at IS NOT NULL AND expires_at <= now()"
        )
        await self.db.execute(
            "DELETE FROM app.memory_embeddings e USING app.long_term_memories m WHERE m.id=e.memory_id "
            "AND m.status NOT IN ('active','candidate')"
        )
        return int(r.split()[-1])

    async def purge_deleted(self, older_than_days: int = 30) -> int:
        r = await self.db.execute(
            "DELETE FROM app.long_term_memories WHERE status IN ('deleted','rejected') "
            "AND updated_at < now() - make_interval(days => $1)",
            older_than_days,
        )
        return int(r.split()[-1])

    async def missing_embeddings(self, limit: int = 64) -> list[tuple[UUID, str]]:
        rows = await self.db.fetch(
            "SELECT m.id, m.content FROM app.long_term_memories m WHERE m.status IN ('active','candidate') AND NOT EXISTS "
            "(SELECT 1 FROM app.memory_embeddings e WHERE e.memory_id=m.id AND e.model=$1) LIMIT $2",
            self.embedding_model,
            limit,
        )
        return [(r["id"], r["content"]) for r in rows]


class ShortTermMemoryRepository:
    def __init__(self, db: Database, ttl_minutes: int) -> None:
        self.db = db
        self.ttl = timedelta(minutes=ttl_minutes)

    async def upsert(
        self,
        conversation_id: UUID,
        kind: str,
        key: str,
        content: str,
        *,
        importance: float = 0.5,
        data: dict[str, Any] | None = None,
        ttl: timedelta | None = None,
    ) -> None:
        expires = datetime.now(UTC) + (ttl or self.ttl)
        await self.db.execute(
            "INSERT INTO app.short_term_memories (conversation_id, kind, key, content, data, importance, token_count, "
            "expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (conversation_id, kind, key) DO UPDATE SET "
            "content=EXCLUDED.content, data=EXCLUDED.data, importance=EXCLUDED.importance, "
            "token_count=EXCLUDED.token_count, expires_at=EXCLUDED.expires_at",
            conversation_id,
            kind,
            key[:200],
            content[:8000],
            data,
            importance,
            len(content) // 4,
            expires,
        )

    async def active(self, conversation_id: UUID, limit: int = 20) -> list[ShortTermItem]:
        rows = await self.db.fetch(
            "SELECT kind, key, content, importance, expires_at, updated_at FROM app.short_term_memories "
            "WHERE conversation_id=$1 AND expires_at > now() ORDER BY importance DESC, updated_at DESC LIMIT $2",
            conversation_id,
            limit,
        )
        return [
            ShortTermItem(r["kind"], r["key"], r["content"], float(r["importance"]), r["expires_at"], r["updated_at"])
            for r in rows
        ]

    async def list_for(self, conversation_id: UUID) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetch(
                "SELECT id, kind, key, content, importance, expires_at, updated_at FROM app.short_term_memories "
                "WHERE conversation_id=$1 ORDER BY updated_at DESC",
                conversation_id,
            )
        ]

    async def delete(self, conversation_id: UUID, kind: str, key: str) -> None:
        await self.db.execute(
            "DELETE FROM app.short_term_memories WHERE conversation_id=$1 AND kind=$2 AND key=$3", conversation_id, kind, key
        )

    async def sweep(self) -> int:
        r = await self.db.execute("DELETE FROM app.short_term_memories WHERE expires_at <= now()")
        return int(r.split()[-1])
