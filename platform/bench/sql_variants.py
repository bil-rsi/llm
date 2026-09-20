"""Diagnostic: time retrieval SQL variants on real embeddings (runs inside the backend container; cleans up after itself).

    docker compose exec -T backend python - < bench/sql_variants.py < new_hybrid.sql   (see usage in PERFORMANCE.md)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from aiplatform.config import Environment, load_settings
from aiplatform.db import Database
from aiplatform.memory.retrieval import to_tsquery
from aiplatform.model.embedding import LlamaCppEmbeddingProvider
from aiplatform.shared.text import content_hash

TOPICS = ["Laravel", "PostgreSQL", "Docker", "Python", "React", "Kubernetes", "Redis", "billing", "invoices", "deploy", "tests",
          "security", "Windows", "backups", "Ollama", "embeddings", "memory", "API", "frontend", "migrations"]
VARIANTS = {name: sql for name, sql in (line.split("|", 1) for line in os.environ.get("SQL_VARIANTS", "").split("\n\n") if "|" in line)}


async def timed(db: Database, sql: str, *args: object, n: int = 300) -> float:
    for _ in range(20):
        await db.fetch(sql, *args)
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        await db.fetch(sql, *args)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return round(ts[len(ts) // 2], 3)


async def main() -> None:
    s = load_settings()
    env = Environment.from_env()
    db = await Database.connect(host=env.db_host, port=env.db_port, database=env.db_name, user=env.db_user,
                                password=env.secret("pg_app"), ef_search=s.memory.retrieval.ef_search)
    emb = LlamaCppEmbeddingProvider(s.embedding.base_url, s.embedding.model, s.embedding.dims,
                                    query_instruction=s.embedding.query_instruction)
    uid = await db.fetchval("INSERT INTO app.users (username,password_hash,must_change_password) VALUES ($1,'x',false) RETURNING id",
                            "sqlv_" + uuid.uuid4().hex[:6])
    try:
        texts = [f"The user's {TOPICS[i % 20]} note {i}: prefers option {i % 7} for {TOPICS[(i * 7) % 20]} work." for i in range(1000)]
        vecs: list[list[float]] = []
        for i in range(0, 1000, 50):
            vecs += await emb.embed(texts[i:i + 50])
        async with db.transaction() as tx:
            for t, v in zip(texts, vecs, strict=True):
                mid = await tx.fetchval(
                    "INSERT INTO app.long_term_memories (user_id,kind,content,content_hash,importance,confidence,status,source_type) "
                    "VALUES ($1,'fact',$2,$3,0.5,0.9,'active','manual') RETURNING id", uid, t, content_hash(t))
                await tx.execute("INSERT INTO app.memory_embeddings (memory_id,model,dims,embedding) VALUES ($1,$2,1024,$3)",
                                 mid, s.embedding.model, v)
        await db.execute("ANALYZE app.memory_embeddings; ANALYZE app.long_term_memories")
        query = "Which option do I prefer for PostgreSQL migrations work?"
        q = await emb.embed_query(query)
        tsq = to_tsquery(query)
        print("function_only", await timed(db, "SELECT * FROM app.nearest_memories($1,$2,$3,8)", q, s.embedding.model, uid))
        for name, sql in VARIANTS.items():
            print(name, "vector-only", await timed(db, sql, q, s.embedding.model, uid, 8, ""),
                  "hybrid", await timed(db, sql, q, s.embedding.model, uid, 8, tsq))
        if os.environ.get("KEEP"):
            print("KEEP_USER", uid, "QUERY_VEC", "[" + ",".join(f"{x:.7g}" for x in q) + "]")
    finally:
        if not os.environ.get("KEEP"):
            await db.execute("DELETE FROM app.users WHERE id=$1", uid)
        await emb.aclose()
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
