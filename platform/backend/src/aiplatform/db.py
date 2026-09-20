"""asyncpg pool + a thin timed wrapper. asyncpg prepares and caches every statement per connection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from aiplatform.shared import timing


def _encode_vector(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _decode_vector(s: str) -> list[float]:
    return [float(x) for x in s.strip("[]").split(",")] if len(s) > 2 else []


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("vector", encoder=_encode_vector, decoder=_decode_vector, schema="public", format="text")


class Conn:
    """A connection (usually inside a transaction) with the same timed query API as Database."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.raw = conn

    async def fetch(self, q: str, *args: Any) -> list[asyncpg.Record]:
        with timing.stage("db"):
            return list(await self.raw.fetch(q, *args))

    async def fetchrow(self, q: str, *args: Any) -> asyncpg.Record | None:
        with timing.stage("db"):
            return await self.raw.fetchrow(q, *args)

    async def fetchval(self, q: str, *args: Any) -> Any:
        with timing.stage("db"):
            return await self.raw.fetchval(q, *args)

    async def execute(self, q: str, *args: Any) -> str:
        with timing.stage("db"):
            return str(await self.raw.execute(q, *args))

    async def executemany(self, q: str, args: list[tuple[Any, ...]]) -> None:
        with timing.stage("db"):
            await self.raw.executemany(q, args)


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        min_size: int = 2,
        max_size: int = 10,
        ef_search: int = 40,
    ) -> Database:
        pool = await asyncpg.create_pool(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            min_size=min_size,
            max_size=max_size,
            init=_init_connection,
            statement_cache_size=256,
            max_inactive_connection_lifetime=300,
            command_timeout=30,
            server_settings={
                "application_name": "aiplatform",
                "hnsw.ef_search": str(int(ef_search)),
                "hnsw.iterative_scan": "relaxed_order",
                # Prepared statements otherwise switch to a generic plan after 5 runs, which cannot use the HNSW
                # index for a parameterised ORDER BY <=> (measured: 3.8 ms seq scan vs 1.1 ms index scan).
                "plan_cache_mode": "force_custom_plan",
            },
        )
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def fetch(self, q: str, *args: Any) -> list[asyncpg.Record]:
        with timing.stage("db"):
            return list(await self.pool.fetch(q, *args))

    async def fetchrow(self, q: str, *args: Any) -> asyncpg.Record | None:
        with timing.stage("db"):
            return await self.pool.fetchrow(q, *args)

    async def fetchval(self, q: str, *args: Any) -> Any:
        with timing.stage("db"):
            return await self.pool.fetchval(q, *args)

    async def execute(self, q: str, *args: Any) -> str:
        with timing.stage("db"):
            return str(await self.pool.execute(q, *args))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Conn]:
        async with self.pool.acquire() as c, c.transaction():
            yield Conn(c)
