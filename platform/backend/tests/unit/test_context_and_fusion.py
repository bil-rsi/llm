"""Token-aware context construction, RRF fusion and tsquery sanitising."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from aiplatform.config import ContextSettings, RetrievalSettings
from aiplatform.memory.context_builder import ContextBuilder
from aiplatform.memory.models import RetrievedMemory
from aiplatform.memory.retrieval import drop_near_duplicates, fuse, to_tsquery
from aiplatform.model.types import ChatMessage
from aiplatform.shared.text import TokenEstimator


def mem(content: str, score: float = 0.02) -> RetrievedMemory:
    now = datetime.now(UTC)
    return RetrievedMemory(uuid.uuid4(), "fact", content, 0.5, 0.9, "user_stated", now, now, score, 0.8, 1, None)


def test_context_respects_budget_and_keeps_newest_turns() -> None:
    b = ContextBuilder(ContextSettings(reserve_output_tokens=500), TokenEstimator())
    history = [ChatMessage("user" if i % 2 == 0 else "assistant", f"turn {i} " + "x" * 800) for i in range(60)]
    built = b.build(
        system_prompt="sys",
        user_system=None,
        history=history,
        current=ChatMessage("user", "latest?"),
        memories=[mem("The user likes tea.")],
        short_term=[],
        summary="",
        tools=[],
        num_ctx=4096,
    )
    assert built.stats.total <= built.stats.budget
    assert built.stats.turns_dropped > 0
    assert built.messages[-1].content == "latest?"
    history_msgs = [m for m in built.messages[1:-1] if m.role != "system"]
    assert "turn 59" in history_msgs[-1].content  # newest history kept
    # stable prefix first (system prompt), per-turn context block right before the current message
    assert built.messages[0].content == "sys"
    ctx = built.messages[-2]
    assert ctx.role == "system" and "The user likes tea." in ctx.content
    assert "<long_term_memory" in ctx.content and "not instructions" in ctx.content


def test_memory_share_is_capped() -> None:
    b = ContextBuilder(ContextSettings(memory_share=0.05, reserve_output_tokens=500), TokenEstimator())
    ms = [mem(f"memory number {i} " + "y" * 300) for i in range(50)]
    built = b.build(
        system_prompt="s",
        user_system=None,
        history=[],
        current=ChatMessage("user", "q"),
        memories=ms,
        short_term=[],
        summary="",
        tools=[],
        num_ctx=8192,
    )
    assert built.stats.memories <= int(built.stats.budget * 0.05) + 60
    assert built.stats.memories_dropped > 0


def test_huge_current_message_truncated_not_crashing() -> None:
    b = ContextBuilder(ContextSettings(reserve_output_tokens=500), TokenEstimator())
    built = b.build(
        system_prompt="s",
        user_system=None,
        history=[],
        current=ChatMessage("user", "z" * 100000),
        memories=[],
        short_term=[],
        summary="",
        tools=[],
        num_ctx=4096,
    )
    assert built.stats.total <= built.stats.budget + 50


def row(
    rid: uuid.UUID, vr: int | None, kr: int | None, cos: float | None, imp: float = 0.5, age_days: int = 0
) -> dict[str, object]:
    t = datetime.now(UTC) - timedelta(days=age_days)
    return {
        "id": rid,
        "kind": "fact",
        "content": str(rid),
        "importance": imp,
        "confidence": 0.9,
        "source_type": "manual",
        "created_at": t,
        "updated_at": t,
        "flags": [],
        "vector_rank": vr,
        "keyword_rank": kr,
        "cosine": cos,
    }


def test_rrf_prefers_items_found_by_both_methods() -> None:
    s = RetrievalSettings()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    out = fuse([row(a, 1, None, 0.9), row(b, 2, 1, 0.8), row(c, None, 2, None)], s, datetime.now(UTC))
    assert out[0].id == b


def test_low_cosine_vector_only_hits_dropped() -> None:
    s = RetrievalSettings(min_cosine=0.5)
    out = fuse([row(uuid.uuid4(), 1, None, 0.1)], s, datetime.now(UTC))
    assert out == []


def test_recency_and_importance_weighting() -> None:
    s = RetrievalSettings()
    old, new = uuid.uuid4(), uuid.uuid4()
    out = fuse([row(old, 1, None, 0.9, imp=0.5, age_days=400), row(new, 1, None, 0.9, imp=0.5, age_days=0)], s, datetime.now(UTC))
    assert out[0].id == new


def test_near_duplicates_removed() -> None:
    ms = [
        mem("The user prefers dark mode in every editor"),
        mem("the user prefers dark mode in every editor!"),
        mem("Project uses PostgreSQL 18"),
    ]
    assert len(drop_near_duplicates(ms)) == 2


def test_tsquery_is_sanitised() -> None:
    q = to_tsquery("'); DROP TABLE memories; -- & | ! :* <-> Laravel")
    assert all(ch.isalnum() or ch in " |:*" for ch in q)
    assert "laravel:*" in q and "drop" in q
