"""OpenAICompatProvider: /v1/chat/completions SSE (llama.cpp llama-server, vLLM, LM Studio, other OpenAI-style APIs)."""

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
        d["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
        ]
    if m.role == "tool":
        d["tool_call_id"] = m.tool_call_id or ""
    return d


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None,
        connect_timeout: float,
        read_timeout: float,
        retries: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.retries = retries
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30, pool=10),
        )

    def _body(self, req: ChatRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [_msg(m) for m in req.messages],
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_tokens": req.max_tokens,
            "chat_template_kwargs": {"enable_thinking": req.think},
        }
        if req.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools
            ]
        if req.json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": req.json_schema}}
        if req.stop:
            body["stop"] = req.stop
        return body

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        partial: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish: str | None = None
        async with open_with_retry(self._http, "POST", "/chat/completions", json=self._body(req), retries=self.retries) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except ValueError as e:
                    raise ProviderError(f"bad SSE chunk: {data[:200]}") from e
                if "error" in d:
                    raise ProviderError(str(d["error"])[:500])
                if u := d.get("usage"):
                    usage.prompt_tokens = u.get("prompt_tokens")
                    usage.completion_tokens = u.get("completion_tokens")
                if t := d.get("timings"):
                    usage.prompt_ms = t.get("prompt_ms")
                    usage.generation_ms = t.get("predicted_ms")
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    for tc in delta.get("tool_calls") or []:
                        slot = partial.setdefault(int(tc.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    content, reasoning = delta.get("content") or "", delta.get("reasoning_content") or ""
                    if content or reasoning:
                        yield ChatChunk(content=content, reasoning=reasoning)
        calls = [
            ToolCall(id=s["id"] or "call_" + secrets.token_hex(6), name=s["name"], arguments=s["arguments"] or "{}")
            for _, s in sorted(partial.items())
        ]
        yield ChatChunk(done=True, tool_calls=calls, finish_reason="tool_calls" if calls else (finish or "stop"), usage=usage)

    async def health(self) -> ProviderHealth:
        try:
            r = await self._http.get("/models", timeout=3)
            ids = [m.get("id") for m in r.json().get("data", [])]
            ok = r.status_code == 200 and (self.model in ids or not ids)
            return ProviderHealth(ok, self.name, self.model, "ok" if ok else f"model {self.model} not served ({ids})", ok)
        except (httpx.HTTPError, ValueError) as e:
            return ProviderHealth(False, self.name, self.model, f"unreachable: {type(e).__name__}")

    async def aclose(self) -> None:
        await self._http.aclose()
