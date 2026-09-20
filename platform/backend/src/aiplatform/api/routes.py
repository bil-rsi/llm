"""JSON REST API (/api/*) for memory, conversations, tools, approvals, permissions, audit, model and auth.

Every route declares its scope. Permission and Bypass changes additionally need an elevated browser session
(password re-entry): API tokens can read permissions but never change them, and the model has no access at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from aiplatform.api.deps import SESSION_COOKIE, Admin, AdminElevated, Reader, container, require
from aiplatform.memory.lifecycle import Candidate
from aiplatform.security.auth import Principal
from aiplatform.shared.errors import NotFound

router = APIRouter(prefix="/api")
PendingReader = Depends(require("read", allow_password_change_pending=True))
PendingAdmin = Depends(require("admin", allow_password_change_pending=True))


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ───────────────────────────── auth ─────────────────────────────
class LoginIn(_In):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)


@router.post("/auth/login", tags=["auth"])
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    c = container(request)
    token, csrf, must_change = await c.auth.login(
        body.username,
        body.password,
        client_key=request.client.host if request.client else "?",
        user_agent=request.headers.get("user-agent", ""),
    )
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/", max_age=c.settings.server.session_hours * 3600
    )
    return {"csrf_token": csrf, "must_change_password": must_change}


@router.post("/auth/logout", tags=["auth"])
async def logout(
    request: Request,
    response: Response,
    p: Principal = PendingReader,
) -> dict[str, str]:
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        await container(request).auth.logout(tok)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "logged out"}


class PasswordIn(_In):
    current: str = Field(..., max_length=256)
    new: str = Field(..., max_length=256)


@router.post("/auth/password", tags=["auth"])
async def change_password(
    body: PasswordIn,
    request: Request,
    p: Principal = PendingAdmin,
) -> dict[str, str]:
    await container(request).auth.change_password(p, body.current, body.new)
    return {"status": "password changed"}


class ElevateIn(_In):
    password: str = Field(..., max_length=256)


@router.post("/auth/elevate", tags=["auth"])
async def elevate(body: ElevateIn, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).auth.elevate(p, body.password)
    return {"status": "elevated"}


class TokenIn(_In):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[Literal["chat", "read", "admin"]] = Field(..., min_length=1)
    days: int | None = Field(None, ge=1, le=3650)


@router.get("/auth/tokens", tags=["auth"])
async def list_tokens(request: Request, p: Principal = Admin) -> list[dict[str, Any]]:
    return await container(request).auth.list_tokens(p.user_id)


@router.post("/auth/tokens", tags=["auth"])
async def create_token(body: TokenIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    token = await container(request).auth.create_token(p, body.name, list(body.scopes), body.days)
    return {"token": token, "note": "shown once; store it now"}


@router.delete("/auth/tokens/{token_id}", tags=["auth"])
async def revoke_token(token_id: UUID, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).auth.revoke_token(p, token_id)
    return {"status": "revoked"}


# ───────────────────────────── conversations ─────────────────────────────
@router.get("/conversations", tags=["conversations"])
async def conversations(
    request: Request,
    p: Principal = Reader,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return await container(request).conversations.list_conversations(p.user_id, limit=limit, offset=offset, q=q)


@router.get("/conversations/{cid}", tags=["conversations"])
async def conversation(
    cid: UUID, request: Request, p: Principal = Reader, limit: int = Query(200, ge=1, le=1000), before_seq: int | None = None
) -> dict[str, Any]:
    c = container(request)
    conv = await c.conversations.get(cid, p.user_id)
    if conv is None:
        raise NotFound("conversation not found")
    conv.pop("fingerprint", None)
    return {
        "conversation": conv,
        "messages": await c.conversations.messages(cid, p.user_id, limit=limit, before_seq=before_seq),
        "short_term_memory": await c.stm.list_for(cid),
    }


@router.delete("/conversations/{cid}", tags=["conversations"])
async def delete_conversation(cid: UUID, request: Request, p: Principal = Admin) -> dict[str, str]:
    c = container(request)
    if not await c.conversations.delete(cid, p.user_id):
        raise NotFound("conversation not found")
    await c.audit.append("conversation.delete", actor_type="user", actor_id=str(p.user_id), target=str(cid), outcome="success")
    return {"status": "deleted"}


# ───────────────────────────── memories ─────────────────────────────
@router.get("/memories", tags=["memory"])
async def memories(
    request: Request,
    p: Principal = Reader,
    status: Literal["candidate", "active", "superseded", "rejected", "expired", "deleted"] | None = None,
    kind: str | None = Query(None, max_length=20),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return await container(request).ltm.list_items(p.user_id, status=status, kind=kind, q=q, limit=limit, offset=offset)


@router.get("/memories/search", tags=["memory"])
async def memory_search(
    request: Request,
    q: str = Query(..., min_length=2, max_length=500),
    p: Principal = Reader,
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    r = await container(request).retriever.retrieve(p.user_id, q, mode=mode, limit=limit, force=True)
    return {"ms": round(r.ms, 2), "candidates": r.candidates, "results": [m.__dict__ for m in r.memories]}


class MemoryIn(_In):
    content: str = Field(..., min_length=8, max_length=1000)
    kind: Literal["preference", "fact", "project", "instruction", "decision", "context"] | None = None
    ttl_days: int | None = Field(None, ge=1, le=3650)


@router.post("/memories", tags=["memory"])
async def create_memory(body: MemoryIn, request: Request, p: Principal = Admin) -> dict[str, Any]:
    c = container(request)
    res = await c.pipeline.submit(
        Candidate(
            user_id=p.user_id,
            content=body.content,
            source_type="manual",
            proposed_kind=body.kind,
            explicit=True,
            confidence=0.95,
            ttl_days=body.ttl_days,
        )
    )
    c.retriever.invalidate(p.user_id)
    return {"outcome": res.outcome, "memory_id": res.memory_id, "reason": res.reason}


class MemoryPatch(_In):
    content: str | None = Field(None, min_length=8, max_length=1000)
    kind: Literal["preference", "fact", "project", "instruction", "decision", "context"] | None = None
    importance: float | None = Field(None, ge=0, le=1)
    expires_at: datetime | None = None
    clear_expiry: bool = False


@router.patch("/memories/{mid}", tags=["memory"])
async def update_memory(mid: UUID, body: MemoryPatch, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).pipeline.update(
        mid,
        p.user_id,
        content=body.content,
        kind=body.kind,
        importance=body.importance,
        expires_at=body.expires_at,
        clear_expiry=body.clear_expiry,
    )
    return {"status": "updated"}


@router.post("/memories/{mid}/approve", tags=["memory"])
async def approve_memory(mid: UUID, request: Request, p: Principal = Admin) -> dict[str, str]:
    c = container(request)
    await c.pipeline.approve(mid, p.user_id)
    c.retriever.invalidate(p.user_id)
    return {"status": "active"}


@router.post("/memories/{mid}/reject", tags=["memory"])
async def reject_memory(mid: UUID, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).pipeline.reject(mid, p.user_id)
    return {"status": "rejected"}


@router.delete("/memories/{mid}", tags=["memory"])
async def delete_memory(mid: UUID, request: Request, p: Principal = Admin) -> dict[str, str]:
    c = container(request)
    await c.pipeline.delete(mid, p.user_id)
    c.retriever.invalidate(p.user_id)
    return {"status": "deleted"}


# ───────────────────────────── tools & approvals ─────────────────────────────
@router.get("/tools", tags=["tools"])
async def tools(request: Request, p: Principal = Reader) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await container(request).db.fetch(
            "SELECT name, category, description, risk, enabled, version, input_schema FROM app.tool_definitions ORDER BY name"
        )
    ]


@router.get("/tool-executions", tags=["tools"])
async def tool_executions(
    request: Request,
    p: Principal = Reader,
    tool: str | None = Query(None, max_length=64),
    status: str | None = Query(None, max_length=20),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await container(request).db.fetch(
            "SELECT t.id, t.tool_name, t.status, t.decision, t.policy_rule, t.decision_reason, t.mode, t.tainted, t.dry_run, "
            "t.arguments, t.requested_at, t.duration_ms, t.result_summary, t.error, f.operation, f.path, f.backup_path, "
            "w.method, w.url_redacted, w.status_code, w.resolved_ip FROM app.tool_executions t "
            "LEFT JOIN app.filesystem_operations f ON f.tool_execution_id=t.id "
            "LEFT JOIN app.web_requests w ON w.tool_execution_id=t.id "
            "WHERE ($1::text IS NULL OR t.tool_name=$1) AND ($2::text IS NULL OR t.status=$2) "
            "ORDER BY t.requested_at DESC LIMIT $3",
            tool,
            status,
            limit,
        )
    ]


@router.get("/approvals", tags=["tools"])
async def approvals(request: Request, p: Principal = Reader) -> list[dict[str, Any]]:
    return await container(request).approvals.pending()


class DecideIn(_In):
    approve: bool
    note: str = Field("", max_length=500)


@router.post("/approvals/{aid}", tags=["tools"])
async def decide(aid: UUID, body: DecideIn, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).approvals.decide(aid, body.approve, p.user_id, body.note)
    return {"status": "approved" if body.approve else "denied"}


# ───────────────────────────── permissions ─────────────────────────────
@router.get("/permissions", tags=["permissions"])
async def permissions(request: Request, p: Principal = Reader) -> dict[str, Any]:
    ps = container(request).permissions
    return {
        "settings": await ps.settings(),
        "rules": await ps.list_rules(),
        "roots": await ps.list_roots(),
        "network": await ps.list_network(),
    }


class SettingIn(_In):
    value: Any


@router.put("/permissions/settings/{key}", tags=["permissions"])
async def set_setting(key: str, body: SettingIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.set_setting(key, body.value, p.user_id)
    return {"status": "saved"}


class RuleIn(_In):
    tool_pattern: str = Field(..., max_length=64)
    mode: Literal["*", "normal", "autonomous", "bypass"] = "*"
    scope_type: Literal["any", "path", "domain", "command"] = "any"
    scope_pattern: str = Field("*", max_length=500)
    effect: Literal["allow", "confirm", "deny"]
    priority: int = Field(100, ge=-1000, le=1000)
    note: str = Field("", max_length=300)
    enabled: bool = True


@router.post("/permissions/rules", tags=["permissions"])
async def add_rule(body: RuleIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    return {"id": await container(request).permissions.upsert_rule(body.model_dump(), p.user_id)}


@router.put("/permissions/rules/{rid}", tags=["permissions"])
async def update_rule(rid: UUID, body: RuleIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    return {"id": await container(request).permissions.upsert_rule(body.model_dump(), p.user_id, rid)}


@router.delete("/permissions/rules/{rid}", tags=["permissions"])
async def delete_rule(rid: UUID, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.delete_rule(rid, p.user_id)
    return {"status": "deleted"}


class ToolToggle(_In):
    enabled: bool


@router.put("/permissions/tools/{name}", tags=["permissions"])
async def toggle_tool(name: str, body: ToolToggle, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.set_tool_enabled(name, body.enabled, p.user_id)
    return {"status": "saved"}


class RootIn(_In):
    name: str = Field(..., max_length=40)
    host_path: str = Field(..., max_length=500)
    access: Literal["ro", "rw"] = "ro"


@router.post("/permissions/roots", tags=["permissions"])
async def add_root(body: RootIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    c = container(request)
    await c.permissions.add_extra_root(body.name, body.host_path, body.access, p.user_id, c.platform_host_dir)
    return {"status": "saved", "note": "run scripts\\platform-up.ps1 to mount the folder"}


@router.delete("/permissions/roots/{name}", tags=["permissions"])
async def remove_root(name: str, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.remove_root(name, p.user_id)
    return {"status": "removed", "note": "run scripts\\platform-up.ps1 to unmount the folder"}


class NetworkIn(_In):
    kind: Literal["public_domain", "private_host"]
    host: str = Field(..., max_length=253)
    port: int | None = Field(None, ge=1, le=65535)
    methods: list[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]] = ["GET", "HEAD"]
    note: str = Field("", max_length=200)


@router.post("/permissions/network", tags=["permissions"])
async def add_network(body: NetworkIn, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.add_network(body.kind, body.host, body.port, list(body.methods), p.user_id, body.note)
    return {"status": "saved"}


@router.delete("/permissions/network/{nid}", tags=["permissions"])
async def remove_network(nid: UUID, request: Request, p: Principal = AdminElevated) -> dict[str, str]:
    await container(request).permissions.remove_network(nid, p.user_id)
    return {"status": "removed"}


@router.get("/permissions/mounts", tags=["permissions"])
async def mounts_export(request: Request, p: Principal = Reader) -> dict[str, Any]:
    """Consumed by platform-up.ps1 to generate compose.mounts.yaml (extra folders + shell network)."""
    ps = container(request).permissions
    roots = [r for r in await ps.list_roots() if r["kind"] == "extra" and r["enabled"]]
    s = await ps.settings()
    return {
        "extra": [
            {"name": r["name"], "host_path": r["host_path"], "container_path": r["container_path"], "access": r["access"]}
            for r in roots
        ],
        "shell_network": bool(s.get("shell_network", False)),
    }


# ───────────────────────────── audit ─────────────────────────────
@router.get("/audit", tags=["audit"])
async def audit(
    request: Request,
    p: Principal = Reader,
    action: str | None = Query(None, max_length=100),
    actor_type: Literal["user", "model", "system"] | None = None,
    outcome: Literal["success", "failure", "denied", "pending", "info"] | None = None,
    before_seq: int | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await container(request).audit.query(
        action=action, actor_type=actor_type, outcome=outcome, before_seq=before_seq, limit=limit
    )


@router.get("/audit/verify", tags=["audit"])
async def audit_verify(request: Request, p: Principal = Reader) -> dict[str, Any]:
    return await container(request).audit.verify()


# ───────────────────────────── model ─────────────────────────────
@router.get("/model", tags=["model"])
async def model_info(request: Request, p: Principal = Reader) -> dict[str, Any]:
    c = container(request)
    h = await c.runtime.provider().health()
    return {
        "params": c.runtime.params().__dict__,
        "health": h.__dict__,
        "embedding": {"model": c.embedder.model, "ok": await c.embedder.health()},
        "chars_per_token": round(c.tokens.chars_per_token, 3),
    }


class ModelSettingIn(_In):
    value: Any


@router.put("/model/{key}", tags=["model"])
async def model_set(key: str, body: ModelSettingIn, request: Request, p: Principal = Admin) -> dict[str, str]:
    await container(request).runtime.set(key, body.value, p.user_id)
    return {"status": "saved"}


# ───────────────────────────── dashboard metrics ─────────────────────────────
@router.get("/metrics/summary", tags=["observability"])
async def metrics_summary(request: Request, p: Principal = Reader, hours: int = Query(24, ge=1, le=720)) -> dict[str, Any]:
    return await container(request).dashboard.summary(p.user_id, hours)
