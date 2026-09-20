"""tool-runner: executes shell commands for the AI inside an isolated container.

The container has no network (unless you enable shell network access in Bypass), no secrets, no database access, runs
as an unprivileged user with all capabilities dropped, and only sees the mounted workspace folders.

Protocol: one JSON object per connection over a unix socket.
  request  {"argv": [...], "shell": null | "string", "cwd": "/workspace/...", "timeout_s": 10, "mode": "restricted"|"broad",
            "max_output": 65536}
  response {"exit_code": int|null, "stdout": str, "stderr": str, "truncated": bool, "timed_out": bool, "duration_ms": float}
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import signal
import sys
import time

from rules import CommandRejected, validate_restricted

SOCKET = os.environ.get("RUNNER_SOCKET", "/run/aip/runner.sock")
# restricted: workspace mounted READ-ONLY, allowlisted argv only. broad: read-write, any command (Bypass only).
RUNNER_MODE = os.environ.get("RUNNER_MODE", "restricted")
ALLOWED_BASES = ("/workspace", "/extra")
ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/runner", "LANG": "C.UTF-8", "TERM": "dumb",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
    "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat",
}
MAX_REQUEST = 256 * 1024


def _limits(broad: bool) -> None:
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    fsize = (512 << 20) if broad else 0          # restricted commands may not write files at all
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _check_cwd(cwd: str) -> str:
    real = os.path.realpath(cwd)
    if not any(real == b or real.startswith(b + "/") for b in ALLOWED_BASES) or not os.path.isdir(real):
        raise CommandRejected("working directory must be an existing folder inside the mounted workspace")
    return real


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    buf = bytearray()
    truncated = False
    while chunk := await stream.read(65536):
        if len(buf) < cap:
            buf += chunk[: cap - len(buf)]
        if len(buf) >= cap:
            truncated = True
    return bytes(buf), truncated


async def run(req: dict) -> dict:
    mode = req.get("mode")
    if mode not in ("restricted", "broad"):
        raise CommandRejected("bad mode")
    if RUNNER_MODE == "restricted" and mode != "restricted":
        raise CommandRejected("this sandbox only runs restricted commands")
    cwd = _check_cwd(str(req.get("cwd", "/workspace")))
    timeout = max(1, min(int(req.get("timeout_s", 10)), 300))
    cap = max(1024, min(int(req.get("max_output", 65536)), 1 << 20))
    shell = req.get("shell")
    if shell is not None:
        if mode != "broad" or not isinstance(shell, str) or not shell.strip() or len(shell) > 8192:
            raise CommandRejected("shell strings are only allowed in broad (Bypass) mode")
        argv = ["/bin/sh", "-c", shell]
    else:
        argv = req.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise CommandRejected("argv must be a non-empty list of strings")
        if mode == "restricted":
            validate_restricted(argv)
    broad = mode == "broad"
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=ENV, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                preexec_fn=lambda: _limits(broad))  # noqa: PLW1509
    timed_out = False
    try:
        (out, t1), (err, t2) = await asyncio.wait_for(
            asyncio.gather(_read_capped(proc.stdout, cap), _read_capped(proc.stderr, cap // 4)), timeout=timeout)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        out, t1, err, t2 = b"", False, b"command timed out", False
    return {"exit_code": None if timed_out else proc.returncode, "stdout": out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace"), "truncated": t1 or t2, "timed_out": timed_out,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        if len(raw) > MAX_REQUEST:
            raise CommandRejected("request too large")
        resp = await run(json.loads(raw))
    except CommandRejected as e:
        resp = {"rejected": str(e)}
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as e:
        resp = {"rejected": f"bad request: {type(e).__name__}"}
    except FileNotFoundError as e:
        resp = {"exit_code": 127, "stdout": "", "stderr": f"command not found: {e.filename}", "truncated": False,
                "timed_out": False, "duration_ms": 0}
    except Exception as e:  # never crash the server on one request
        resp = {"rejected": f"runner error: {type(e).__name__}"}
    writer.write((json.dumps(resp) + "\n").encode())
    try:
        await writer.drain()
    finally:
        writer.close()


async def main() -> None:
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    server = await asyncio.start_unix_server(handle, path=SOCKET, limit=MAX_REQUEST + 1)
    os.chmod(SOCKET, 0o660)  # the backend runs with this container's group (see compose), nobody else can connect
    print(f"tool-runner listening on {SOCKET}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
