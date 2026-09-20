"""ChatService (application service): one user turn from request to streamed answer.

resolve conversation → persist user message → explicit remember/forget fast path → retrieve memories
→ build bounded context → loop { stream model → execute tool calls (policy-checked) } → persist → publish event
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import structlog

from aiplatform.config import Settings
from aiplatform.conversation.repository import ConversationRepository, fingerprint
from aiplatform.db import Database
from aiplatform.memory import classifier
from aiplatform.memory.context_builder import ContextBuilder
from aiplatform.memory.extraction import ModelGate
from aiplatform.memory.lifecycle import Candidate, MemoryPipeline
from aiplatform.memory.repository import ShortTermMemoryRepository
from aiplatform.memory.retrieval import MemoryRetriever
from aiplatform.model.runtime import ModelRuntime
from aiplatform.model.types import ChatMessage, ChatRequest, ProviderError, ToolCall, ToolSpec, Usage
from aiplatform.observability import context as obs
from aiplatform.observability.metrics import Metrics
from aiplatform.orchestrator.prompts import system_prompt
from aiplatform.permissions.service import PermissionService
from aiplatform.security.auth import Principal
from aiplatform.shared import timing
from aiplatform.shared.events import EventBus, TurnCompleted
from aiplatform.shared.text import TokenEstimator
from aiplatform.tools.base import TaintState, ToolContext
from aiplatform.tools.executor import ToolExecutor
from aiplatform.tools.registry import ToolRegistry

log = structlog.get_logger("chat")
EventKind = Literal["content", "reasoning", "done", "error"]


@dataclass
class ChatCommand:
    principal: Principal
    messages: list[dict[str, Any]]
    client: Literal["webui", "api"] = "api"
    conversation_id: UUID | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    think: bool | None = None
    client_tools: list[dict[str, Any]] | None = None


@dataclass
class StreamEvent:
    kind: EventKind
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def parse_client_messages(raw: list[dict[str, Any]], passthrough: bool) -> tuple[str | None, list[ChatMessage], ChatMessage]:
    """Split the client's message list into (client system text, history, current user message)."""
    sys_parts: list[str] = []
    msgs: list[ChatMessage] = []
    for m in raw[-400:]:
        role = m.get("role")
        if role == "system":
            sys_parts.append(_text(m.get("content"))[:8000])
        elif role in ("user", "assistant"):
            calls = None
            if passthrough and role == "assistant" and m.get("tool_calls"):
                calls = [
                    ToolCall(
                        str(c.get("id", "")),
                        str(c.get("function", {}).get("name", "")),
                        str(c.get("function", {}).get("arguments", "{}")),
                    )
                    for c in m["tool_calls"]
                ]
            msgs.append(ChatMessage(role, _text(m.get("content")), tool_calls=calls))
        elif role == "tool" and passthrough:
            msgs.append(ChatMessage("tool", _text(m.get("content")), tool_call_id=m.get("tool_call_id")))
    if not msgs or msgs[-1].role not in ("user", "tool"):
        from aiplatform.shared.errors import ValidationFailed

        raise ValidationFailed("the last message must be from the user")
    return ("\n\n".join(p for p in sys_parts if p) or None), msgs[:-1], msgs[-1]


