"""Approval broker: a confirm decision parks the tool call until you approve/deny it in the admin console."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from aiplatform.audit.service import AuditLog
from aiplatform.db import Database
from aiplatform.observability.metrics import Metrics
from aiplatform.shared.errors import Conflict, NotFound

Verdict = Literal["approved", "denied", "expired"]


class ApprovalBroker:
    def __init__(self, db: Database, audit: AuditLog, metrics: Metrics) -> None:
        self.db = db
        self.audit = audit
        self.metrics = metrics
        self._waiters: dict[UUID, asyncio.Future[Verdict]] = {}

    async def request(self, tool_execution_id: UUID, summary: str, timeout_s: float) -> UUID:
        expires = datetime.now(UTC) + timedelta(seconds=timeout_s)
        approval_id: UUID = await self.db.fetchval(
            "INSERT INTO app.approvals (tool_execution_id, summary, expires_at) VALUES ($1,$2,$3) RETURNING id",
            tool_execution_id,
            summary[:1000],
            expires,
        )
        self._waiters[approval_id] = asyncio.get_running_loop().create_future()
        self.metrics.approvals_pending.set(len(self._waiters))
        return approval_id

    async def wait(self, approval_id: UUID, timeout_s: float) -> Verdict:
        fut = self._waiters.get(approval_id)
        if fut is None:
            return "expired"
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
        except TimeoutError:
            await self.db.execute(
                "UPDATE app.approvals SET decision='expired', decided_at=now() WHERE id=$1 AND decision IS NULL", approval_id
            )
            return "expired"
        finally:
            self._waiters.pop(approval_id, None)
            self.metrics.approvals_pending.set(len(self._waiters))

    async def decide(self, approval_id: UUID, approve: bool, by_user: UUID, note: str = "") -> None:
        verdict: Verdict = "approved" if approve else "denied"
        row = await self.db.fetchrow(
            "UPDATE app.approvals SET decision=$2, decided_at=now(), decided_by=$3, note=$4 "
            "WHERE id=$1 AND decision IS NULL AND expires_at > now() RETURNING tool_execution_id",
            approval_id,
            verdict,
            by_user,
            note[:500],
        )
        if row is None:
            exists = await self.db.fetchval("SELECT decision FROM app.approvals WHERE id=$1", approval_id)
            if exists is None and not await self.db.fetchval("SELECT 1 FROM app.approvals WHERE id=$1", approval_id):
                raise NotFound("approval not found")
            raise Conflict("approval already decided or expired")
        await self.audit.append(
            "approval.decide",
            actor_type="user",
            actor_id=str(by_user),
            target=str(approval_id),
            outcome="success",
            detail={"decision": verdict, "tool_execution_id": str(row["tool_execution_id"])},
        )
        fut = self._waiters.get(approval_id)
        if fut is not None and not fut.done():
            fut.set_result(verdict)

    async def pending(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT a.id, a.summary, a.requested_at, a.expires_at, t.tool_name, t.arguments, t.mode, t.tainted, "
            "t.decision_reason, t.conversation_id FROM app.approvals a JOIN app.tool_executions t ON t.id = a.tool_execution_id "
            "WHERE a.decision IS NULL AND a.expires_at > now() ORDER BY a.requested_at"
        )
        return [dict(r) for r in rows]

    async def expire_stale(self) -> int:
        """Approvals left over from a previous process can never be answered: mark them expired."""
        live = list(self._waiters)
        r = await self.db.execute(
            "UPDATE app.approvals SET decision='expired', decided_at=now() WHERE decision IS NULL "
            "AND (expires_at <= now() OR NOT (id = ANY($1::uuid[])))",
            live,
        )
        return int(r.split()[-1]) if r.startswith("UPDATE") else 0
