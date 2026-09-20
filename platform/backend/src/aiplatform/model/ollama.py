"""OllamaProvider: /api/chat (NDJSON streaming, native tool calls, `think`, options.num_ctx)."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aiplatform.model.retry import open_with_retry
from aiplatform.model.types import ChatChunk, ChatMessage, ChatRequest, ProviderError, ProviderHealth, ToolCall, Usage


def _msg(m: ChatMessage) -> dict[str, Any]:
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [{"function": {"name": c.name, "arguments": _args_obj(c.arguments)}} for c in m.tool_calls]
    if m.role == "tool" and m.name:
        d["tool_name"] = m.name
    return d


def _args_obj(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return {}


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        connect_timeout: float,
        read_timeout: float,
        retries: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.retries = retries
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30, pool=10),
        )

    def _body(self, req: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "think": req.think,
            "keep_alive": "30m",
            "messages": [_msg(m) for m in req.messages],
            "options": {
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_ctx": req.num_ctx,
                "num_predict": req.max_tokens,
            },
        }
        if req.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools
            ]
        if req.json_schema is not None:
            body["format"] = req.json_schema
        if req.stop:
            body["options"]["stop"] = req.stop
        return body

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        body = self._body(req)
        async with open_with_retry(self._http, "POST", "/api/chat", json=body, retries=self.retries) as resp:
            calls: list[ToolCall] = []
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError as e:
                    raise ProviderError(f"bad NDJSON from Ollama: {line[:200]}") from e
                if "error" in d:
                    raise ProviderError(str(d["error"])[:500])
                m = d.get("message") or {}
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    calls.append(
                        ToolCall(
                            id=c.get("id") or "call_" + secrets.token_hex(6),
                            name=str(fn.get("name", "")),
                            arguments=json.dumps(fn.get("arguments") or {}),
                        )
                    )
                if d.get("done"):
                    yield ChatChunk(
                        content=m.get("content") or "",
                        reasoning=m.get("thinking") or "",
                        tool_calls=calls,
                        done=True,
                        finish_reason="tool_calls" if calls else d.get("done_reason", "stop"),
                        usage=Usage(
                            prompt_tokens=d.get("prompt_eval_count"),
                            completion_tokens=d.get("eval_count"),
                            prompt_ms=_ns_ms(d.get("prompt_eval_duration")),
                            generation_ms=_ns_ms(d.get("eval_duration")),
                            load_ms=_ns_ms(d.get("load_duration")),
                        ),
                    )
                    return
                if m.get("content") or m.get("thinking"):
                    yield ChatChunk(content=m.get("content") or "", reasoning=m.get("thinking") or "")
        raise ProviderError("Ollama stream ended without done=true", retryable=False)

    async def health(self) -> ProviderHealth:
        try:
            tags = (await self._http.get("/api/tags", timeout=3)).json()
            names = {m.get("name", "").split(":")[0] for m in tags.get("models", [])} | {
                m.get("name", "") for m in tags.get("models", [])
            }
            if self.model not in names:
                return ProviderHealth(False, self.name, self.model, f"model {self.model} not found in Ollama")
            ps = (await self._http.get("/api/ps", timeout=3)).json()
            loaded = any(m.get("name", "").split(":")[0] == self.model.split(":")[0] for m in ps.get("models", []))
            return ProviderHealth(True, self.name, self.model, "ok", loaded)
        except (httpx.HTTPError, ValueError) as e:
            return ProviderHealth(False, self.name, self.model, f"unreachable: {type(e).__name__}")

    async def aclose(self) -> None:
        await self._http.aclose()


def _ns_ms(ns: Any) -> float | None:
    return ns / 1e6 if isinstance(ns, int | float) else None
