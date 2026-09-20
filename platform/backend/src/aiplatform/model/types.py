"""Provider-neutral chat types. Business logic only sees these; adapters translate to Ollama / OpenAI wire formats."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text exactly as produced by the model (untrusted)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ChatMessage:
    role: Role
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ChatRequest:
    messages: list[ChatMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.7
    top_p: float = 0.8
    max_tokens: int = 4096
    num_ctx: int = 16384
    think: bool = False
    json_schema: dict[str, Any] | None = None
    stop: list[str] | None = None


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_ms: float | None = None
    generation_ms: float | None = None
    load_ms: float | None = None


@dataclass
class ChatChunk:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # only on the final chunk, complete
    done: bool = False
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True)
class ProviderHealth:
    ok: bool
    provider: str
    model: str
    detail: str = ""
    loaded: bool | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None: ...


class EmbeddingProvider(Protocol):
    model: str
    dims: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def health(self) -> bool: ...


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
