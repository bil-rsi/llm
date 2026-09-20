"""Platform benchmark. Runs INSIDE the backend container against the live stack:

    docker compose exec -T backend python - < bench/bench.py            (from C:\\llm-platform\\platform)
    scripts\\platform-bench.ps1 also adds host-side HTTP timings through the published port.

Measures each stage separately (never claims end-to-end LLM latency is ~ms). Writes JSON to stdout.
Env: BENCH_N (iterations, default 1000), BENCH_MEMORIES (seeded memories, default 1000), BENCH_LLM (runs, default 5).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from aiplatform.config import Environment, load_settings
from aiplatform.db import Database
from aiplatform.memory.context_builder import ContextBuilder
from aiplatform.memory.repository import LongTermMemoryRepository
from aiplatform.memory.retrieval import MemoryRetriever, to_tsquery
from aiplatform.model.embedding import LlamaCppEmbeddingProvider
from aiplatform.model.factory import create_provider
from aiplatform.model.types import ChatMessage, ChatRequest
from aiplatform.permissions.models import Root
from aiplatform.security.auth import AuthService, Principal
from aiplatform.shared.text import TokenEstimator, content_hash
from aiplatform.tools.base import TaintState, ToolContext
from aiplatform.tools.filesystem.tools import FsContext, filesystem_tools

N = int(os.environ.get("BENCH_N", "1000"))
N_MEM = int(os.environ.get("BENCH_MEMORIES", "1000"))
N_LLM = int(os.environ.get("BENCH_LLM", "5"))
TOPICS = ["Laravel", "PostgreSQL", "Docker", "Python", "React", "Kubernetes", "Redis", "billing", "invoices", "deploy",
          "tests", "security", "Windows", "backups", "Ollama", "embeddings", "memory", "API", "frontend", "migrations"]


def stats(xs: list[float]) -> dict[str, float]:
    s = sorted(xs)
    n = len(s)
    return {"n": n, "p50": round(s[n // 2], 3), "p95": round(s[min(n - 1, int(n * 0.95))], 3),
            "p99": round(s[min(n - 1, int(n * 0.99))], 3), "mean": round(statistics.fmean(s), 3), "min": round(s[0], 3)}


async def timeit(fn: Callable[[], Awaitable[Any]], n: int, warmup: int = 20) -> dict[str, float]:
    for _ in range(warmup):
        await fn()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - t0) * 1000)
    return stats(out)


async def main() -> None:
    s = load_settings()
    env = Environment.from_env()
    db = await Database.connect(host=env.db_host, port=env.db_port, database=env.db_name, user=env.db_user,
                                password=env.secret("pg_app"), ef_search=s.memory.retrieval.ef_search)
    emb = LlamaCppEmbeddingProvider(s.embedding.base_url, s.embedding.model, s.embedding.dims,
                                    query_instruction=s.embedding.query_instruction, cache_size=4096)
    res: dict[str, Any] = {"n": N, "memories": N_MEM}
    uid = await db.fetchval("INSERT INTO app.users (username, password_hash, must_change_password) VALUES ($1,'x',false) "
                            "RETURNING id", "bench_" + uuid.uuid4().hex[:8])
    try:
        # ── seed memories with real embeddings ──
        # Varied sentences: a corpus of near-identical ones makes every full-text query match every row, which
        # exaggerates the keyword-search time (measured 5.5 ms vs ~1 ms on varied text).
        FORMS = [
            "The user prefers {a} over {b} for {c} work.",
            "The {a} project lives in projects/{c} and is deployed with {b}.",
            "Decision {i}: use {a} for {c}; revisit when {b} changes.",
            "The user's {a} credentials rotate every {i} days (handled outside the platform).",
            "{a} notes for {c}: keep {b} pinned, run migrations before deploying.",
            "When working on {c}, the user wants short answers and {a} examples.",
            "The nightly {a} job writes reports to projects/{c}/reports.",
            "The user dislikes {b} in {c} code reviews.",
        ]
        texts = [
            FORMS[i % len(FORMS)].format(a=TOPICS[i % 20], b=TOPICS[(i * 7) % 20], c=TOPICS[(i * 13) % 20].lower(), i=i)
            + f" (note {i})"
            for i in range(N_MEM)
        ]
        t0 = time.perf_counter()
        vecs: list[list[float]] = []
        for i in range(0, N_MEM, 32):
            vecs += await emb.embed(texts[i:i + 32])
        res["embed_seed_ms_per_doc"] = round((time.perf_counter() - t0) * 1000 / N_MEM, 2)
        async with db.transaction() as tx:
            for t, v in zip(texts, vecs, strict=True):
                mid = await tx.fetchval(
                    "INSERT INTO app.long_term_memories (user_id, kind, content, content_hash, importance, confidence, status, "
                    "source_type) VALUES ($1,'fact',$2,$3,0.5,0.9,'active','manual') RETURNING id", uid, t, content_hash(t))
                await tx.execute("INSERT INTO app.memory_embeddings (memory_id, model, dims, embedding) VALUES ($1,$2,1024,$3)",
                                 mid, s.embedding.model, v)
        await db.execute("ANALYZE app.long_term_memories; ANALYZE app.memory_embeddings")
        repo = LongTermMemoryRepository(db, s.embedding.model)
        retr = MemoryRetriever(repo, emb, s.memory.retrieval)
        query = "Which option do I prefer for PostgreSQL migrations work?"
        qvec = await emb.embed_query(query)
        tsq = to_tsquery(query)

        # ── database ──
        res["db_select_1"] = await timeit(lambda: db.fetchval("SELECT 1"), N)
        cid = await db.fetchval("INSERT INTO app.conversations (user_id) VALUES ($1) RETURNING id", uid)
        for i in range(40):
            await db.execute("INSERT INTO app.messages (conversation_id, seq, role, content) VALUES ($1,$2,'user',$3)", cid, i + 1, "m" * 300)
        res["db_recent_messages_20"] = await timeit(lambda: db.fetch(
            "SELECT seq, role, content FROM app.messages WHERE conversation_id=$1 ORDER BY seq DESC LIMIT 20", cid), N)
        res["db_insert_message"] = await timeit(lambda: db.fetchval(
            "INSERT INTO app.short_term_memories (conversation_id, kind, key, content, expires_at) VALUES ($1,'note',$2,'x', now()+interval '1h') "
            "RETURNING id", cid, uuid.uuid4().hex), N)
        res["audit_append"] = await timeit(lambda: db.execute(
            "INSERT INTO app.audit_log (actor_type, action, outcome) VALUES ('system','bench','info')"), min(N, 300))

        # ── memory retrieval ──
        res["vector_search_hnsw_top8"] = await timeit(lambda: repo.hybrid(uid, qvec, "", 8), N)
        res["keyword_search_fts_top8"] = await timeit(lambda: repo.keyword(uid, tsq, 8), N)
        res["hybrid_sql_one_roundtrip"] = await timeit(lambda: repo.hybrid(uid, qvec, tsq, 8), N)
        words = iter(f"{TOPICS[i % 20].lower()} {TOPICS[(i * 3) % 20].lower()} item {i}" for i in range(10_000))
        # uncached but realistic: ordinary words (a random hex suffix would add dozens of tokens and distort the number)
        res["embed_query_uncached"] = await timeit(lambda: emb.embed_query(f"{query} {next(words)}"), 100, warmup=3)
        res["embed_query_cached"] = await timeit(lambda: emb.embed_query(query), N)
        res["memory_retrieval_total_cached_embedding"] = await timeit(lambda: retr.retrieve(uid, query), N)
        async with db.transaction() as tx:  # same setting app.nearest_memories() pins (migration 0002)
            for guc in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):  # same as app.nearest_memories()
                await tx.execute(f"SET LOCAL {guc} = off")
            plan = await tx.fetch(
                "EXPLAIN (ANALYZE, FORMAT TEXT) SELECT m.id FROM app.memory_embeddings e JOIN app.long_term_memories m "
                "ON m.id=e.memory_id WHERE e.model=$1 AND m.user_id=$2 AND m.status='active' ORDER BY e.embedding <=> $3 LIMIT 8",
                s.embedding.model, uid, qvec)
        res["vector_plan_uses_hnsw"] = any("memory_embeddings_hnsw" in r[0] for r in plan)

        # ── context construction ──
        builder = ContextBuilder(s.context, TokenEstimator())
        mems = (await retr.retrieve(uid, query, force=True)).memories
        hist = [ChatMessage("user" if i % 2 == 0 else "assistant", "text " * 120) for i in range(40)]

        async def build() -> None:
            builder.build(system_prompt="system " * 80, user_system=None, history=hist, current=ChatMessage("user", query),
                          memories=mems, short_term=[], summary="", tools=[], num_ctx=s.provider.num_ctx)
        res["context_build_40_turns"] = await timeit(build, N)

        # ── tool execution (filesystem, direct; excludes approval waiting) ──
        tmp = Path(tempfile.mkdtemp(dir="/tmp"))
        (tmp / "sandbox").mkdir()
        for i in range(50):
            (tmp / "sandbox" / f"f{i}.txt").write_text("hello " * 200)
        roots = (Root("sandbox", "sandbox", str(tmp / "sandbox"), "rw", "zone"),)
        from aiplatform.permissions.models import BypassCapabilities, PermissionSnapshot
        snap = PermissionSnapshot(1, "bypass", "restricted", False, False, BypassCapabilities(), {}, (), roots, ())
        ctx = ToolContext(uid, None, None, snap, TaintState())
        tools = {t.name: t for t in filesystem_tools(FsContext(s.filesystem, "C:/AIWorkspace"))}

        async def fs_read() -> None:
            t = tools["filesystem.read"]
            await t.run(t.prepare(t.Args.model_validate({"path": "sandbox/f1.txt"}), ctx), ctx)

        async def fs_list() -> None:
            t = tools["filesystem.list"]
            await t.run(t.prepare(t.Args.model_validate({"path": "sandbox"}), ctx), ctx)
        res["tool_filesystem_read_tmpfs"] = await timeit(fs_read, N)
        res["tool_filesystem_list_50_tmpfs"] = await timeit(fs_list, N)
        ws = Path("/workspace/sandbox")
        if ws.is_dir():
            (ws / "_bench.txt").write_text("hello " * 200)
            roots_ws = (Root("sandbox", "sandbox", str(ws), "rw", "zone"),)
            ctx_ws = ToolContext(uid, None, None, PermissionSnapshot(1, "bypass", "restricted", False, False, BypassCapabilities(),
                                                                     {}, (), roots_ws, ()), TaintState())

            async def fs_read_ws() -> None:
                t = tools["filesystem.read"]
                await t.run(t.prepare(t.Args.model_validate({"path": "sandbox/_bench.txt"}), ctx_ws), ctx_ws)
            res["tool_filesystem_read_windows_bind_mount"] = await timeit(fs_read_ws, min(N, 300))
            (ws / "_bench.txt").unlink()

        # ── HTTP API overhead inside the container (loopback, no model) ──
        auth = AuthService(db, __import__("aiplatform.audit.service", fromlist=["AuditLog"]).AuditLog(db),
                           session_hours=1, elevation_minutes=1, login_per_minute=100)
        token = await auth.create_token(Principal(uid, "bench", frozenset({"admin"}), "session"), "bench", ["read"], 1)
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", headers={"Host": "127.0.0.1:8090"}) as http:
            res["http_health_live"] = await timeit(lambda: http.get("/health/live"), N)
            res["http_api_memories_list_10"] = await timeit(
                lambda: http.get("/api/memories", params={"limit": 10}, headers={"Authorization": f"Bearer {token}"}), N)
            r = await http.get("/health/live")
            res["server_timing_header_example"] = r.headers.get("server-timing")

        # ── model (real inference; seconds, not milliseconds) ──
        provider = create_provider(s.provider, env)
        health = await provider.health()
        res["model_health"] = health.__dict__
        if health.ok and N_LLM > 0:
            runs = []
            for i in range(N_LLM):
                req = ChatRequest(messages=[ChatMessage("user", f"In one short sentence: what is a vector index? (run {i})")],
                                  max_tokens=64, num_ctx=s.provider.num_ctx, temperature=0.7, think=False)
                t0 = time.perf_counter()
                ttft = None
                usage = None
                async for ch in provider.chat(req):
                    if ttft is None and (ch.content or ch.reasoning):
                        ttft = (time.perf_counter() - t0) * 1000
                    if ch.done:
                        usage = ch.usage
                total = (time.perf_counter() - t0) * 1000
                runs.append({"ttft_ms": round(ttft or total, 1), "total_ms": round(total, 1),
                             "prompt_tokens": usage.prompt_tokens if usage else None,
                             "completion_tokens": usage.completion_tokens if usage else None,
                             "tokens_per_s": round(usage.completion_tokens / (usage.generation_ms / 1000), 2)
                             if usage and usage.completion_tokens and usage.generation_ms else None,
                             "prompt_tokens_per_s": round(usage.prompt_tokens / (usage.prompt_ms / 1000), 1)
                             if usage and usage.prompt_tokens and usage.prompt_ms else None,
                             "load_ms": round(usage.load_ms, 1) if usage and usage.load_ms else None})
            res["model_runs"] = runs
            warm = runs[1:] or runs
            res["model_summary_warm"] = {"ttft": stats([r["ttft_ms"] for r in warm]), "total": stats([r["total_ms"] for r in warm]),
                                         "tokens_per_s": stats([r["tokens_per_s"] or 0 for r in warm])}
        await provider.aclose()
    finally:
        await db.execute("DELETE FROM app.users WHERE id=$1", uid)
        await emb.aclose()
        await db.close()
    json.dump(res, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    asyncio.run(main())
