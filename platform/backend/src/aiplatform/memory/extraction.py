"""Background model jobs: long-term memory extraction and rolling conversation summaries.

The model has one slot (single-user laptop), so background jobs only run when no chat turn is in progress and never
delay a reply. The queue is bounded; when full, the oldest optional job is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from aiplatform.config import Settings
from aiplatform.conversation.repository import ConversationRepository
from aiplatform.db import Database
from aiplatform.memory import classifier
from aiplatform.memory.lifecycle import Candidate, MemoryPipeline
from aiplatform.model.types import ChatMessage, ChatRequest, LLMProvider, ProviderError
from aiplatform.shared.events import TurnCompleted

log = structlog.get_logger("background")

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "maxLength": 300},
                    "kind": {"type": "string", "enum": ["preference", "fact", "project", "instruction", "decision", "context"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["content", "kind", "confidence"],
            },
        }
    },
    "required": ["memories"],
}

EXTRACT_PROMPT = """You maintain long-term memory for a personal assistant. From the USER message below, extract at most 3
durable facts worth remembering in future conversations: stable preferences, facts about the user or their projects,
decisions, standing instructions about how they like answers. Rules:
- Only use what the USER said. Ignore the assistant reply except to understand context.
- Skip one-off requests, questions, small talk, anything temporary, and anything already obvious.
- Never extract passwords, keys, tokens or other secrets.
- Write each memory as one short self-contained sentence in third person ("The user prefers ...").
- If nothing is worth remembering, return {"memories": []}.

USER: {user}

ASSISTANT (context only): {assistant}"""

SUMMARY_PROMPT = """Summarise this conversation so far for your own future reference in at most 200 words. Keep decisions,
open tasks, file/project names and facts the user gave. Plain prose, no preamble.

