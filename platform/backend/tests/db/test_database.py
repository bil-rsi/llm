"""Database tests: least-privilege grants, append-only hash-chained audit log, SQL injection resistance."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from aiplatform.audit.service import AuditLog
from aiplatform.db import Database

pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE app.evil (x int)",
        "DROP TABLE app.long_term_memories",
        "ALTER TABLE app.users ADD COLUMN x int",
        "TRUNCATE app.messages",
        "CREATE EXTENSION IF NOT EXISTS plpython3u",
        "CREATE ROLE evil",
        "COPY app.users TO PROGRAM 'id'",
        "UPDATE app.audit_log SET outcome='success'",
        "DELETE FROM app.audit_log",
        "CREATE SCHEMA evil",
    ],
)
async def test_app_role_cannot_do_ddl_or_tamper(db: Database, sql: str) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await db.execute(sql)


async def test_audit_chain_and_immutability(db: Database, owner_conn: asyncpg.Connection) -> None:
    audit = AuditLog(db)
    for i in range(5):
        await audit.append(f"test.event{i}", actor_type="system", target=f"t{i}", outcome="info", detail={"i": i})
    v = await audit.verify()
    assert v["intact"] and v["entries"] >= 5
    # even the owner role cannot rewrite history (trigger), so tampering needs superuser + trigger disable
    with pytest.raises(asyncpg.PostgresError):
        await owner_conn.execute("UPDATE app.audit_log SET target='x' WHERE seq = (SELECT max(seq) FROM app.audit_log)")


async def test_audit_redacts_secrets(db: Database) -> None:
    audit = AuditLog(db)
    await audit.append("test.secret", actor_type="system", detail={"note": "password=Sup3rS3cretValue"})
    rows = await audit.query(action="test.secret", limit=1)
    assert "Sup3rS3cretValue" not in str(rows[0]["detail"])


@pytest.mark.parametrize("evil", ["'; DROP TABLE app.long_term_memories; --", "1 OR 1=1", "\\x00", "%' OR '1'='1"])
async def test_sql_injection_strings_are_data(db: Database, user_id: uuid.UUID, evil: str) -> None:
    from aiplatform.memory.repository import LongTermMemoryRepository

    repo = LongTermMemoryRepository(db, "fake-embed")
    assert await repo.list_items(user_id, status=None, kind=evil, q=evil, limit=5, offset=0) == []
    assert await db.fetchval("SELECT count(*) FROM app.long_term_memories") is not None  # table still exists
