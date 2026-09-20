"""In-process domain event bus (Observer). Handlers run as background tasks so publishers never wait on them."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger("events")


@dataclass(frozen=True)
class TurnCompleted:
    user_id: UUID
    conversation_id: UUID
    user_message_id: int
    user_text: str
    assistant_text: str


@dataclass(frozen=True)
class MemoryChanged:
    user_id: UUID
    memory_id: UUID
    change: str


@dataclass(frozen=True)
class PermissionsChanged:
    by_user: UUID | None
    what: str


Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: object) -> None:
        for h in self._handlers.get(type(event), []):
            t = asyncio.create_task(self._run(h, event))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def publish_and_wait(self, event: object) -> None:
        await asyncio.gather(*(self._run(h, event) for h in self._handlers.get(type(event), [])))

    @staticmethod
    async def _run(h: Handler, event: object) -> None:
        try:
            await h(event)
        except Exception:
            log.exception("event_handler_failed", event=type(event).__name__, handler=getattr(h, "__qualname__", str(h)))

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
