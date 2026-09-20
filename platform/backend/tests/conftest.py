"""Shared fixtures. Unit tests need nothing; `db` tests need the throwaway Postgres started by platform-check.ps1.

Fakes (no model or embedding server needed):
  FakeEmbedder  deterministic bag-of-words hashing into 1024 dims (similar texts → high cosine)
  FakeProvider  replays scripted turns (text and/or tool calls) and records every request it received
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from aiplatform.config import Environment, Settings
from aiplatform.db import Database
from aiplatform.migrate import migrate
from aiplatform.model.types import ChatChunk, ChatRequest, ProviderHealth, ToolCall, Usage
from aiplatform.permissions.models import BypassCapabilities, NetworkEntry, PermissionSnapshot, Root, Rule

CONFIG_DIR = Path(os.environ.get("AIP_TEST_CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))
MIGRATIONS = Path(os.environ.get("AIP_MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "db" / "migrations"))
DEFAULTS = {
    "normal": {"read": "allow", "write": "confirm", "destructive": "confirm", "execute": "confirm", "network": "allow"},
    "autonomous": {"read": "allow", "write": "confirm", "destructive": "deny", "execute": "confirm", "network": "allow"},
    "bypass": {"read": "allow", "write": "allow", "destructive": "allow", "execute": "allow", "network": "allow"},
}


class FakeEmbedder:
    model = "fake-embed"
    dims = 1024

    def __init__(self) -> None:
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            if len(w) < 3:
                continue
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dims] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v] if any(v) else [1.0 / math.sqrt(self.dims)] * self.dims

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeProvider:
    name = "fake"
    model = "fake-qwen"

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[ChatRequest] = []

    def push(self, *, text: str = "", calls: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        self.script.append({"text": text, "calls": calls or []})

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.requests.append(req)
        step = self.script.pop(0) if self.script else {"text": "ok", "calls": []}
        if req.json_schema is not None and not step.get("text"):
            step = {"text": '{"memories": []}', "calls": []}
        for part in re.findall(r".{1,12}", step["text"], re.S):
            yield ChatChunk(content=part)
        calls = [ToolCall(f"call_{i}", n, __import__("json").dumps(a)) for i, (n, a) in enumerate(step["calls"])]
        yield ChatChunk(
            done=True,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            usage=Usage(
                prompt_tokens=sum(len(m.content) for m in req.messages) // 4,
                completion_tokens=len(step["text"]) // 4 + 1,
                prompt_ms=5.0,
                generation_ms=10.0,
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, self.name, self.model, "fake", True)

    async def aclose(self) -> None:
        return None


def snapshot(
    mode: str = "normal",
    *,
    internet: str = "restricted",
    rules: tuple[Rule, ...] = (),
    roots: tuple[Root, ...] = (),
    network: tuple[NetworkEntry, ...] = (),
    taint: bool = False,
    bypass: BypassCapabilities | None = None,
    tools_enabled: dict[str, bool] | None = None,
) -> PermissionSnapshot:
    return PermissionSnapshot(
        1,
        mode,
        internet,
        taint,
        False,
        bypass or BypassCapabilities(),
        DEFAULTS,
        rules,  # type: ignore[arg-type]
        roots,
        network,
        tools_enabled or {},
    )


def rule(tool: str, mode: str, effect: str, *, scope: tuple[str, str] = ("any", "*"), priority: int = 100) -> Rule:
    return Rule(str(uuid.uuid4()), tool, mode, scope[0], scope[1], effect, priority)  # type: ignore[arg-type]


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, tuple[Root, ...]]:
    roots = []
    for name, access in (("allowed", "ro"), ("projects", "rw"), ("sandbox", "rw")):
        (tmp_path / name).mkdir()
        roots.append(Root(name, name, str(tmp_path / name), access, "zone"))  # type: ignore[arg-type]
    return tmp_path, tuple(roots)


# ───────────────────────────── database fixtures ─────────────────────────────
def _db_env() -> dict[str, str] | None:
    host = os.environ.get("AIP_TEST_DB_HOST")
    if not host:
        return None
    return {"host": host, "owner_pw": os.environ["AIP_TEST_OWNER_PW"], "app_pw": os.environ["AIP_TEST_APP_PW"]}


@pytest.fixture(scope="session")
async def migrated() -> dict[str, str]:
    env = _db_env()
    if env is None:
        pytest.fail("database tests need AIP_TEST_DB_HOST (run scripts\\platform-check.ps1)")
    conn = await asyncpg.connect(host=env["host"], database="aiplatform", user="aimem_owner", password=env["owner_pw"])
    try:
        await migrate(conn, MIGRATIONS)
    finally:
        await conn.close()
    return env


@pytest.fixture(scope="session")
async def db(migrated: dict[str, str]) -> AsyncIterator[Database]:
    d = await Database.connect(
        host=migrated["host"],
        port=5432,
        database="aiplatform",
        user="aimem_app",
        password=migrated["app_pw"],
        min_size=1,
        max_size=5,
    )
    yield d
    await d.close()


@pytest.fixture
async def owner_conn(migrated: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(host=migrated["host"], database="aiplatform", user="aimem_owner", password=migrated["owner_pw"])
    yield c
    await c.close()


@pytest.fixture
async def user_id(db: Database) -> uuid.UUID:
    uid: uuid.UUID = await db.fetchval(
        "INSERT INTO app.users (username, password_hash, must_change_password) VALUES ($1, 'x', false) RETURNING id",
        "t" + uuid.uuid4().hex[:12],
    )
    return uid


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def test_env(tmp_path: Path) -> Environment:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "admin_bootstrap").write_text("Bootstrap-Pass-123!")
    return Environment(
        secrets_dir=secrets,
        permissions_seed=CONFIG_DIR / "permissions.yaml",
        runtime_dir=tmp_path / "runtime",
        runner_socket=tmp_path / "runner.sock",
        workspace_host_dir="C:/AIWorkspace",
    )
