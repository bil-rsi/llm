"""Tool abstractions. Every tool: strict argument model → prepare (canonicalise + permission request) → run.

Model-generated arguments are untrusted: they are parsed with extra="forbid", bounded, canonicalised by the tool, and
only then evaluated by the permission engine. Tools raise GuardrailViolation for hard limits that no mode can lift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aiplatform.permissions.engine import PermissionRequest
from aiplatform.permissions.models import PermissionSnapshot, Risk

Category = Literal["filesystem", "shell", "web", "memory"]


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class ToolInputError(Exception):
    """Arguments are well-formed JSON but make no sense (missing file, bad pattern...). Not a security event."""


class GuardrailViolation(Exception):
    """A hard limit (outside mounted roots, internal network, protected path...). Always blocked and audited."""

    def __init__(self, reason: str, code: str = "guardrail") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class TaintState:
    """Tracks whether this turn has ingested untrusted content (web pages, files, tool-sourced memories)."""

    sources: list[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        return bool(self.sources)

    def mark(self, source: str) -> None:
        if source not in self.sources:
            self.sources.append(source)


@dataclass
class ToolContext:
    user_id: UUID
    conversation_id: UUID | None
    model_request_id: UUID | None
    snapshot: PermissionSnapshot
    taint: TaintState
    user_text: str = ""  # the current user message (e.g. memory.save checks for an explicit "remember")


@dataclass
class Prepared:
    request: PermissionRequest
    canonical: dict[str, Any]  # canonical arguments shown in approvals/audit
    summary: str  # one-line human description, e.g. "write projects/app/main.py (1.2 KB)"
    payload: Any = None  # tool-private data for run()


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any]
    summary: str
    untrusted_source: str | None = None  # set when data contains outside content (marks the turn tainted)
    detail_kind: Literal["filesystem", "web"] | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    """Read-only protocol members so concrete tools can declare them as plain class attributes."""

    @property
    def name(self) -> str: ...
    @property
    def category(self) -> Category: ...
    @property
    def description(self) -> str: ...
    @property
    def risk(self) -> Risk: ...  # base risk for the catalogue; prepare() may report a higher per-call risk

    @property
    def Args(self) -> type[ToolArgs]: ...
    @property
    def timeout_s(self) -> float: ...

    def prepare(self, args: Any, ctx: ToolContext) -> Prepared: ...

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult: ...

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult: ...


def model_tool_name(name: str) -> str:
    """Names shown to the model use underscores (some chat templates reject dots): filesystem.read → filesystem_read."""
    return name.replace(".", "_", 1)


def json_schema(args: type[ToolArgs]) -> dict[str, Any]:
    s = args.model_json_schema()
    s.pop("title", None)
    for p in s.get("properties", {}).values():
        p.pop("title", None)
    s["additionalProperties"] = False
    return s
