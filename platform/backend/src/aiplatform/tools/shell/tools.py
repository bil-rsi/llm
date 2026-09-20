"""shell.execute: runs commands in the isolated tool-runner container over a unix socket."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from aiplatform.config import ShellSettings
from aiplatform.permissions.engine import PermissionRequest
from aiplatform.permissions.models import Decision, Risk
from aiplatform.tools.base import GuardrailViolation, Prepared, ToolArgs, ToolContext, ToolInputError, ToolResult
from aiplatform.tools.filesystem.paths import PathResolver
from aiplatform.tools.shell.rules import CommandRejected, validate_restricted


class RunnerClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    async def run(self, request: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path), limit=4 << 20), 5)
        except (OSError, TimeoutError) as e:
            raise ToolInputError("the shell sandbox (tool-runner container) is not running") from e
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_s + 10)
        finally:
            writer.close()
        resp: dict[str, Any] = json.loads(line or b"{}")
        return resp

    async def health(self) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path)), 2)
            w.close()
            return True
        except (OSError, TimeoutError):
            return False


class ShellArgs(ToolArgs):
    command: list[str] | None = Field(
        None, min_length=1, max_length=64, description='program and arguments, e.g. ["git", "log", "-5", "--oneline"]'
    )
    script: str | None = Field(
        None, min_length=1, max_length=8192, description="shell script run with sh -c (only available in Bypass mode)"
    )
    cwd: str = Field("sandbox", max_length=1024, description="working folder, e.g. projects/app")
    timeout_s: int = Field(10, ge=1, le=300)

    @model_validator(mode="after")
    def _one(self) -> ShellArgs:
        if (self.command is None) == (self.script is None):
            raise ValueError("give exactly one of command (argv list) or script")
        return self


@dataclass
class ShellContext:
    settings: ShellSettings
    client: RunnerClient  # restricted runner: workspace mounted read-only
    workspace_host_dir: str
    broad_client: RunnerClient | None = None  # Bypass broad-shell runner: read-write


class ShellExecute:
    name = "shell.execute"
    category: ClassVar[Literal["shell"]] = "shell"
    description = (
        "Run a command in an isolated Linux sandbox that only sees the workspace folders (no network). "
        "Outside Bypass mode only read-only commands are allowed: ls, cat, grep, find, git log/diff/status, "
        "wc, head, tail, diff, tree, jq... Pass the program and arguments as a list."
    )
    risk: ClassVar[Risk] = "execute"
    Args = ShellArgs
    timeout_s: ClassVar[float] = 320.0

    def __init__(self, sh: ShellContext) -> None:
        self.sh = sh

    def prepare(self, args: ShellArgs, ctx: ToolContext) -> Prepared:
        rp = PathResolver(ctx.snapshot.usable_roots(), self.sh.workspace_host_dir).resolve(args.cwd, must_exist=True)
        broad = ctx.snapshot.bypass_on("broad_shell")
        cap: Decision | None = None
        mode = "broad" if broad else "restricted"
        cmd_name = "sh" if args.script else (args.command or [""])[0]
        if args.script is not None and not broad:
            cap = Decision("deny", "capability:shell_script", "scripts (sh -c) need Bypass mode with broad shell enabled")
        elif args.command is not None and not broad:
            try:
                validate_restricted(list(args.command))
            except CommandRejected as e:
                cap = Decision("deny", "capability:command", str(e))
        timeout = min(args.timeout_s, self.sh.settings.max_timeout_s if not broad else 300)
        shown = args.script if args.script is not None else " ".join(args.command or [])
        req = {
            "argv": list(args.command) if args.command else None,
            "shell": args.script,
            "cwd": rp.abs,
            "timeout_s": timeout,
            "mode": mode,
            "max_output": self.sh.settings.max_output_bytes,
        }
        return Prepared(
            PermissionRequest(self.name, "execute", {"command": cmd_name, "path": rp.display}, cap, taint_sensitive=True),
            {"command": shown[:2000], "cwd": rp.display, "mode": mode, "timeout_s": timeout},
            f"run `{shown[:120]}` in {rp.display}",
            (req, rp),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        req, rp = prepared.payload
        client = self.sh.broad_client if req["mode"] == "broad" and self.sh.broad_client else self.sh.client
        resp = await client.run(req, req["timeout_s"])
        if "rejected" in resp:
            raise GuardrailViolation(f"sandbox rejected the command: {resp['rejected']}", "runner_rejected")
        ok = resp.get("exit_code") == 0
        taint = None if rp.root.name == "sandbox" else f"shell:{rp.display}"
        return ToolResult(
            ok,
            {
                "exit_code": resp.get("exit_code"),
                "stdout": resp.get("stdout", ""),
                "stderr": resp.get("stderr", ""),
                "truncated": resp.get("truncated", False),
                "timed_out": resp.get("timed_out", False),
            },
            f"exit {resp.get('exit_code')} in {resp.get('duration_ms')} ms",
            untrusted_source=taint,
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            True, {"dry_run": True, "would": prepared.summary, **prepared.canonical}, f"dry run: {prepared.summary}"
        )
