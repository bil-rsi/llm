"""Forward-only SQL migration runner. Runs as aimem_owner (the only role with DDL) in the one-shot `migrate` service.

Each file in AIP_MIGRATIONS_DIR (NNNN_name.sql) runs once, in its own transaction, and is recorded with its SHA-256.
Changing an applied file aborts: write a new migration instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from aiplatform.config import Environment

LOCK_KEY = 7373002


async def migrate(conn: asyncpg.Connection, directory: Path) -> list[str]:
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS app.schema_migrations (version text PRIMARY KEY, "
            "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r["version"]: r["checksum"] for r in await conn.fetch("SELECT version, checksum FROM app.schema_migrations")}
        done: list[str] = []
        for f in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):  # noqa: ASYNC240 - one-shot startup job
            sql = f.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if f.name in applied:
                if applied[f.name] != digest:
                    raise RuntimeError(
                        f"applied migration {f.name} was modified (checksum mismatch); add a new migration instead"
                    )
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO app.schema_migrations (version, checksum) VALUES ($1, $2)", f.name, digest)
            done.append(f.name)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)


async def _main() -> int:
    env = Environment.from_env()
    conn = await asyncpg.connect(
        host=env.db_host, port=env.db_port, database=env.db_name, user="aimem_owner", password=env.secret("pg_owner")
    )
    try:
        done = await migrate(conn, env.migrations_dir)
    finally:
        await conn.close()
    print(f"migrations applied: {', '.join(done) if done else 'none (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
