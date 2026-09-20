"""OpenAI-compatible endpoints used by the llama.cpp web UI and other clients:
/v1/chat/completions (SSE or JSON), /v1/models, /props, /slots, /tools. Memory and server-side tools are applied
transparently; tool/memory status is streamed as `reasoning_content` (shown in the UI's collapsible block)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aiplatform.api.deps import ChatUser, container, current_principal
from aiplatform.orchestrator.chat_service import ChatCommand, StreamEvent
from aiplatform.security.auth import Principal
from aiplatform.shared.errors import RateLimited, Unauthorized

router = APIRouter(tags=["openai-compatible"])


class ChatCompletionIn(BaseModel):
    """Only the fields we honour are typed; unknown sampling fields from clients are accepted and ignored."""

    model_config = ConfigDict(extra="allow")
    messages: list[dict[str, Any]] = Field(..., min_length=1, max_length=2000)
    model: str | None = None
    stream: bool = False
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    max_tokens: int | None = Field(None, ge=1, le=65536)
    max_completion_tokens: int | None = Field(None, ge=1, le=65536)
    tools: list[dict[str, Any]] | None = Field(None, max_length=64)
    chat_template_kwargs: dict[str, Any] | None = None
    think: bool | None = None
    reasoning_effort: Literal["low", "medium", "high", "none"] | None = None


def _think(body: ChatCompletionIn) -> bool | None:
    if body.think is not None:
        return body.think
    if body.chat_template_kwargs and "enable_thinking" in body.chat_template_kwargs:
        return bool(body.chat_template_kwargs["enable_thinking"])
    if body.reasoning_effort:
        return body.reasoning_effort != "none"
    return None


def _chunk(cid: str, model: str, delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n".encode()


def _timings(done: dict[str, Any]) -> dict[str, Any]:
    u = done.get("usage") or {}
    gen_ms, n = u.get("generation_ms") or 0, u.get("completion_tokens") or 0
    return {
        "prompt_n": u.get("prompt_tokens") or 0,
        "prompt_ms": u.get("prompt_ms") or 0,
        "predicted_n": n,
        "predicted_ms": gen_ms,
        "predicted_per_second": (n / (gen_ms / 1000)) if gen_ms else 0,
    }


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionIn, request: Request, p: Principal = ChatUser) -> Any:
    c = container(request)
    c.chat_limiter.check(str(p.user_id))
    conv_hdr = request.headers.get("x-conversation-id")
    try:
        conv_id = UUID(conv_hdr) if conv_hdr else None
    except ValueError:
        conv_id = None
    ua = request.headers.get("user-agent", "")
    cmd = ChatCommand(
        principal=p,
        messages=body.messages,
        client="webui" if "Mozilla" in ua else "api",
        conversation_id=conv_id,
        temperature=body.temperature,
        top_p=body.top_p,
        max_tokens=body.max_completion_tokens or body.max_tokens,
        think=_think(body),
        client_tools=body.tools,
    )
    model = c.runtime.params().model
    cid = "chatcmpl-" + c.new_id()
    events = c.chat.handle(cmd)

    if not body.stream:
        content, reasoning, done, error = [], [], {}, None
        async for ev in events:
            if ev.kind == "content":
                content.append(ev.text)
            elif ev.kind == "reasoning":
                reasoning.append(ev.text)
            elif ev.kind == "done":
                done = ev.data
            elif ev.kind == "error":
                error = ev.text
        if error and not content:
            return JSONResponse({"error": {"code": "model_error", "message": error}}, status_code=502)
        u = done.get("usage") or {}
        msg: dict[str, Any] = {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}
        if done.get("tool_calls"):
            msg["tool_calls"] = [
                {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["arguments"]}}
                for t in done["tool_calls"]
            ]
        return {
            "id": cid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": done.get("finish_reason", "stop")}],
            "usage": {
                "prompt_tokens": u.get("prompt_tokens"),
                "completion_tokens": u.get("completion_tokens"),
                "total_tokens": (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0),
            },
            "timings": _timings(done),
            "aip": {k: done.get(k) for k in ("conversation_id", "stages", "context", "memories")},
        }

    async def sse() -> AsyncIterator[bytes]:
        yield _chunk(cid, model, {"role": "assistant", "content": ""})
        ev: StreamEvent
        async for ev in events:
            if ev.kind == "content":
                yield _chunk(cid, model, {"content": ev.text})
            elif ev.kind == "reasoning":
                yield _chunk(cid, model, {"reasoning_content": ev.text})
            elif ev.kind == "error":
                yield _chunk(cid, model, {"content": f"\n\n⚠️ {ev.text}"}, "stop")
                yield b"data: [DONE]\n\n"
                return
            elif ev.kind == "done":
                d = ev.data
                u = d.get("usage") or {}
                delta: dict[str, Any] = {}
                if d.get("tool_calls"):
                    delta["tool_calls"] = [
                        {
                            "index": i,
                            "id": t["id"],
                            "type": "function",
                            "function": {"name": t["name"], "arguments": t["arguments"]},
                        }
                        for i, t in enumerate(d["tool_calls"])
                    ]
                yield _chunk(
                    cid,
                    model,
                    delta,
                    d.get("finish_reason", "stop"),
                    usage={
                        "prompt_tokens": u.get("prompt_tokens"),
                        "completion_tokens": u.get("completion_tokens"),
                        "total_tokens": (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0),
                    },
                    timings=_timings(d),
                    aip={k: d.get(k) for k in ("conversation_id", "stages", "context", "memories", "tools_called")},
                )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/v1/models")
async def models(request: Request, p: Principal = ChatUser) -> dict[str, Any]:
    params = container(request).runtime.params()
    return {
        "object": "list",
        "data": [
            {"id": params.model, "object": "model", "created": 0, "owned_by": params.provider, "meta": {"n_ctx": params.num_ctx}}
        ],
        "models": [{"name": params.model, "model": params.model, "capabilities": ["completion", "tools"]}],
    }


@router.get("/props")
async def props(request: Request) -> dict[str, Any]:
    # The UI calls /props before login state is known; answer 401 so it prompts for an API key / login.
    if await current_principal(request) is None:
        raise Unauthorized("login at /admin/login or set an API token in the web UI settings")
    params = container(request).runtime.params()
    return {
        "default_generation_settings": {
            "params": {
                "temperature": params.temperature,
                "top_p": params.top_p,
                "top_k": 20,
                "min_p": 0.0,
                "n_predict": params.max_output_tokens,
                "max_tokens": params.max_output_tokens,
            },
            "n_ctx": params.num_ctx,
        },
        "total_slots": 1,
        "model_alias": params.model,
        "model_path": params.model,
        "model_ftype": "",
        "modalities": {"vision": False, "video": False, "audio": False},
        "endpoint_slots": False,
        "endpoint_props": False,
        "endpoint_metrics": False,
        "ui": True,
        "ui_settings": {},
        "chat_template": "",
        "build_info": "aiplatform (memory + tools) over " + params.provider,
    }


@router.get("/slots")
async def slots(p: Principal = ChatUser) -> list[Any]:
    return []


@router.get("/tools")
async def ui_tools(p: Principal = ChatUser) -> list[Any]:
    """The UI's own 'server tools' list. Our tools run server-side automatically, so the UI gets none to call."""
    return []


class _Limiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        h = [t for t in self.hits.get(key, []) if now - t < 60]
        if len(h) >= self.per_minute:
            raise RateLimited("too many chat requests per minute")
        h.append(now)
        self.hits[key] = h


ChatLimiter = _Limiter
