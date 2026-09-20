"""Dashboard aggregates: requests, latency percentiles, tokens, memory, tools, blocked operations, DB and model stats."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aiplatform.db import Database
from aiplatform.shared.timing import RECORDER


class DashboardService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def summary(self, user_id: UUID, hours: int) -> dict[str, Any]:
        model = await self.db.fetchrow(
            "SELECT count(*) AS responses, count(*) FILTER (WHERE r.status <> 'ok') AS errors, "
            "sum(r.prompt_tokens) AS prompt_tokens, sum(r.completion_tokens) AS completion_tokens, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ttft_ms) AS ttft_p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY r.ttft_ms) AS ttft_p95, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY r.total_ms) AS total_p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY r.total_ms) AS total_p95, avg(r.tokens_per_s) AS tokens_per_s "
            "FROM app.model_responses r WHERE r.created_at > now() - make_interval(hours => $1)",
            hours,
        )
        tools = await self.db.fetch(
            "SELECT tool_name, count(*) AS calls, count(*) FILTER (WHERE status='succeeded') AS ok, "
            "count(*) FILTER (WHERE status='failed' OR status='timed_out') AS failed, "
            "count(*) FILTER (WHERE status='denied') AS blocked, count(*) FILTER (WHERE status='expired') AS expired, "
            "round(avg(duration_ms)::numeric, 1) AS avg_ms FROM app.tool_executions "
            "WHERE requested_at > now() - make_interval(hours => $1) GROUP BY tool_name ORDER BY calls DESC",
            hours,
        )
        blocked = await self.db.fetch(
            "SELECT tool_name, policy_rule, decision_reason, requested_at FROM app.tool_executions WHERE status='denied' "
            "AND requested_at > now() - make_interval(hours => $1) ORDER BY requested_at DESC LIMIT 20",
            hours,
        )
        mem = await self.db.fetch(
            "SELECT status, count(*) AS n FROM app.long_term_memories WHERE user_id=$1 GROUP BY status", user_id
        )
        stm = await self.db.fetchval("SELECT count(*) FROM app.short_term_memories WHERE expires_at > now()")
        convs = await self.db.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE updated_at > now() - make_interval(hours => $2)) AS active "
            "FROM app.conversations WHERE user_id=$1",
            user_id,
            hours,
        )
        dbstats = await self.db.fetchrow(
            "SELECT pg_database_size(current_database()) AS bytes, "
            "(SELECT sum(xact_commit) FROM pg_stat_database WHERE datname=current_database()) AS commits, "
            "(SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0), 2) FROM pg_stat_database "
            " WHERE datname=current_database()) AS cache_hit_pct"
        )
        return {
            "window_hours": hours,
            "model": dict(model) if model else {},
            "tools": [dict(r) for r in tools],
            "blocked_recent": [dict(r) for r in blocked],
            "memory": {"long_term": {r["status"]: r["n"] for r in mem}, "short_term_active": stm},
            "conversations": dict(convs) if convs else {},
            "database": dict(dbstats) if dbstats else {},
            "stages_ms": RECORDER.summary(),
        }
