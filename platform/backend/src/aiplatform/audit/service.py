"""Append-only, hash-chained audit log. The chain (seq, prev_hash, hash) is computed by a database trigger."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from aiplatform.db import Conn, Database
from aiplatform.observability import context
from aiplatform.security.sensitivity import redact

ActorType = Literal["user", "model", "system"]
Outcome = Literal["success", "failure", "denied", "pending", "info"]


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[:4000]
    if isinstance(value, dict):
        return {str(k)[:100]: _clean(v) for k, v in list(value.items())[:100]}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in list(value)[:100]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:1000]


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self,
        action: str,
        *,
        actor_type: ActorType,
        actor_id: str | None = None,
        target: str | None = None,
        outcome: Outcome = "info",
        detail: dict[str, Any] | None = None,
        conn: Conn | None = None,
    ) -> None:
        q = (
            "INSERT INTO app.audit_log (actor_type, actor_id, action, target, outcome, request_id, correlation_id, detail) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)"
        )
        args = (
            actor_type,
            actor_id,
            action[:100],
            (redact(target)[:1000] if target else None),
            outcome,
            context.request_id.get(),
            context.correlation_id.get(),
            _clean(detail or {}),
        )
        if conn is not None:
            await conn.execute(q, *args)
        else:
            await self.db.execute(q, *args)

    async def query(
        self,
        *,
        action: str | None = None,
        actor_type: str | None = None,
        outcome: str | None = None,
        before_seq: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT seq, at, actor_type, actor_id, action, target, outcome, request_id, correlation_id, detail, "
            "encode(hash, 'hex') AS hash FROM app.audit_log WHERE ($1::text IS NULL OR action LIKE $1 || '%') "
            "AND ($2::text IS NULL OR actor_type = $2) AND ($3::text IS NULL OR outcome = $3) "
            "AND ($4::bigint IS NULL OR seq < $4) ORDER BY seq DESC LIMIT $5",
            action,
            actor_type,
            outcome,
            before_seq,
            max(1, min(limit, 500)),
        )
        return [dict(r) for r in rows]

    async def verify(self) -> dict[str, Any]:
        broken = await self.db.fetchval("SELECT app.audit_verify()")
        total = await self.db.fetchval("SELECT count(*) FROM app.audit_log")
        last: datetime | None = await self.db.fetchval("SELECT max(at) FROM app.audit_log")
        return {"intact": broken is None, "first_broken_seq": broken, "entries": total, "last_at": last}
