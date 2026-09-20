"""Filesystem sandbox: malicious path corpus + property test. Nothing may resolve outside a usable root."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aiplatform.permissions.models import Root
from aiplatform.tools.base import GuardrailViolation, ToolInputError
from aiplatform.tools.filesystem.paths import PathResolver

MALICIOUS = [
    "../../../../Windows/System32/",
    "..\\..\\..\\Windows\\System32\\config\\SAM",
    "projects/../../etc/passwd",
    "projects/..\\..\\secrets",
    "projects/%2e%2e/%2e%2e/etc/passwd",
    "projects/%2E%2E%2Fsecrets",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "C:/Users/Administrator/.ssh/id_rsa",
    "D:\\anything",
    "\\\\attacker\\share\\file",
    "//attacker/share/file",
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\.\\PhysicalDrive0",
    "/etc/passwd",
    "/run/secrets/pg_app",
    "/proc/self/environ",
    "/workspace/../run/secrets/pg_app",
    "projects/file.txt:hidden_stream",
    "projects/CON",
    "projects/nul.txt",
    "projects/COM1",
    "projects/LPT9.log",
    "projects/evil. ",
    "projects/evil.",
    "projects/a\x00b",
    "projects/line\nbreak",
    "~/.bashrc",
    "projects/.aiplatform/versions/x",
    "projects/" + "a" * 300,
    "sandbox/" + "x/" * 600,
]


def resolver(roots: tuple[Root, ...], host: str) -> PathResolver:
    return PathResolver(roots, host)


@pytest.mark.parametrize("raw", MALICIOUS)
def test_malicious_paths_rejected(workspace: tuple[Path, tuple[Root, ...]], raw: str) -> None:
    base, roots = workspace
    with pytest.raises((GuardrailViolation, ToolInputError)):
        resolver(roots, str(base)).resolve(raw)


def test_valid_forms_resolve_inside_root(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    base, roots = workspace
    r = resolver(roots, "C:/AIWorkspace")
    assert r.resolve("projects/app/main.py").abs == str(base / "projects" / "app" / "main.py")
    assert r.resolve("C:\\AIWorkspace\\projects\\app\\main.py").display == "projects/app/main.py"
    assert r.resolve("c:/aiworkspace/Sandbox/x.txt").root.name == "sandbox"
    assert r.resolve(str(base / "projects" / "x")).rel == "x"
    assert r.resolve("projects/./a/./b").rel == "a/b"


def test_symlink_escape_blocked(workspace: tuple[Path, tuple[Root, ...]], tmp_path: Path) -> None:
    base, roots = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    os.symlink(outside, base / "projects" / "link")
    with pytest.raises(GuardrailViolation):
        resolver(roots, "C:/AIWorkspace").resolve("projects/link/secret.txt")


def test_symlink_across_roots_blocked(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    base, roots = workspace
    os.symlink(base / "projects", base / "sandbox" / "toprojects")
    with pytest.raises(GuardrailViolation):
        resolver(roots, "C:/AIWorkspace").resolve("sandbox/toprojects/x")


def test_unusable_root_not_reachable(workspace: tuple[Path, tuple[Root, ...]]) -> None:
    _, roots = workspace
    only_sandbox = tuple(r for r in roots if r.name == "sandbox")
    with pytest.raises((GuardrailViolation, ToolInputError)):
        resolver(only_sandbox, "C:/AIWorkspace").resolve("projects/x")


@settings(max_examples=400, deadline=None)
@given(st.lists(st.sampled_from(["..", ".", "a", "b", "..\\", "%2e", "/", "\\", "c:", "~", "x y", "..."]), max_size=12))
def test_property_never_escapes(parts: list[str]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Root("sandbox", "sandbox", os.path.join(d, "sandbox"), "rw", "zone")
        os.makedirs(root.container_path)
        raw = "sandbox/" + "/".join(parts)
        try:
            rp = PathResolver((root,), "C:/AIWorkspace").resolve(raw)
        except (GuardrailViolation, ToolInputError):
            return
        base = os.path.realpath(root.container_path)
        assert os.path.commonpath([rp.abs, base]) == base