class ChatService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        conversations: ConversationRepository,
        retriever: MemoryRetriever,
        pipeline: MemoryPipeline,
        stm: ShortTermMemoryRepository,
        builder: ContextBuilder,
        runtime: ModelRuntime,
        registry: ToolRegistry,
        executor: ToolExecutor,
        permissions: PermissionService,
        events: EventBus,
        metrics: Metrics,
        gate: ModelGate,
        tokens: TokenEstimator,
    ) -> None:
        self.db = db
        self.s = settings
        self.conversations = conversations
        self.retriever = retriever
        self.pipeline = pipeline
        self.stm = stm
        self.builder = builder
        self.runtime = runtime
        self.registry = registry
        self.executor = executor
        self.permissions = permissions
        self.events = events
        self.metrics = metrics
        self.gate = gate
        self.tokens = tokens

    async def _resolve_conversation(
        self, cmd: ChatCommand, history: list[ChatMessage], current: ChatMessage
    ) -> tuple[UUID, bool]:
        uid = cmd.principal.user_id
        if cmd.conversation_id is not None and await self.conversations.get(cmd.conversation_id, uid):
            return cmd.conversation_id, False
        users = [m for m in history if m.role == "user"]
        first_user = users[0].content if users else current.content
        first_asst = next((m.content for m in history if m.role == "assistant" and m.content), None)
        title = " ".join(first_user.split())[:80]
        if first_asst is None and not users:
            return await self.conversations.create(uid, client=cmd.client, title=title, fp=fingerprint(first_user, None)), True
        fp = fingerprint(first_user, first_asst)
        cid = await self.conversations.find_by_fingerprint(uid, fp)
        if cid is None:
            cid = await self.conversations.create(uid, client=cmd.client, title=title, fp=fp)
        return cid, False

    async def handle(self, cmd: ChatCommand) -> AsyncIterator[StreamEvent]:
        t_start = time.perf_counter()
        passthrough = bool(cmd.client_tools)
        user_system, history, current = parse_client_messages(cmd.messages, passthrough)
        params = self.runtime.params()
        snap = await self.permissions.snapshot()
        cid, new_chat = await self._resolve_conversation(cmd, history, current)
        user_msg_id = await self.conversations.append(
            cid, current.role, current.content, tokens=self.tokens.count(current.content)
        )
        self.gate.enter()
        try:
            async for ev in self._turn(
                cmd, cid, new_chat, user_msg_id, user_system, history, current, params, snap, passthrough, t_start
            ):
                yield ev
        finally:
            self.gate.leave()

    async def _turn(
        self,
        cmd: ChatCommand,
        cid: UUID,
        new_chat: bool,
        user_msg_id: int,
        user_system: str | None,
        history: list[ChatMessage],
        current: ChatMessage,
        params: Any,
        snap: Any,
        passthrough: bool,
        t_start: float,
    ) -> AsyncIterator[StreamEvent]:
        uid = cmd.principal.user_id
        # explicit "remember ..." / "forget ..." fast path (no model call needed to store it)
        if current.role == "user":
            note = await self._explicit_memory(uid, cid, user_msg_id, current.content)
            if note:
                yield StreamEvent("reasoning", note + "\n")
        # long-term + short-term memory
        retrieval = await self.retriever.retrieve(uid, current.content) if current.role == "user" else None
        memories = retrieval.memories if retrieval else []
        stm_items = await self.stm.active(cid)
        conv = await self.conversations.get(cid)
        summary = (conv or {}).get("summary", "")
        tools: list[ToolSpec] = [] if passthrough else self.registry.specs_for(snap)
        if passthrough:
            tools = [
                ToolSpec(
                    str(t.get("function", {}).get("name", "")),
                    str(t.get("function", {}).get("description", "")),
                    t.get("function", {}).get("parameters") or {"type": "object"},
                )
                for t in (cmd.client_tools or [])
            ]
        with timing.stage("context_build"):
            built = self.builder.build(
                system_prompt=system_prompt(snap, params.model, bool(tools)),
                user_system=user_system,
                history=history,
                current=current,
                memories=memories,
                short_term=stm_items,
                summary=summary,
                tools=tools,
                num_ctx=params.num_ctx,
            )
        yield StreamEvent(
            "reasoning",
            f"🧠 memory: short-term {len(stm_items)} item(s), {built.stats.turns_included} turn(s)"
            f" · long-term {len(memories)}"
            + (f" ({retrieval.skipped})" if retrieval and retrieval.skipped else "")
            + f" · context ≈{built.stats.total} / {params.num_ctx} tokens · mode {snap.mode}\n",
        )

        messages = built.messages
        taint = TaintState()
        streamed: list[str] = []
        total = Usage(prompt_tokens=0, completion_tokens=0, prompt_ms=0.0, generation_ms=0.0)
        ttft: float | None = None
        tool_calls_total = 0
        final_calls: list[ToolCall] = []
        finish = "stop"
        provider = self.runtime.provider()
        for round_no in range(self.s.tools.max_rounds + 1):
            use_tools = tools if round_no < self.s.tools.max_rounds else []
            req = ChatRequest(
                messages=messages,
                tools=use_tools,
                temperature=cmd.temperature if cmd.temperature is not None else params.temperature,
                top_p=cmd.top_p if cmd.top_p is not None else params.top_p,
                max_tokens=min(cmd.max_tokens or params.max_output_tokens, params.max_output_tokens),
                num_ctx=params.num_ctx,
                think=cmd.think if cmd.think is not None else params.think,
            )
            mr_id = await self._record_request(cid, provider.name, params, built.stats.total, len(memories), round_no, req)
            t_round = time.perf_counter()
            round_text: list[str] = []
            calls: list[ToolCall] = []
            usage: Usage | None = None
            try:
                with timing.stage("model"):
                    async for ch in provider.chat(req):
                        if ttft is None and (ch.content or ch.reasoning or ch.done):
                            ttft = (time.perf_counter() - t_round) * 1000
                            timing.add("provider_ttft", ttft)
                        if ch.reasoning:
                            yield StreamEvent("reasoning", ch.reasoning)
                        if ch.content:
                            round_text.append(ch.content)
                            yield StreamEvent("content", ch.content)
                        if ch.done:
                            calls, usage, finish = ch.tool_calls, ch.usage, ch.finish_reason or "stop"
            except ProviderError as e:
                await self._record_response(mr_id, "error", None, None, time.perf_counter() - t_round, 0, str(e))
                log.warning("provider_error", error=str(e))
                yield StreamEvent("error", str(e))
                return
            if usage:
                for k in ("prompt_tokens", "completion_tokens", "prompt_ms", "generation_ms"):
                    v = getattr(usage, k)
                    if v is not None:
                        setattr(total, k, (getattr(total, k) or 0) + v)
                if usage.prompt_tokens:
                    schema_chars = len(json.dumps([t.__dict__ for t in req.tools])) if req.tools else 0
                    self.tokens.calibrate(sum(len(m.content) for m in messages) + schema_chars, usage.prompt_tokens)
            await self._record_response(mr_id, "ok", finish, usage, time.perf_counter() - t_round, len(calls), None, ttft)
            streamed.extend(round_text)
            if not calls:
                break
            if passthrough:
                final_calls = calls
                break
            messages = [*messages, ChatMessage("assistant", "".join(round_text), tool_calls=calls)]
            budget = max(0, self.s.tools.max_calls_per_turn - tool_calls_total)
            for skipped in calls[budget:]:
                # every tool_call must get a result, or strict OpenAI-compatible servers reject the next request
                messages.append(
                    ChatMessage(
                        "tool",
                        '<tool_result status="skipped">tool call limit for this turn reached</tool_result>',
                        tool_call_id=skipped.id,
                        name=skipped.name,
                    )
                )
            for call in calls[:budget]:
                tool_calls_total += 1
                yield StreamEvent("reasoning", f"🔧 {call.name.replace('_', '.', 1)} {_args_preview(call.arguments)}\n")
                status_lines: list[str] = []

                async def on_status(line: str, _sl: list[str] = status_lines) -> None:
                    _sl.append(line)

                tctx = ToolContext(uid, cid, mr_id, snap, taint, current.content)
                outcome = await self.executor.execute(call, tctx, on_status)
                for line in status_lines:
                    yield StreamEvent("reasoning", line + "\n")
                mark = {"succeeded": "✓", "denied": "⛔", "expired": "⌛", "timed_out": "⌛"}.get(outcome.status, "✗")
                yield StreamEvent("reasoning", f"   {mark} {outcome.result.summary} ({outcome.duration_ms:.0f} ms)\n")
                content = outcome.model_content(self.s.tools.result_max_chars)
                messages.append(ChatMessage("tool", content, tool_call_id=call.id, name=call.name))
                await self.conversations.append(
                    cid, "tool", content[:20000], tokens=self.tokens.count(content), tool_call_id=call.id, model_request_id=mr_id
                )
            if tool_calls_total >= self.s.tools.max_calls_per_turn:
                messages.append(
                    ChatMessage("user", "(system notice: tool call limit for this turn reached; answer now with what you have.)")
                )
        answer = "".join(streamed)
        await self.conversations.append(
            cid, "assistant", answer, tokens=self.tokens.count(answer), tool_calls=final_calls or None
        )
        users_hist = [m for m in history if m.role == "user"]
        if new_chat or (not users_hist and not any(m.role == "assistant" for m in history)):
            await self.conversations.set_fingerprint(
                cid, fingerprint(users_hist[0].content if users_hist else current.content, answer)
            )
        if current.role == "user" and answer:
            self.events.publish(TurnCompleted(uid, cid, user_msg_id, current.content, answer))
        total_ms = (time.perf_counter() - t_start) * 1000
        timing.add("total", total_ms)
        self.metrics.model_tokens.labels("prompt").inc(total.prompt_tokens or 0)
        self.metrics.model_tokens.labels("completion").inc(total.completion_tokens or 0)
        if ttft is not None:
            self.metrics.model_ttft.observe(ttft)
        if total.generation_ms and total.completion_tokens:
            self.metrics.model_tps.observe(total.completion_tokens / (total.generation_ms / 1000))
        yield StreamEvent(
            "done",
            data={
                "conversation_id": str(cid),
                "finish_reason": "tool_calls" if final_calls else "stop",
                "tool_calls": [c.__dict__ for c in final_calls],
                "usage": total.__dict__,
                "ttft_ms": ttft,
                "total_ms": total_ms,
                "stages": timing.current(),
                "context": built.stats.as_dict(),
                "memories": built.injected_memory_ids,
                "tools_called": tool_calls_total,
                "tainted_by": taint.sources,
            },
        )

    async def _explicit_memory(self, uid: UUID, cid: UUID, msg_id: int, text: str) -> str | None:
        body = classifier.explicit_remember(text)
        if body:
            res = await self.pipeline.submit(
                Candidate(
                    user_id=uid,
                    content=body,
                    source_type="user_stated",
                    explicit=True,
                    conversation_id=cid,
                    message_id=msg_id,
                    confidence=0.95,
                )
            )
            self.retriever.invalidate(uid)
            return {
                "stored_active": "💾 saved to long-term memory",
                "superseded": "💾 updated long-term memory",
                "duplicate": "💾 already in long-term memory",
                "merged": "💾 merged with an existing memory",
                "stored_candidate": "💾 saved as a memory candidate (review in the admin console)",
            }.get(res.outcome, f"💾 not saved: {res.reason}")
        return None

    async def _record_request(
        self, cid: UUID, provider: str, params: Any, prompt_est: int, n_mem: int, round_no: int, req: ChatRequest
    ) -> UUID:
        mid: UUID = await self.db.fetchval(
            "INSERT INTO app.model_requests (conversation_id, request_id, correlation_id, provider, model, purpose, params, "
            "context_budget, prompt_tokens_est, memories_injected, tool_round) VALUES ($1,$2,$3,$4,$5,'chat',$6,$7,$8,$9,$10) "
            "RETURNING id",
            cid,
            obs.request_id.get(),
            obs.correlation_id.get(),
            provider,
            params.model,
            {
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_ctx": req.num_ctx,
                "max_tokens": req.max_tokens,
                "think": req.think,
                "tools": len(req.tools),
            },
            params.num_ctx,
            prompt_est,
            n_mem,
            round_no,
        )
        return mid

    async def _record_response(
        self,
        mr_id: UUID,
        status: str,
        finish: str | None,
        usage: Usage | None,
        secs: float,
        n_calls: int,
        error: str | None,
        ttft: float | None = None,
    ) -> None:
        tps = (
            (usage.completion_tokens / (usage.generation_ms / 1000))
            if usage and usage.completion_tokens and usage.generation_ms
            else None
        )
        await self.db.execute(
            "INSERT INTO app.model_responses (model_request_id, status, finish_reason, prompt_tokens, completion_tokens, "
            "ttft_ms, total_ms, tokens_per_s, stage_ms, tool_calls, error) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            mr_id,
            status,
            finish,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            ttft,
            secs * 1000,
            tps,
            {k: round(v, 3) for k, v in timing.current().items()},
            n_calls,
            (error or "")[:1000] or None,
        )


def _args_preview(raw: str) -> str:
    s = " ".join(raw.split())
    return s if len(s) <= 140 else s[:137] + "…"
