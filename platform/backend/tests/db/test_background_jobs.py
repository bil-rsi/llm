"""Background model jobs: extraction after a turn, recording, pre-emption by a chat turn, and the summariser."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aiplatform.audit.service import AuditLog
from aiplatform.config import MemorySettings, Settings
from aiplatform.conversation.repository import ConversationRepository
from aiplatform.db import Database
from aiplatform.memory.extraction import BackgroundModelJobs, ModelGate
from aiplatform.memory.lifecycle import MemoryPipeline
from aiplatform.memory.repository import LongTermMemoryRepository
from aiplatform.model.runtime import ModelParams
from aiplatform.observability.metrics import Metrics
from aiplatform.shared.events import EventBus, TurnCompleted
from tests.conftest import FakeEmbedder, FakeProvider

pytestmark = pytest.mark.db
PARAMS = ModelParams("fake", "fake-qwen", 0.7, 0.8, 16384, 1024, False)


def build(db: Database, provider: FakeProvider, gate: ModelGate) -> tuple[BackgroundModelJobs, LongTermMemoryRepository]:
    emb = FakeEmbedder()
    repo = LongTermMemoryRepository(db, emb.model)
    pipeline = MemoryPipeline(db, repo, emb, MemorySettings(), AuditLog(db), Metrics(), EventBus())  # type: ignore[arg-type]
    jobs = BackgroundModelJobs(lambda: provider, lambda: PARAMS, db, pipeline, ConversationRepository(db), Settings(), gate)
    return jobs, repo


async def _wait_for(check: object, seconds: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        if await check():  # type: ignore[operator]
            return True
        await asyncio.sleep(0.05)
    return False


async def test_extraction_stores_candidate_and_records_the_model_call(db: Database, user_id: uuid.UUID) -> None:
    provider = FakeProvider()
    provider.push(
        text='{"memories": [{"content": "The user works on the shop project in Laravel.", "kind": "project", "confidence": 0.9}]}'
    )
    jobs, repo = build(db, provider, ModelGate())
    cid = await db.fetchval("INSERT INTO app.conversations (user_id) VALUES ($1) RETURNING id", user_id)
    mid = await db.fetchval(
        "INSERT INTO app.messages (conversation_id, seq, role, content) VALUES ($1,1,'user',$2) RETURNING id",
        cid,
        "I am rebuilding the shop project with Laravel this week.",
    )
    jobs.start()
    try:
        await jobs.on_turn_completed(
            TurnCompleted(user_id, cid, mid, "I am rebuilding the shop project with Laravel this week.", "Noted.")
        )

        async def stored() -> bool:
            return bool(await repo.list_items(user_id, status=None, kind=None, q="shop project", limit=5, offset=0))

        assert await _wait_for(stored), "extraction did not store a memory"
        rows = await repo.list_items(user_id, status=None, kind=None, q="shop project", limit=5, offset=0)
        # confidence 0.9 >= auto_activate_confidence (0.7) from a trusted source, so it is active (see MEMORY.md)
        assert rows[0]["status"] == "active" and rows[0]["source_type"] == "model_extracted"
        rec = await db.fetchrow(
            "SELECT q.purpose, r.status FROM app.model_requests q JOIN app.model_responses r "
            "ON r.model_request_id = q.id WHERE q.conversation_id = $1",
            cid,
        )
        assert rec is not None and rec["purpose"] == "extract" and rec["status"] == "ok"
    finally:
        await jobs.stop()


async def test_low_confidence_extraction_waits_for_review(db: Database, user_id: uuid.UUID) -> None:
    provider = FakeProvider()
    provider.push(
        text='{"memories": [{"content": "The user might be moving the API to Kubernetes.", "kind": "project", '
        '"confidence": 0.4}]}'
    )
    jobs, repo = build(db, provider, ModelGate())
    cid = await db.fetchval("INSERT INTO app.conversations (user_id) VALUES ($1) RETURNING id", user_id)
    jobs.start()
    try:
        await jobs.on_turn_completed(TurnCompleted(user_id, cid, 1, "We were discussing moving the API somewhere else.", "ok"))

        async def stored() -> bool:
            return bool(await repo.list_items(user_id, status=None, kind=None, q="Kubernetes", limit=5, offset=0))

        assert await _wait_for(stored), "extraction did not store a memory"
        rows = await repo.list_items(user_id, status=None, kind=None, q="Kubernetes", limit=5, offset=0)
        assert rows[0]["status"] == "candidate"
    finally:
        await jobs.stop()


async def test_explicit_remember_turn_is_not_re_extracted(db: Database, user_id: uuid.UUID) -> None:
    provider = FakeProvider()
    jobs, _ = build(db, provider, ModelGate())
    cid = await db.fetchval("INSERT INTO app.conversations (user_id) VALUES ($1) RETURNING id", user_id)
    await jobs.on_turn_completed(TurnCompleted(user_id, cid, 1, "Remember that I deploy on Fridays only.", "Saved."))
    assert jobs.queue.qsize() == 0


async def test_a_chat_turn_preempts_a_running_job(db: Database, user_id: uuid.UUID) -> None:
    class SlowProvider(FakeProvider):
        async def chat(self, req):  # type: ignore[no-untyped-def, override]
            self.requests.append(req)
            await asyncio.sleep(30)  # still "generating" when the user sends a message
            yield None  # pragma: no cover

    gate = ModelGate()
    jobs, _ = build(db, SlowProvider(), gate)
    cid = await db.fetchval("INSERT INTO app.conversations (user_id) VALUES ($1) RETURNING id", user_id)
    jobs.start()
    try:
        await jobs.on_turn_completed(TurnCompleted(user_id, cid, 1, "The billing module needs a rewrite before October.", "ok"))

        async def running() -> bool:
            return jobs._current is not None

        assert await _wait_for(running, 8.0), "job never started"
        gate.enter()  # a chat turn begins
        await asyncio.sleep(0.2)
        assert jobs._current is None or jobs._current.cancelled() or jobs._current.done()
        rec = await db.fetchrow(
            "SELECT status FROM app.model_responses r JOIN app.model_requests q ON q.id = r.model_request_id "
            "WHERE q.conversation_id = $1",
            cid,
        )
        assert rec is not None and rec["status"] == "cancelled"
        gate.leave()
    finally:
        await jobs.stop()
