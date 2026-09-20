"""Memory tools the model may call: memory.search, memory.save, memory.forget.

memory.save only creates *candidates* unless the current user message explicitly asked to remember something; the
model cannot promote its own guesses into trusted long-term memory.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import Field

from aiplatform.memory.lifecycle import Candidate, MemoryPipeline
from aiplatform.memory.retrieval import MemoryRetriever
from aiplatform.permissions.engine import PermissionRequest
from aiplatform.permissions.models import Risk
from aiplatform.tools.base import Prepared, ToolArgs, ToolContext, ToolInputError, ToolResult

_EXPLICIT = re.compile(r"\b(remember|don't forget|keep in mind|note that|save (this|that))\b", re.I)


class MemSearchArgs(ToolArgs):
    query: str = Field(..., min_length=2, max_length=500)
    limit: int = Field(8, ge=1, le=20)


class MemorySearch:
    name = "memory.search"
    category: ClassVar[Literal["memory"]] = "memory"
    description = "Search the user's long-term memory (hybrid semantic + keyword). Use when earlier context may help."
    risk: ClassVar[Risk] = "read"
    Args = MemSearchArgs
    timeout_s: ClassVar[float] = 15.0

    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    def prepare(self, args: MemSearchArgs, ctx: ToolContext) -> Prepared:
        return Prepared(PermissionRequest(self.name, "read"), {"query": args.query}, f"search memory “{args.query[:60]}”", args)

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        args: MemSearchArgs = prepared.payload
        r = await self.retriever.retrieve(ctx.user_id, args.query, limit=args.limit, force=True)
        items = [
            {
                "id": str(m.id),
                "kind": m.kind,
                "content": m.content,
                "confidence": round(m.confidence, 2),
                "source": m.source_type,
                "updated": m.updated_at.date().isoformat(),
            }
            for m in r.memories
        ]
        untrusted = any(m.source_type in ("tool_output", "web") for m in r.memories)
        return ToolResult(
            True, {"memories": items}, f"{len(items)} memories", untrusted_source="memory:untrusted" if untrusted else None
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return await self.run(prepared, ctx)


class MemSaveArgs(ToolArgs):
    content: str = Field(..., min_length=8, max_length=1000, description="one self-contained sentence")
    kind: Literal["preference", "fact", "project", "instruction", "decision", "context"] = "fact"


class MemorySave:
    name = "memory.save"
    category: ClassVar[Literal["memory"]] = "memory"
    description = (
        "Save a durable fact/preference about the user or their projects to long-term memory. Use when the "
        "user asks you to remember something, or states a lasting preference."
    )
    risk: ClassVar[Risk] = "write"
    Args = MemSaveArgs
    timeout_s: ClassVar[float] = 20.0

    def __init__(self, pipeline: MemoryPipeline) -> None:
        self.pipeline = pipeline

    def prepare(self, args: MemSaveArgs, ctx: ToolContext) -> Prepared:
        return Prepared(
            PermissionRequest(self.name, "write", taint_sensitive=True),
            {"content": args.content, "kind": args.kind},
            f"remember “{args.content[:80]}”",
            args,
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        args: MemSaveArgs = prepared.payload
        explicit = bool(_EXPLICIT.search(ctx.user_text)) and not ctx.taint.tainted
        res = await self.pipeline.submit(
            Candidate(
                user_id=ctx.user_id,
                content=args.content,
                proposed_kind=args.kind,
                confidence=0.75 if explicit else 0.55,
                source_type="user_stated" if explicit else ("tool_output" if ctx.taint.tainted else "model_extracted"),
                explicit=explicit,
                conversation_id=ctx.conversation_id,
            )
        )
        ok = res.outcome not in ("rejected_sensitive", "rejected_invalid")
        return ToolResult(
            ok,
            {"outcome": res.outcome, "memory_id": str(res.memory_id) if res.memory_id else None, "note": res.reason},
            f"memory {res.outcome}",
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(True, {"dry_run": True, **prepared.canonical}, "dry run: save memory")


class MemForgetArgs(ToolArgs):
    memory_id: str = Field(..., min_length=8, max_length=36, description="id (or 8-char prefix) from memory_search")


class MemoryForget:
    name = "memory.forget"
    category: ClassVar[Literal["memory"]] = "memory"
    description = "Delete one long-term memory by id when the user asks you to forget it."
    risk: ClassVar[Risk] = "destructive"
    Args = MemForgetArgs
    timeout_s: ClassVar[float] = 10.0

    def __init__(self, pipeline: MemoryPipeline) -> None:
        self.pipeline = pipeline

    def prepare(self, args: MemForgetArgs, ctx: ToolContext) -> Prepared:
        if not re.fullmatch(r"[0-9a-fA-F-]{8,36}", args.memory_id):
            raise ToolInputError("memory_id must be a UUID or its first 8 characters")
        return Prepared(
            PermissionRequest(self.name, "destructive"),
            {"memory_id": args.memory_id},
            f"forget memory {args.memory_id[:8]}",
            args,
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        args: MemForgetArgs = prepared.payload
        mid = await self._resolve(args.memory_id, ctx.user_id)
        await self.pipeline.delete(mid, ctx.user_id, actor="model")
        return ToolResult(True, {"deleted": str(mid)}, "memory deleted")

    async def _resolve(self, ident: str, user_id: UUID) -> UUID:
        if len(ident) == 36:
            return UUID(ident)
        rows: list[Any] = await self.pipeline.db.fetch(
            "SELECT id FROM app.long_term_memories WHERE user_id=$1 AND id::text LIKE $2 || '%' AND status IN "
            "('active','candidate') LIMIT 2",
            user_id,
            ident.lower(),
        )
        if len(rows) != 1:
            raise ToolInputError("memory id prefix not found or ambiguous; use memory_search to get the full id")
        mid: UUID = rows[0]["id"]
        return mid

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(True, {"dry_run": True, **prepared.canonical}, "dry run: forget memory")


def memory_tools(retriever: MemoryRetriever, pipeline: MemoryPipeline) -> list[Any]:
    return [MemorySearch(retriever), MemorySave(pipeline), MemoryForget(pipeline)]
