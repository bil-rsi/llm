"""Filesystem tools end to end (real temp directories): capability checks, backups, soft delete, dry run."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from aiplatform.config import FilesystemSettings
from aiplatform.permissions.engine import PermissionEngine
from aiplatform.permissions.models import Root
from aiplatform.tools.base import GuardrailViolation, TaintState, ToolContext, ToolInputError
from aiplatform.tools.filesystem.tools import FsContext, filesystem_tools
from tests.conftest import snapshot

E = PermissionEngine()


def tools(workspace: tuple[Path, tuple[Root, ...]]) -> dict[str, Any]:
    return {t.name: t for t in filesystem_tools(FsContext(FilesystemSettings(), "C:/AIWorkspace"))}


def ctx(roots: tuple[Root, ...], mode: str = "normal") -> ToolContext:
    return ToolContext(uuid.uuid4(), None, None, snapshot(mode, roots=roots), TaintState())


async def run(tool: Any, args: dict[str, Any], c: ToolContext) -> tuple[str, Any]:
    prepared = tool.prepare(tool.Args.model_validate(args), c)
    decision = E.decide(prepared.request, c.snapshot, tainted=c.taint.tainted)
    if decision.effect != "allow":
        return decision.effect, decision
    return "allow", await tool.run(prepared, c)


async def test_write_read_modify_delete_cycle(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    base, roots = workspace
    t = tools(workspace)
    c = ctx(roots, "bypass")
    eff, res = await run(t["filesystem.write"], {"path": "sandbox/app/a.txt", "content": "hello world"}, c)
    assert eff == "allow" and res.ok and (base / "sandbox/app/a.txt").read_text() == "hello world"
    eff, res = await run(
        t["filesystem.modify"], {"path": "sandbox/app/a.txt", "edits": [{"find": "world", "replace": "there"}]}, c
    )
    assert res.ok and (base / "sandbox/app/a.txt").read_text() == "hello there"
    assert res.data["backup"] and (base / "sandbox" / res.data["backup"]).read_text() == "hello world"
    eff, res = await run(t["filesystem.read"], {"path": "sandbox/app/a.txt"}, c)
    assert res.data["content"] == "hello there"
    eff, res = await run(t["filesystem.delete"], {"path": "sandbox/app/a.txt"}, c)
    assert res.ok and not (base / "sandbox/app/a.txt").exists()
    assert (base / "sandbox" / res.data["trash"]).read_text() == "hello there"  # recoverable


async def test_read_only_root_cannot_be_written_even_in_bypass(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    eff, d = await run(tools(workspace)["filesystem.write"], {"path": "allowed/x.txt", "content": "x"}, ctx(roots, "bypass"))
    assert eff == "deny" and "read-only" in d.reason


async def test_normal_mode_write_needs_confirmation(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    eff, _ = await run(tools(workspace)["filesystem.write"], {"path": "projects/x.txt", "content": "x"}, ctx(roots))
    assert eff == "confirm"


async def test_autonomous_blocks_delete_outside_rules(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    base, roots = workspace
    (base / "projects" / "keep.txt").write_text("k")
    eff, _ = await run(tools(workspace)["filesystem.delete"], {"path": "projects/keep.txt"}, ctx(roots, "autonomous"))
    assert eff == "deny" and (base / "projects" / "keep.txt").exists()


async def test_executable_extensions_blocked_outside_bypass(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    for name in ("evil.ps1", "run.bat", "x.EXE", "a.lnk"):
        eff, _ = await run(
            tools(workspace)["filesystem.write"], {"path": f"sandbox/{name}", "content": "x"}, ctx(roots, "autonomous")
        )
        assert eff == "deny"


async def test_dry_run_changes_nothing(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    base, roots = workspace
    (base / "sandbox" / "f.txt").write_text("abc")
    t = tools(workspace)["filesystem.modify"]
    c = ctx(roots, "bypass")
    prepared = t.prepare(
        t.Args.model_validate({"path": "sandbox/f.txt", "edits": [{"find": "b", "replace": "X"}], "dry_run": True}), c
    )
    res = await t.preview(prepared, c)
    assert "-abc" in res.data["diff"] and "+aXc" in res.data["diff"] and (base / "sandbox/f.txt").read_text() == "abc"


async def test_internal_dir_hidden_and_protected(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    t = tools(workspace)
    c = ctx(roots, "bypass")
    await run(t["filesystem.write"], {"path": "sandbox/a.txt", "content": "1"}, c)
    await run(t["filesystem.write"], {"path": "sandbox/a.txt", "content": "2"}, c)
    _, listing = await run(t["filesystem.list"], {"path": "sandbox", "depth": 4, "include_hidden": True}, c)
    assert all(".aiplatform" not in e["path"] for e in listing.data["entries"])
    with pytest.raises(GuardrailViolation):
        t["filesystem.read"].prepare(t["filesystem.read"].Args.model_validate({"path": "sandbox/.aiplatform/versions"}), c)


async def test_delete_wildcards_and_roots_refused(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    t = tools(workspace)["filesystem.delete"]
    c = ctx(roots, "bypass")
    with pytest.raises(ToolInputError):
        t.prepare(t.Args.model_validate({"path": "sandbox/*"}), c)
    with pytest.raises(GuardrailViolation):
        t.prepare(t.Args.model_validate({"path": "sandbox", "recursive": True}), c)


async def test_unknown_args_rejected() -> None:
    from pydantic import ValidationError

    from aiplatform.tools.filesystem.tools import ReadArgs

    with pytest.raises(ValidationError):
        ReadArgs.model_validate({"path": "sandbox/x", "follow_symlinks": True})
