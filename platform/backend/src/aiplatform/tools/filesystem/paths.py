"""Path canonicalisation and sandbox checks for model-supplied paths.

Accepted inputs (case-insensitive prefixes):
    projects/app/main.py            root-relative (first component = root name)
    /workspace/projects/app/main.py container path
    C:\\AIWorkspace\\projects\\app\\main.py  host path of the workspace or of an extra root
Everything is resolved with realpath and must stay inside one usable root. Symlinks may not escape it.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from aiplatform.permissions.models import Root
from aiplatform.tools.base import GuardrailViolation, ToolInputError

INTERNAL_DIR = ".aiplatform"
MAX_PATH = 1024
MAX_COMPONENT = 255
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ResolvedPath:
    root: Root
    rel: str  # POSIX, relative to root ("" = the root itself)
    abs: str  # absolute container path after realpath

    @property
    def display(self) -> str:
        return f"{self.root.name}/{self.rel}" if self.rel else self.root.name

    @property
    def host(self) -> str:
        base = self.root.host_path.replace("/", "\\")
        return base + ("\\" + self.rel.replace("/", "\\") if self.rel else "")


class PathResolver:
    def __init__(self, roots: tuple[Root, ...], workspace_host_dir: str, workspace_mount: str = "/workspace") -> None:
        self.roots = roots
        self.workspace_host = workspace_host_dir.replace("\\", "/").rstrip("/").lower()
        self.workspace_mount = workspace_mount

    def resolve(self, raw: str, *, must_exist: bool = False) -> ResolvedPath:
        text = _reject_bad_forms(raw)
        root, rel = self._split(text)
        rel = _normalise_rel(rel)
        if rel.split("/")[0] == INTERNAL_DIR:
            raise GuardrailViolation(f"{INTERNAL_DIR} holds versions and trash and is managed by the platform", "internal_dir")
        base = os.path.realpath(root.container_path)
        joined = os.path.join(base, rel) if rel else base
        real = os.path.realpath(joined)
        if os.path.commonpath([real, base]) != base:
            raise GuardrailViolation("path resolves outside its root (symlink or traversal)", "path_escape")
        if real != os.path.normpath(joined):
            # A symlink somewhere inside the root is fine only if it stays inside the same root.
            real_rel = os.path.relpath(real, base).replace(os.sep, "/")
            rel = "" if real_rel == "." else real_rel
        if must_exist and not os.path.lexists(real):
            raise ToolInputError(f"not found: {root.name}/{rel}")
        return ResolvedPath(root, rel, real)

    def _split(self, text: str) -> tuple[Root, str]:
        t = text.replace("\\", "/")
        low = t.lower()
        # 1. host paths: workspace or extra roots
        if _DRIVE.match(t):
            for r in sorted(self.roots, key=lambda r: -len(r.host_path)):
                hp = self._host_of(r)
                if low == hp or low.startswith(hp + "/"):
                    return r, t[len(hp) + 1 :] if len(t) > len(hp) else ""
            raise GuardrailViolation("path is outside the folders the AI may use", "outside_roots")
        # 2. container paths
        if t.startswith("/"):
            for r in sorted(self.roots, key=lambda r: -len(r.container_path)):
                cp = r.container_path.rstrip("/")
                if t == cp or t.startswith(cp + "/"):
                    return r, t[len(cp) + 1 :]
            raise GuardrailViolation("path is outside the folders the AI may use", "outside_roots")
        # 3. root-relative
        first, _, rest = t.partition("/")
        for r in self.roots:
            if r.name == first.lower():
                return r, rest
        names = ", ".join(r.name for r in self.roots)
        raise ToolInputError(f"path must start with a root name ({names}) or C:\\AIWorkspace\\<root>")

    def _host_of(self, r: Root) -> str:
        if r.kind == "zone":
            return f"{self.workspace_host}/{r.host_path.strip('/').lower()}"
        return r.host_path.replace("\\", "/").rstrip("/").lower()


def _reject_bad_forms(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolInputError("path is empty")
    text = unicodedata.normalize("NFC", raw.strip())
    if len(text) > MAX_PATH:
        raise GuardrailViolation("path too long", "path_form")
    if _CONTROL.search(text):
        raise GuardrailViolation("control characters in path", "path_form")
    if re.search(r"%[0-9a-fA-F]{2}", text):
        raise GuardrailViolation("URL-encoded characters are not allowed in paths", "path_form")
    t = text.replace("\\", "/")
    if t.startswith("//") or t.startswith("/?") or t.startswith("/."):
        raise GuardrailViolation("UNC, device and namespace paths are not allowed", "path_form")
    if t.startswith("~"):
        raise GuardrailViolation("home-relative paths are not allowed", "path_form")
    if ".." in t.split("/"):
        raise GuardrailViolation("'..' is not allowed in paths", "path_traversal")
    body = t[2:] if _DRIVE.match(t) else t
    if ":" in body:
        raise GuardrailViolation("':' is not allowed in paths (alternate data streams)", "path_form")
    return text


def _normalise_rel(rel: str) -> str:
    parts: list[str] = []
    for comp in rel.replace("\\", "/").split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise GuardrailViolation("'..' is not allowed in paths", "path_traversal")
        if len(comp) > MAX_COMPONENT:
            raise GuardrailViolation("path component too long", "path_form")
        if comp != comp.rstrip(" ."):
            raise GuardrailViolation("path components may not end with a dot or space", "path_form")
        if _RESERVED.match(comp):
            raise GuardrailViolation(f"{comp!r} is a reserved Windows device name", "path_form")
        if any(c in comp for c in '<>"|?*'):
            raise GuardrailViolation('characters <>"|?* are not allowed in file names', "path_form")
        parts.append(comp)
    return str(PurePosixPath(*parts)) if parts else ""