{previous}{transcript}"""


class ModelGate:
    """Tracks in-flight chat turns; background jobs wait for idle and are cancelled the moment a chat starts."""

    def __init__(self) -> None:
        self.active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._on_enter: list[Callable[[], None]] = []

    def on_enter(self, cb: Callable[[], None]) -> None:
        self._on_enter.append(cb)

    def enter(self) -> None:
        self.active += 1
        self._idle.clear()
        for cb in self._on_enter:
            cb()

    def leave(self) -> None:
        self.active = max(0, self.active - 1)
        if self.active == 0:
            self._idle.set()

    async def wait_idle(self, settle_s: float = 2.0) -> None:
        while True:
            await self._idle.wait()
            await asyncio.sleep(settle_s)
            if self.active == 0:
                return


Job = Callable[[], Awaitable[None]]


class BackgroundModelJobs:
    def __init__(
        self,
        provider_fn: Callable[[], LLMProvider],
        params_fn: Callable[[], Any],
        db: Database,
        pipeline: MemoryPipeline,
        conversations: ConversationRepository,
        settings: Settings,
        gate: ModelGate,
        max_queue: int = 32,
    ) -> None:
        self.provider_fn = provider_fn
        self.params_fn = params_fn
        self.db = db
        self._current: asyncio.Task[None] | None = None
        gate.on_enter(self._preempt)
        self.pipeline = pipeline
        self.conversations = conversations
        self.s = settings
        self.gate = gate
        self.queue: asyncio.Queue[tuple[str, Job]] = asyncio.Queue(maxsize=max_queue)
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name="background-model-jobs")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    def submit(self, name: str, job: Job) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait((name, job))

    def _preempt(self) -> None:
        """A chat turn started: stop the running background job (closing the stream aborts generation)."""
        if self._current is not None and not self._current.done():
            self._current.cancel()

    async def _run(self) -> None:
        while True:
            name, job = await self.queue.get()
            for attempt in range(3):
                await self.gate.wait_idle()
                self._current = asyncio.create_task(asyncio.wait_for(job(), timeout=300))
                try:
                    await self._current
                    break
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    log.info("background_job_preempted", job=name, attempt=attempt + 1)
                except Exception:
                    log.exception("background_job_failed", job=name)
                    break
                finally:
                    self._current = None

    # ── event handler ──
    async def on_turn_completed(self, ev: TurnCompleted) -> None:
        if (
            self.s.memory.extraction == "auto"
            and not classifier.trivial(ev.user_text)
            and len(ev.user_text) >= 20
            and classifier.explicit_remember(ev.user_text) is None
        ):
            self.submit("extract", lambda: self._extract(ev))
        conv = await self.conversations.get(ev.conversation_id)
        if conv and conv["message_count"] - conv["summary_upto_seq"] > self.s.context.summarise_after_messages:
            self.submit("summarise", lambda: self._summarise(ev.conversation_id))

    async def _complete(
        self, prompt: str, schema: dict[str, Any] | None, max_tokens: int, purpose: str, conversation_id: Any = None
    ) -> str:
        params = self.params_fn()
        # Same num_ctx as chat: a different value makes Ollama reload the whole model (tens of seconds).
        req = ChatRequest(
            messages=[ChatMessage("user", prompt)],
            temperature=0.2,
            top_p=0.9,
            max_tokens=max_tokens,
            num_ctx=params.num_ctx,
            think=False,
            json_schema=schema,
        )
        provider = self.provider_fn()
        mr_id = await self.db.fetchval(
            "INSERT INTO app.model_requests (conversation_id, request_id, correlation_id, provider, model, purpose, params, "
            "context_budget, prompt_tokens_est) VALUES ($1,'background','background',$2,$3,$4,$5,$6,$7) RETURNING id",
            conversation_id,
            provider.name,
            params.model,
            purpose,
            {"max_tokens": max_tokens, "json_schema": schema is not None},
            params.num_ctx,
            len(prompt) // 4,
        )
        t0 = time.perf_counter()
        out: list[str] = []
        usage = None
        status, error = "ok", None
        try:
            async for ch in provider.chat(req):
                out.append(ch.content)
                if ch.done:
                    usage = ch.usage
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except ProviderError as e:
            status, error = "error", str(e)[:1000]
            raise
        finally:
            tps = None
            if usage and usage.completion_tokens and usage.generation_ms:
                tps = usage.completion_tokens / (usage.generation_ms / 1000)
            await self.db.execute(
                "INSERT INTO app.model_responses (model_request_id, status, prompt_tokens, completion_tokens, total_ms, "
                "tokens_per_s, error) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                mr_id,
                status,
                usage.prompt_tokens if usage else None,
                usage.completion_tokens if usage else None,
                (time.perf_counter() - t0) * 1000,
                tps,
                error,
            )
        return "".join(out)

    async def _extract(self, ev: TurnCompleted) -> None:
        try:
            raw = await self._complete(
                EXTRACT_PROMPT.replace("{user}", ev.user_text[:4000]).replace("{assistant}", ev.assistant_text[:1500]),
                EXTRACT_SCHEMA,
                256,
                "extract",
                ev.conversation_id,
            )
            items = json.loads(raw).get("memories", [])
            log.info("extraction_done", candidates=len(items))
        except (ProviderError, ValueError, AttributeError) as e:
            log.warning("extraction_failed", error=str(e)[:200])
            return
        for it in items[:3]:
            if not isinstance(it, dict) or not isinstance(it.get("content"), str):
                continue
            await self.pipeline.submit(
                Candidate(
                    user_id=ev.user_id,
                    content=it["content"],
                    source_type="model_extracted",
                    proposed_kind=it.get("kind"),
                    confidence=float(it.get("confidence", 0.5)) if isinstance(it.get("confidence"), int | float) else 0.5,
                    conversation_id=ev.conversation_id,
                    message_id=ev.user_message_id,
                )
            )

    async def _summarise(self, conversation_id: Any) -> None:
        conv = await self.conversations.get(conversation_id)
        if conv is None:
            return
        keep_recent = 8
        upto = conv["message_count"] - keep_recent
        if upto <= conv["summary_upto_seq"]:
            return
        msgs = await self.conversations.messages_between(conversation_id, conv["summary_upto_seq"], upto)
        transcript = "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in msgs if m["role"] in ("user", "assistant"))
        prev = f"Previous summary:\n{conv['summary']}\n\nNew messages:\n" if conv["summary"] else ""
        try:
            text = await self._complete(
                SUMMARY_PROMPT.replace("{previous}", prev).replace("{transcript}", transcript[:24000]),
                None,
                400,
                "summarise",
                conversation_id,
            )
        except ProviderError as e:
            log.warning("summary_failed", error=str(e)[:200])
            return
        if text.strip():
            await self.conversations.set_summary(conversation_id, text.strip()[:4000], upto)
