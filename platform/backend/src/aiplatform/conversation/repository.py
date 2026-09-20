"""Conversation aggregate persistence + web-UI conversation matching (the UI is stateless and sends no chat id)."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from aiplatform.db import Database
from aiplatform.model.types import ToolCall
from aiplatform.shared.text import normalise


def fingerprint(first_user: str, first_assistant: str | None) -> bytes:
    """Stable identity of a chat: its first exchange. New chats (no reply yet) use the first user message alone."""
    h = hashlib.sha256(normalise(first_user).encode("utf-8"))
    if first_assistant is not None:
        h.update(b"\x1f" + normalise(first_assistant).encode("utf-8"))
    return h.digest()


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, conversation_id: UUID, user_id: UUID | None = None) -> dict[str, Any] | None:
        r = await self.db.fetchrow(
            "SELECT * FROM app.conversations WHERE id=$1 AND ($2::uuid IS NULL OR user_id=$2)", conversation_id, user_id
        )
        return dict(r) if r else None

    async def find_by_fingerprint(self, user_id: UUID, fp: bytes, *, recent_seconds: int | None = None) -> UUID | None:
        cid: UUID | None = await self.db.fetchval(
            "SELECT id FROM app.conversations WHERE user_id=$1 AND fingerprint=$2 AND archived_at IS NULL "
            "AND ($3::int IS NULL OR created_at > now() - make_interval(secs => $3)) ORDER BY updated_at DESC LIMIT 1",
            user_id,
            fp,
            recent_seconds,
        )
        return cid

    async def create(self, user_id: UUID, *, client: str, title: str, fp: bytes | None) -> UUID:
        cid: UUID = await self.db.fetchval(
            "INSERT INTO app.conversations (user_id, client, title, fingerprint) VALUES ($1,$2,$3,$4) RETURNING id",
            user_id,
            client,
            title[:200],
            fp,
        )
        return cid

    async def set_fingerprint(self, conversation_id: UUID, fp: bytes) -> None:
        await self.db.execute("UPDATE app.conversations SET fingerprint=$2 WHERE id=$1", conversation_id, fp)

    async def append(
        self,
        conversation_id: UUID,
        role: str,
        content: str,
        *,
        tokens: int,
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str | None = None,
        model_request_id: UUID | None = None,
    ) -> int:
        """Append atomically: seq = message_count + 1 (row lock on the conversation serialises concurrent appends)."""
        async with self.db.transaction() as tx:
            seq = await tx.fetchval(
                "UPDATE app.conversations SET message_count = message_count + 1, "
                "token_count = token_count + $2 WHERE id=$1 RETURNING message_count",
                conversation_id,
                tokens,
            )
            mid: int = await tx.fetchval(
                "INSERT INTO app.messages (conversation_id, seq, role, content, tool_calls, tool_call_id, token_count, "
                "model_request_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                conversation_id,
                seq,
                role,
                content,
                [c.__dict__ for c in tool_calls] if tool_calls else None,
                tool_call_id,
                tokens,
                model_request_id,
            )
        return mid

    async def messages_between(self, conversation_id: UUID, after_seq: int, upto_seq: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetch(
                "SELECT seq, role, content FROM app.messages WHERE conversation_id=$1 AND seq > $2 AND seq <= $3 ORDER BY seq",
                conversation_id,
                after_seq,
                upto_seq,
            )
        ]

    async def recent(self, conversation_id: UUID, limit: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT seq, role, content, tool_calls, tool_call_id FROM app.messages "
            "WHERE conversation_id=$1 ORDER BY seq DESC LIMIT $2",
            conversation_id,
            limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def set_summary(self, conversation_id: UUID, summary: str, upto_seq: int) -> None:
        await self.db.execute(
            "UPDATE app.conversations SET summary=$2, summary_upto_seq=$3 WHERE id=$1", conversation_id, summary, upto_seq
        )

    async def list_conversations(self, user_id: UUID, *, limit: int, offset: int, q: str | None) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT id, title, client, message_count, token_count, created_at, updated_at, archived_at, "
            "(summary <> '') AS has_summary FROM app.conversations WHERE user_id=$1 "
            "AND ($4::text IS NULL OR title ILIKE '%' || $4 || '%') ORDER BY updated_at DESC LIMIT $2 OFFSET $3",
            user_id,
            limit,
            offset,
            q,
        )
        return [dict(r) for r in rows]

    async def messages(self, conversation_id: UUID, user_id: UUID, *, limit: int, before_seq: int | None) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT m.id, m.seq, m.role, m.content, m.tool_calls, m.tool_call_id, m.token_count, m.created_at "
            "FROM app.messages m JOIN app.conversations c ON c.id=m.conversation_id WHERE m.conversation_id=$1 "
            "AND c.user_id=$2 AND ($3::int IS NULL OR m.seq < $3) ORDER BY m.seq DESC LIMIT $4",
            conversation_id,
            user_id,
            before_seq,
            limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def archive(self, conversation_id: UUID, user_id: UUID) -> bool:
        r = await self.db.execute(
            "UPDATE app.conversations SET archived_at=now() WHERE id=$1 AND user_id=$2", conversation_id, user_id
        )
        return r.endswith(" 1")

    async def delete(self, conversation_id: UUID, user_id: UUID) -> bool:
        r = await self.db.execute("DELETE FROM app.conversations WHERE id=$1 AND user_id=$2", conversation_id, user_id)
        return r.endswith(" 1")
