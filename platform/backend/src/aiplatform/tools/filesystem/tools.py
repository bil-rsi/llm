"""Filesystem tools. All I/O runs in a worker thread; every path goes through PathResolver first.

Safety net for destructive operations: overwrite/modify keep a version copy in <root>/.aiplatform/versions, delete is a
move to <root>/.aiplatform/trash (purged after `trash_retention_days`). Both folders are hidden from the model.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field

from aiplatform.config import FilesystemSettings
from aiplatform.permissions.engine import PermissionRequest
from aiplatform.permissions.models import Decision, PermissionSnapshot, Risk
from aiplatform.tools.base import (
    GuardrailViolation,
    Prepared,
    ToolArgs,
    ToolContext,
    ToolInputError,
    ToolResult,
)
from aiplatform.tools.filesystem.paths import INTERNAL_DIR, PathResolver, ResolvedPath

PathStr = Field(min_length=1, max_length=1024, description="e.g. projects/app/main.py or C:\\AIWorkspace\\projects\\app\\main.py")


def _sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


@dataclass
class FsContext:
    settings: FilesystemSettings
    workspace_host_dir: str

    def resolver(self, snap: PermissionSnapshot) -> PathResolver:
        return PathResolver(snap.usable_roots(), self.workspace_host_dir)


class _FsTool:
    category: ClassVar[Literal["filesystem"]] = "filesystem"
    risk: ClassVar[Risk] = "read"
    writes: ClassVar[bool] = False
    timeout_s: ClassVar[float] = 30.0

    def __init__(self, fs: FsContext) -> None:
        self.fs = fs

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _resolve(self, ctx: ToolContext, raw: str, *, must_exist: bool = False) -> ResolvedPath:
        return self.fs.resolver(ctx.snapshot).resolve(raw, must_exist=must_exist)

    def _capability(self, rp: ResolvedPath, ctx: ToolContext, writing: bool) -> Decision | None:
        if writing and rp.root.access == "ro":
            return Decision("deny", "capability:read_only_root", f"{rp.root.name} is read-only for the AI")
        if writing and ctx.snapshot.mode != "bypass":
            ext = os.path.splitext(rp.rel)[1].lower()
            if ext in self.fs.settings.write_denied_extensions:
                return Decision("deny", "capability:extension", f"writing {ext} files is blocked outside Bypass mode")
        return None

    def _request(
        self,
        rp: ResolvedPath,
        ctx: ToolContext,
        *,
        risk: Risk | None = None,
        writing: bool | None = None,
        extra: ResolvedPath | None = None,
    ) -> PermissionRequest:
        w = self.writes if writing is None else writing
        cap = self._capability(rp, ctx, w) or (self._capability(extra, ctx, w) if extra else None)
        return PermissionRequest(self.name, risk or self.risk, {"path": rp.display}, cap, taint_sensitive=w)  # type: ignore[attr-defined]

    def _fs_detail(self, op: str, rp: ResolvedPath, **kw: Any) -> dict[str, Any]:
        return {"operation": op, "root": rp.root.name, "path": rp.rel or ".", **kw}

    def _backup(self, rp: ResolvedPath) -> str | None:
        if not os.path.isfile(rp.abs):
            return None
        vdir = Path(rp.root.container_path) / INTERNAL_DIR / "versions" / os.path.dirname(rp.rel)
        vdir.mkdir(parents=True, exist_ok=True)
        base = os.path.basename(rp.rel)
        dest = vdir / f"{base}.{_stamp()}"
        shutil.copy2(rp.abs, dest)
        olds = sorted(vdir.glob(f"{glob_escape(base)}.*"))
        for old in olds[: max(0, len(olds) - self.fs.settings.versions_keep)]:
            old.unlink(missing_ok=True)
        return str(dest.relative_to(rp.root.container_path))

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            True, {"dry_run": True, "would": prepared.summary, "arguments": prepared.canonical}, f"dry run: {prepared.summary}"
        )


def glob_escape(s: str) -> str:
    return "".join(f"[{c}]" if c in "*?[]" else c for c in s)


def _check_regular_writable(rp: ResolvedPath) -> None:
    try:
        st = os.lstat(rp.abs)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise GuardrailViolation("refusing to write through a symbolic link", "symlink")
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise GuardrailViolation("refusing to modify a hard-linked file", "hardlink")


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".aip-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ─────────────────────────────── read-only tools ───────────────────────────────
class ReadArgs(ToolArgs):
    path: str = PathStr
    offset: int = Field(0, ge=0, description="byte offset to start reading at")
    max_bytes: int = Field(200_000, ge=1, le=1_048_576)


class FileRead(_FsTool):
    name = "filesystem.read"
    description = "Read a text file inside the allowed folders. Returns its content (secrets are redacted)."
    Args = ReadArgs

    def prepare(self, args: ReadArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path, must_exist=True)
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "offset": args.offset, "max_bytes": args.max_bytes},
            f"read {rp.display}",
            rp,
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp: ResolvedPath = prepared.payload
        args = prepared.canonical
        limit = min(args["max_bytes"], self.fs.settings.max_read_bytes)

        def _read() -> tuple[bytes, int]:
            if os.path.isdir(rp.abs):
                raise ToolInputError(f"{rp.display} is a directory; use filesystem_list")
            size = os.path.getsize(rp.abs)
            with open(rp.abs, "rb") as f:
                f.seek(args["offset"])
                return f.read(limit), size

        data, size = await asyncio.to_thread(_read)
        if b"\x00" in data[:8192]:
            return ToolResult(False, {"error": "binary file; only text files can be read", "size": size}, "binary file")
        text = data.decode("utf-8", errors="replace")
        end = args["offset"] + len(data)
        taint = None if rp.root.name == "sandbox" else f"file:{rp.display}"
        return ToolResult(
            True,
            {
                "path": rp.display,
                "size": size,
                "offset": args["offset"],
                "truncated": end < size,
                "next_offset": end if end < size else None,
                "content": text,
            },
            f"read {rp.display} ({len(data)} of {size} bytes)",
            untrusted_source=taint,
            detail_kind="filesystem",
            detail=self._fs_detail("read", rp, bytes=len(data)),
        )


class ListArgs(ToolArgs):
    path: str = PathStr
    depth: int = Field(1, ge=1, le=4)
    include_hidden: bool = False
    max_entries: int = Field(300, ge=1, le=2000)


class FileList(_FsTool):
    name = "filesystem.list"
    description = "List files and folders (name, type, size, modified). Use a root name like 'projects' to start."
    Args = ListArgs

    def prepare(self, args: ListArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path, must_exist=True)
        return Prepared(self._request(rp, ctx), {"path": rp.display, "depth": args.depth}, f"list {rp.display}", (rp, args))

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload

        def _walk() -> tuple[list[dict[str, Any]], bool]:
            if not os.path.isdir(rp.abs):
                raise ToolInputError(f"{rp.display} is not a directory")
            out: list[dict[str, Any]] = []
            base_depth = rp.abs.rstrip("/").count("/")
            for cur, dirs, files in os.walk(rp.abs):
                dirs[:] = sorted(d for d in dirs if d != INTERNAL_DIR and (args.include_hidden or not d.startswith(".")))
                if cur.count("/") - base_depth >= args.depth:
                    dirs[:] = []
                for n in dirs + sorted(files):
                    if n == INTERNAL_DIR or (not args.include_hidden and n.startswith(".")):
                        continue
                    p = os.path.join(cur, n)
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    kind = "dir" if stat.S_ISDIR(st.st_mode) else ("link" if stat.S_ISLNK(st.st_mode) else "file")
                    out.append(
                        {
                            "path": os.path.relpath(p, rp.abs).replace(os.sep, "/"),
                            "type": kind,
                            "size": st.st_size if kind == "file" else None,
                            "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
                        }
                    )
                    if len(out) >= args.max_entries:
                        return out, True
            return out, False

        entries, truncated = await asyncio.to_thread(_walk)
        return ToolResult(
            True,
            {"path": rp.display, "entries": entries, "truncated": truncated},
            f"{rp.display}: {len(entries)} entries",
            detail_kind="filesystem",
            detail=self._fs_detail("list", rp),
        )


class SearchArgs(ToolArgs):
    path: str = PathStr
    name_glob: str | None = Field(None, max_length=200, description="file name pattern, e.g. *.py")
    content: str | None = Field(None, min_length=2, max_length=200, description="case-insensitive text to find")
    max_results: int = Field(100, ge=1, le=500)


class FileSearch(_FsTool):
    name = "filesystem.search"
    description = "Find files by name pattern and/or text content (case-insensitive, literal) under a folder."
    Args = SearchArgs
    timeout_s = 60.0

    def prepare(self, args: SearchArgs, ctx: ToolContext) -> Prepared:
        if not args.name_glob and not args.content:
            raise ToolInputError("give name_glob, content or both")
        rp = self._resolve(ctx, args.path, must_exist=True)
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "name_glob": args.name_glob, "content": args.content},
            f"search {rp.display}",
            (rp, args),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload
        needle = args.content.lower() if args.content else None

        def _search() -> tuple[list[dict[str, Any]], int]:
            hits: list[dict[str, Any]] = []
            scanned = 0
            for cur, dirs, files in os.walk(rp.abs):
                dirs[:] = [d for d in dirs if d != INTERNAL_DIR and not d.startswith(".git")]
                for n in files:
                    if args.name_glob and not fnmatch.fnmatch(n.lower(), args.name_glob.lower()):
                        continue
                    p = os.path.join(cur, n)
                    scanned += 1
                    if scanned > 20000:
                        return hits, scanned
                    rel = os.path.relpath(p, rp.abs).replace(os.sep, "/")
                    if needle is None:
                        hits.append({"path": rel})
                    else:
                        try:
                            if os.path.getsize(p) > 1_048_576:
                                continue
                            with open(p, "rb") as f:
                                data = f.read()
                            if b"\x00" in data[:4096]:
                                continue
                            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                                if needle in line.lower():
                                    hits.append({"path": rel, "line": i, "text": line.strip()[:200]})
                                    if len(hits) >= args.max_results:
                                        return hits, scanned
                        except OSError:
                            continue
                    if len(hits) >= args.max_results:
                        return hits, scanned
            return hits, scanned

        hits, scanned = await asyncio.to_thread(_search)
        taint = None if (rp.root.name == "sandbox" or needle is None) else f"file-search:{rp.display}"
        return ToolResult(
            True,
            {"path": rp.display, "matches": hits, "files_scanned": scanned},
            f"{len(hits)} matches in {rp.display}",
            untrusted_source=taint,
            detail_kind="filesystem",
            detail=self._fs_detail("search", rp),
        )


class OpenArgs(ToolArgs):
    path: str = PathStr


class FileOpen(_FsTool):
    name = "filesystem.open"
    description = (
        "Show a file's details and a short preview, plus its Windows path so the user can open it. Does not launch programs."
    )
    Args = OpenArgs

    def prepare(self, args: OpenArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path, must_exist=True)
        return Prepared(self._request(rp, ctx), {"path": rp.display}, f"open {rp.display}", rp)

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp: ResolvedPath = prepared.payload

        def _info() -> dict[str, Any]:
            st = os.stat(rp.abs)
            info: dict[str, Any] = {
                "path": rp.display,
                "windows_path": rp.host,
                "size": st.st_size,
                "type": "dir" if stat.S_ISDIR(st.st_mode) else "file",
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
            }
            if info["type"] == "file":
                with open(rp.abs, "rb") as f:
                    head = f.read(2000)
                info["preview"] = None if b"\x00" in head else head.decode("utf-8", "replace")
                info["sha256"] = _sha256(rp.abs)
            return info

        info = await asyncio.to_thread(_info)
        return ToolResult(
            True,
            info,
            f"opened {rp.display}",
            untrusted_source=None if rp.root.name == "sandbox" else f"file:{rp.display}",
            detail_kind="filesystem",
            detail=self._fs_detail("open", rp),
        )


# ─────────────────────────────── mutating tools ───────────────────────────────
class CreateArgs(ToolArgs):
    path: str = PathStr
    kind: Literal["file", "directory"] = "file"
    content: str = Field("", max_length=2_000_000)
    dry_run: bool = False


class FileCreate(_FsTool):
    name = "filesystem.create"
    description = "Create a new file (with optional content) or directory. Fails if it already exists."
    risk = "write"
    writes = True
    Args = CreateArgs

    def prepare(self, args: CreateArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path)
        if not rp.rel:
            raise ToolInputError("cannot create a root")
        if os.path.lexists(rp.abs):
            raise ToolInputError(f"{rp.display} already exists")
        size = len(args.content.encode("utf-8"))
        if size > self.fs.settings.max_write_bytes:
            raise ToolInputError(f"content is {size} bytes; limit is {self.fs.settings.max_write_bytes}")
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "kind": args.kind, "bytes": size},
            f"create {args.kind} {rp.display}" + (f" ({size} bytes)" if args.kind == "file" else ""),
            (rp, args),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload

        def _create() -> str | None:
            if args.kind == "directory":
                os.makedirs(rp.abs, exist_ok=False)
                return None
            os.makedirs(os.path.dirname(rp.abs), exist_ok=True)
            fd = os.open(rp.abs, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(args.content.encode("utf-8"))
            return _sha256(rp.abs)

        digest = await asyncio.to_thread(_create)
        return ToolResult(
            True,
            {"path": rp.display, "created": args.kind},
            f"created {rp.display}",
            detail_kind="filesystem",
            detail=self._fs_detail("create", rp, bytes=len(args.content.encode()), sha256_after=digest),
        )


class WriteArgs(ToolArgs):
    path: str = PathStr
    content: str = Field(..., max_length=2_000_000)
    create_dirs: bool = True
    dry_run: bool = False


class FileWrite(_FsTool):
    name = "filesystem.write"
    description = "Write a whole text file (creates it or replaces it; the previous version is kept as a backup)."
    risk = "write"
    writes = True
    Args = WriteArgs

    def prepare(self, args: WriteArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path)
        if not rp.rel or os.path.isdir(rp.abs):
            raise ToolInputError(f"{rp.display} is a directory")
        _check_regular_writable(rp)
        size = len(args.content.encode("utf-8"))
        if size > self.fs.settings.max_write_bytes:
            raise ToolInputError(f"content is {size} bytes; limit is {self.fs.settings.max_write_bytes}")
        exists = os.path.exists(rp.abs)
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "bytes": size, "overwrite": exists},
            f"{'overwrite' if exists else 'write'} {rp.display} ({size} bytes)",
            (rp, args),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload

        def _write() -> tuple[str | None, str | None, str | None]:
            before = _sha256(rp.abs)
            backup = self._backup(rp)
            if args.create_dirs:
                os.makedirs(os.path.dirname(rp.abs), exist_ok=True)
            _atomic_write(rp.abs, args.content.encode("utf-8"))
            return before, _sha256(rp.abs), backup

        before, after, backup = await asyncio.to_thread(_write)
        return ToolResult(
            True,
            {"path": rp.display, "bytes": len(args.content.encode()), "backup": backup},
            f"wrote {rp.display}",
            detail_kind="filesystem",
            detail=self._fs_detail(
                "write", rp, bytes=len(args.content.encode()), sha256_before=before, sha256_after=after, backup_path=backup
            ),
        )


class Edit(ToolArgs):
    find: str = Field(..., min_length=1, max_length=20000)
    replace: str = Field(..., max_length=100000)
    count: int = Field(1, ge=0, le=1000, description="replacements to make; 0 = all occurrences")


class ModifyArgs(ToolArgs):
    path: str = PathStr
    edits: list[Edit] = Field(..., min_length=1, max_length=50)
    dry_run: bool = False


class FileModify(_FsTool):
    name = "filesystem.modify"
    description = "Edit a text file with exact find/replace edits (each 'find' must exist). A backup is kept."
    risk = "write"
    writes = True
    Args = ModifyArgs

    def prepare(self, args: ModifyArgs, ctx: ToolContext) -> Prepared:
        rp = self._resolve(ctx, args.path, must_exist=True)
        _check_regular_writable(rp)
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "edits": len(args.edits)},
            f"modify {rp.display} ({len(args.edits)} edits)",
            (rp, args),
        )

    def _apply(self, rp: ResolvedPath, args: ModifyArgs) -> tuple[str, str, list[int]]:
        with open(rp.abs, "rb") as f:
            raw = f.read(self.fs.settings.max_write_bytes + 1)
        if len(raw) > self.fs.settings.max_write_bytes or b"\x00" in raw[:8192]:
            raise ToolInputError("file is too large or binary")
        original = raw.decode("utf-8")
        text = original
        counts = []
        for i, e in enumerate(args.edits):
            n = text.count(e.find)
            if n == 0:
                raise ToolInputError(f"edit {i + 1}: text to find was not found")
            text = text.replace(e.find, e.replace, e.count if e.count else -1)
            counts.append(min(n, e.count) if e.count else n)
        return original, text, counts

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload
        original, new, counts = await asyncio.to_thread(self._apply, rp, args)
        import difflib

        diff = "".join(difflib.unified_diff(original.splitlines(True), new.splitlines(True), rp.display, rp.display, n=2))
        return ToolResult(True, {"dry_run": True, "replacements": counts, "diff": diff[:20000]}, f"dry run: modify {rp.display}")

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp, args = prepared.payload

        def _do() -> tuple[list[int], str | None, str | None, str | None]:
            original, new, counts = self._apply(rp, args)
            before = hashlib.sha256(original.encode()).hexdigest()
            backup = self._backup(rp)
            _atomic_write(rp.abs, new.encode("utf-8"))
            return counts, before, _sha256(rp.abs), backup

        counts, before, after, backup = await asyncio.to_thread(_do)
        return ToolResult(
            True,
            {"path": rp.display, "replacements": counts, "backup": backup},
            f"modified {rp.display}",
            detail_kind="filesystem",
            detail=self._fs_detail("modify", rp, sha256_before=before, sha256_after=after, backup_path=backup),
        )


class RenameArgs(ToolArgs):
    path: str = PathStr
    new_name: str = Field(..., min_length=1, max_length=255, description="new file/folder name (same folder)")
    dry_run: bool = False


class FileRename(_FsTool):
    name = "filesystem.rename"
    description = "Rename a file or folder within the same folder."
    risk = "write"
    writes = True
    Args = RenameArgs

    def prepare(self, args: RenameArgs, ctx: ToolContext) -> Prepared:
        if "/" in args.new_name or "\\" in args.new_name:
            raise ToolInputError("new_name must be a plain name; use filesystem_move to change folders")
        src = self._resolve(ctx, args.path, must_exist=True)
        if not src.rel:
            raise GuardrailViolation("roots cannot be renamed", "root")
        parent = os.path.dirname(src.rel)
        dst = self._resolve(ctx, f"{src.root.name}/{parent + '/' if parent else ''}{args.new_name}")
        if os.path.lexists(dst.abs):
            raise ToolInputError(f"{dst.display} already exists")
        return Prepared(
            self._request(src, ctx, extra=dst),
            {"path": src.display, "new_path": dst.display},
            f"rename {src.display} → {dst.display}",
            (src, dst),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        src, dst = prepared.payload
        await asyncio.to_thread(os.rename, src.abs, dst.abs)
        return ToolResult(
            True,
            {"path": dst.display},
            f"renamed to {dst.display}",
            detail_kind="filesystem",
            detail=self._fs_detail("rename", src, dest_path=dst.display),
        )


class MoveArgs(ToolArgs):
    path: str = PathStr
    destination: str = Field(..., min_length=1, max_length=1024, description="target folder or full target path")
    overwrite: bool = False
    dry_run: bool = False


class FileMove(_FsTool):
    name = "filesystem.move"
    description = "Move a file or folder to another folder (possibly another root). Overwriting needs overwrite=true."
    risk = "write"
    writes = True
    Args = MoveArgs

    def prepare(self, args: MoveArgs, ctx: ToolContext) -> Prepared:
        src = self._resolve(ctx, args.path, must_exist=True)
        if not src.rel:
            raise GuardrailViolation("roots cannot be moved", "root")
        dst = self._resolve(ctx, args.destination)
        if os.path.isdir(dst.abs):
            dst = self._resolve(ctx, f"{dst.display}/{os.path.basename(src.rel)}")
        if dst.abs == src.abs or dst.abs.startswith(src.abs + "/"):
            raise ToolInputError("cannot move a folder into itself")
        exists = os.path.lexists(dst.abs)
        if exists and not args.overwrite:
            raise ToolInputError(f"{dst.display} exists; set overwrite=true to replace it")
        risk: Risk = "destructive" if exists else "write"
        return Prepared(
            self._request(src, ctx, risk=risk, extra=dst),
            {"path": src.display, "destination": dst.display, "overwrite": exists},
            f"move {src.display} → {dst.display}" + (" (replacing)" if exists else ""),
            (src, dst, exists),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        src, dst, exists = prepared.payload

        def _move() -> str | None:
            backup = None
            if exists:
                backup = _to_trash(dst)
            os.makedirs(os.path.dirname(dst.abs), exist_ok=True)
            shutil.move(src.abs, dst.abs)
            return backup

        backup = await asyncio.to_thread(_move)
        return ToolResult(
            True,
            {"path": dst.display, "replaced_backup": backup},
            f"moved to {dst.display}",
            detail_kind="filesystem",
            detail=self._fs_detail("move", src, dest_path=dst.display, backup_path=backup),
        )


class DeleteArgs(ToolArgs):
    path: str = PathStr
    recursive: bool = Field(False, description="required to delete a non-empty folder")
    dry_run: bool = False


def _to_trash(rp: ResolvedPath) -> str:
    trash = Path(rp.root.container_path) / INTERNAL_DIR / "trash" / _stamp() / os.path.dirname(rp.rel)
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / os.path.basename(rp.rel)
    shutil.move(rp.abs, dest)
    return str(dest.relative_to(rp.root.container_path))


class FileDelete(_FsTool):
    name = "filesystem.delete"
    description = "Delete a file or folder (moved to the platform trash, recoverable for 30 days). No wildcards."
    risk = "destructive"
    writes = True
    Args = DeleteArgs

    def prepare(self, args: DeleteArgs, ctx: ToolContext) -> Prepared:
        if any(c in args.path for c in "*?["):
            raise ToolInputError("wildcards are not supported; delete one path at a time")
        rp = self._resolve(ctx, args.path, must_exist=True)
        if not rp.rel:
            raise GuardrailViolation("a root folder cannot be deleted", "root")
        is_dir = os.path.isdir(rp.abs) and not os.path.islink(rp.abs)
        count = sum(len(f) for _, _, f in os.walk(rp.abs)) if is_dir else 1
        if is_dir and count and not args.recursive:
            raise ToolInputError(f"{rp.display} is a non-empty folder ({count} files); set recursive=true")
        return Prepared(
            self._request(rp, ctx),
            {"path": rp.display, "type": "dir" if is_dir else "file", "files": count},
            f"delete {'folder' if is_dir else 'file'} {rp.display}" + (f" ({count} files)" if is_dir else ""),
            rp,
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        rp: ResolvedPath = prepared.payload
        before = await asyncio.to_thread(_sha256, rp.abs)
        trashed = await asyncio.to_thread(_to_trash, rp)
        return ToolResult(
            True,
            {"path": rp.display, "trash": trashed, "recoverable_days": self.fs.settings.trash_retention_days},
            f"deleted {rp.display} (in trash)",
            detail_kind="filesystem",
            detail=self._fs_detail("delete", rp, sha256_before=before, backup_path=trashed),
        )


def purge_trash(roots: list[str], retention_days: int) -> int:
    """Remove trash batches older than retention (called by the sweeper)."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for root in roots:
        trash = Path(root) / INTERNAL_DIR / "trash"
        if not trash.is_dir():
            continue
        for batch in trash.iterdir():
            try:
                if batch.stat().st_mtime < cutoff:
                    shutil.rmtree(batch)
                    removed += 1
            except OSError:
                continue
    return removed


def filesystem_tools(fs: FsContext) -> list[Any]:
    return [
        cls(fs)
        for cls in (FileRead, FileList, FileSearch, FileOpen, FileCreate, FileWrite, FileModify, FileRename, FileMove, FileDelete)
    ]
