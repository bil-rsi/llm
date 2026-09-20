"""Admin console (server-rendered, works without JavaScript; htmx only for auto-refresh and inline actions).

Forms use POST-redirect-GET with a CSRF token (kept in an HttpOnly SameSite=Strict cookie and echoed in the form).
Permission and Bypass changes need step-up auth (password re-entry, valid for a few minutes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from aiplatform.api.deps import SESSION_COOKIE, container, current_principal
from aiplatform.memory.lifecycle import Candidate
from aiplatform.security.auth import Principal
from aiplatform.shared.errors import DomainError, PermissionDenied, Unauthorized

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=1, default=str, ensure_ascii=False)
router = APIRouter(prefix="/admin", include_in_schema=False)
static = StaticFiles(directory=str(HERE / "static"))
CSRF_COOKIE = "aip_csrf"


def _redirect(to: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(to + q, status_code=303)


async def _page_principal(request: Request) -> Principal | None:
    try:
        p = await current_principal(request)
    except Unauthorized:
        return None
    return p if p is not None and p.via == "session" else None


async def _form(request: Request, p: Principal) -> dict[str, str]:
    form = await request.form()
    if not container(request).auth.csrf_ok(getattr(request.state, "csrf_hash", b""), str(form.get("csrf") or "")):
        raise PermissionDenied("CSRF check failed; reload the page")
    return {k: str(v) for k, v in form.items()}


def _render(request: Request, name: str, p: Principal, **ctx: Any) -> HTMLResponse:
    c = container(request)
    return templates.TemplateResponse(
        request,
        name,
        {
            "p": p,
            "csrf": request.cookies.get(CSRF_COOKIE, ""),
            "msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", ""),
            "nav": request.url.path,
            "mode": c.permissions_mode_cache,
            **ctx,
        },
    )


async def _guard(request: Request) -> Principal | Response:
    p = await _page_principal(request)
    if p is None:
        return RedirectResponse(f"/admin/login?next={quote(request.url.path)}", status_code=303)
    if p.must_change_password and request.url.path != "/admin/account":
        return _redirect("/admin/account", err="Change the bootstrap password before using the platform.")
    c = container(request)
    c.permissions_mode_cache = (await c.permissions.snapshot()).mode
    return p


# ───────────────────────────── login ─────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "login.html", {"err": request.query_params.get("err", ""), "next": request.query_params.get("next", "/admin")}
    )


@router.post("/login")
async def login_submit(request: Request) -> Response:
    form = await request.form()
    nxt = str(form.get("next") or "/admin")
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(c) < 32 for c in nxt):
        nxt = "/admin"
    c = container(request)
    try:
        token, csrf, must_change = await c.auth.login(
            str(form.get("username", ""))[:64],
            str(form.get("password", ""))[:256],
            client_key=request.client.host if request.client else "?",
            user_agent=request.headers.get("user-agent", ""),
        )
    except DomainError as e:
        return _redirect("/admin/login", err=e.message)
    resp = RedirectResponse("/admin/account" if must_change else nxt, status_code=303)
    age = c.settings.server.session_hours * 3600
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/", max_age=age)
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="strict", path="/admin", max_age=age)
    return resp


@router.post("/logout")
async def logout(request: Request) -> Response:
    p = await _page_principal(request)
    if p is not None:
        await _form(request, p)
        tok = request.cookies.get(SESSION_COOKIE)
        if tok:
            await container(request).auth.logout(tok)
    resp = _redirect("/admin/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/admin")
    return resp


# ───────────────────────────── dashboard ─────────────────────────────
@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    summary = await c.dashboard.summary(p.user_id, 24)
    health = await c.runtime.provider().health()
    return _render(
        request,
        "dashboard.html",
        p,
        s=summary,
        health=health,
        params=c.runtime.params(),
        embed_ok=await c.embedder.health(),
        runner_ok=await c.runner.health(),
        approvals=len(await c.approvals.pending()),
    )


# ───────────────────────────── account ─────────────────────────────
@router.get("/account", response_class=HTMLResponse)
async def account(request: Request) -> Response:
    p = await _page_principal(request)
    if p is None:
        return _redirect("/admin/login")
    return _render(request, "account.html", p, tokens=await container(request).auth.list_tokens(p.user_id), new_token="")


@router.post("/account/password")
async def account_password(request: Request) -> Response:
    p = await _page_principal(request)
    if p is None:
        return _redirect("/admin/login")
    f = await _form(request, p)
    try:
        await container(request).auth.change_password(p, f.get("current", ""), f.get("new", ""))
    except DomainError as e:
        return _redirect("/admin/account", err=e.message)
    return _redirect("/admin", msg="Password changed.")


@router.post("/account/elevate")
async def account_elevate(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    back = f.get("back", "/admin/permissions")
    back = back if back.startswith("/admin") else "/admin/permissions"
    try:
        await container(request).auth.elevate(p, f.get("password", ""))
    except DomainError as e:
        return _redirect(back, err=e.message)
    return _redirect(back, msg="Unlocked for a few minutes.")


@router.post("/account/tokens")
async def account_token(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    if not p.elevated:
        return _redirect("/admin/account", err="Unlock (re-enter your password) to create tokens.")
    scopes = [s for s in ("chat", "read", "admin") if f.get(f"scope_{s}")]
    try:
        tok = await container(request).auth.create_token(
            p, f.get("name", "token"), scopes, int(f["days"]) if f.get("days", "").isdigit() else None
        )
    except DomainError as e:
        return _redirect("/admin/account", err=e.message)
    # Rendered directly (never put the token in a URL): it is shown exactly once.
    return _render(request, "account.html", p, tokens=await container(request).auth.list_tokens(p.user_id), new_token=tok)


@router.post("/account/tokens/{tid}/revoke")
async def account_token_revoke(tid: UUID, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    await _form(request, p)
    await container(request).auth.revoke_token(p, tid)
    return _redirect("/admin/account", msg="Token revoked.")


# ───────────────────────────── approvals ─────────────────────────────
@router.get("/approvals", response_class=HTMLResponse)
async def approvals(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    items = await container(request).approvals.pending()
    tpl = "approvals_list.html" if request.headers.get("hx-request") else "approvals.html"
    return _render(request, tpl, p, items=items)


@router.post("/approvals/{aid}")
async def approvals_decide(aid: UUID, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    try:
        await container(request).approvals.decide(aid, f.get("decision") == "approve", p.user_id, f.get("note", ""))
    except DomainError as e:
        return _redirect("/admin/approvals", err=e.message)
    return _redirect("/admin/approvals", msg="Decision sent.")


# ───────────────────────────── memories ─────────────────────────────
@router.get("/memories", response_class=HTMLResponse)
async def memories(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    qp = request.query_params
    status = qp.get("status") or None
    if status not in (None, "candidate", "active", "superseded", "rejected", "expired", "deleted"):
        status = None
    items = await c.ltm.list_items(
        p.user_id, status=status, kind=qp.get("kind") or None, q=qp.get("q") or None, limit=200, offset=0
    )
    search = None
    if qp.get("test"):
        search = await c.retriever.retrieve(p.user_id, qp["test"][:500], force=True)
    return _render(
        request,
        "memories.html",
        p,
        items=items,
        stats=await c.ltm.stats(p.user_id),
        status=status or "",
        kind=qp.get("kind", ""),
        q=qp.get("q", ""),
        test=qp.get("test", ""),
        search=search,
    )


@router.post("/memories")
async def memories_add(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    c = container(request)
    res = await c.pipeline.submit(
        Candidate(
            user_id=p.user_id,
            content=f.get("content", ""),
            source_type="manual",
            proposed_kind=f.get("kind") or None,
            explicit=True,
            confidence=0.95,
        )
    )
    c.retriever.invalidate(p.user_id)
    return _redirect("/admin/memories", msg=f"{res.outcome} {res.reason}".strip())


@router.post("/memories/{mid}/{action}")
async def memories_action(mid: UUID, action: str, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    c = container(request)
    try:
        if action == "approve":
            await c.pipeline.approve(mid, p.user_id)
        elif action == "reject":
            await c.pipeline.reject(mid, p.user_id)
        elif action == "delete":
            await c.pipeline.delete(mid, p.user_id)
        elif action == "edit":
            imp = f.get("importance", "")
            await c.pipeline.update(
                mid,
                p.user_id,
                content=f.get("content") or None,
                kind=f.get("kind") or None,
                importance=float(imp) if imp else None,
                expires_at=None,
            )
        else:
            return _redirect("/admin/memories", err="unknown action")
    except (DomainError, ValueError) as e:
        return _redirect("/admin/memories", err=str(e))
    c.retriever.invalidate(p.user_id)
    return _redirect("/admin/memories", msg=f"Memory {action}d." if action != "edit" else "Memory updated.")


# ───────────────────────────── conversations ─────────────────────────────
@router.get("/conversations", response_class=HTMLResponse)
async def conversations(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    items = await container(request).conversations.list_conversations(
        p.user_id, limit=100, offset=0, q=request.query_params.get("q") or None
    )
    return _render(request, "conversations.html", p, items=items, q=request.query_params.get("q", ""))


@router.get("/conversations/{cid}", response_class=HTMLResponse)
async def conversation(cid: UUID, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    conv = await c.conversations.get(cid, p.user_id)
    if conv is None:
        return _redirect("/admin/conversations", err="not found")
    msgs = await c.conversations.messages(cid, p.user_id, limit=500, before_seq=None)
    return _render(request, "conversation.html", p, conv=conv, msgs=msgs, stm=await c.stm.list_for(cid))


@router.post("/conversations/{cid}/delete")
async def conversation_delete(cid: UUID, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    await _form(request, p)
    c = container(request)
    await c.conversations.delete(cid, p.user_id)
    await c.audit.append("conversation.delete", actor_type="user", actor_id=str(p.user_id), target=str(cid), outcome="success")
    return _redirect("/admin/conversations", msg="Conversation deleted.")


# ───────────────────────────── permissions ─────────────────────────────
@router.get("/permissions", response_class=HTMLResponse)
async def permissions(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    ps = c.permissions
    tools = [
        dict(r)
        for r in await c.db.fetch("SELECT name, category, risk, enabled, description FROM app.tool_definitions ORDER BY name")
    ]
    restart = (c.env.runtime_dir / "restart-required.json").exists()
    return _render(
        request,
        "permissions.html",
        p,
        settings=await ps.settings(),
        rules=await ps.list_rules(),
        roots=await ps.list_roots(),
        network=await ps.list_network(),
        tools=tools,
        restart=restart,
    )


@router.post("/permissions/{what}")
async def permissions_change(what: str, request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    c = container(request)
    ps = c.permissions
    if not p.elevated:
        return _redirect("/admin/permissions", err="Unlock first: re-enter your password (top of this page).")
    try:
        if what == "settings":
            await ps.set_setting("mode", f["mode"], p.user_id)
            await ps.set_setting("internet_mode", f["internet_mode"], p.user_id)
            await ps.set_setting("taint_escalation", f.get("taint_escalation") == "on", p.user_id)
            await ps.set_setting("shell_network", f.get("shell_network") == "on", p.user_id)
            await ps.set_setting(
                "bypass",
                {
                    k: f.get(f"bypass_{k}") == "on"
                    for k in ("extra_folders", "broad_shell", "private_network", "unrestricted_internet")
                },
                p.user_id,
            )
        elif what == "rule":
            await ps.upsert_rule(
                {
                    "tool_pattern": f.get("tool_pattern", ""),
                    "mode": f.get("mode", "*"),
                    "scope_type": f.get("scope_type", "any"),
                    "scope_pattern": f.get("scope_pattern") or "*",
                    "effect": f.get("effect", ""),
                    "priority": f.get("priority") or 100,
                    "note": f.get("note", ""),
                },
                p.user_id,
            )
        elif what == "rule-delete":
            await ps.delete_rule(UUID(f["id"]), p.user_id)
        elif what == "tool":
            await ps.set_tool_enabled(f["name"], f.get("enabled") == "on", p.user_id)
        elif what == "root":
            await ps.add_extra_root(
                f.get("name", ""), f.get("host_path", ""), f.get("access", "ro"), p.user_id, c.platform_host_dir
            )
        elif what == "root-delete":
            await ps.remove_root(f["name"], p.user_id)
        elif what == "network":
            port = f.get("port", "")
            methods = [m for m in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE") if f.get(f"m_{m}")] or ["GET", "HEAD"]
            await ps.add_network(
                f.get("kind", "public_domain"),
                f.get("host", ""),
                int(port) if port.isdigit() else None,
                methods,
                p.user_id,
                f.get("note", ""),
            )
        elif what == "network-delete":
            await ps.remove_network(UUID(f["id"]), p.user_id)
        elif what == "reseed":
            await ps.apply_seed(p.user_id)
        else:
            return _redirect("/admin/permissions", err="unknown change")
    except (DomainError, KeyError, ValueError) as e:
        return _redirect("/admin/permissions", err=getattr(e, "message", str(e)))
    return _redirect("/admin/permissions", msg="Saved.")


# ───────────────────────────── tools / audit / model ─────────────────────────────
@router.get("/tools", response_class=HTMLResponse)
async def tool_log(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    rows = [
        dict(r)
        for r in await container(request).db.fetch(
            "SELECT t.*, f.operation, f.path, f.backup_path, w.method, w.url_redacted, w.status_code FROM app.tool_executions t "
            "LEFT JOIN app.filesystem_operations f ON f.tool_execution_id=t.id LEFT JOIN app.web_requests w "
            "ON w.tool_execution_id=t.id WHERE ($1::text = '' OR t.status = $1) ORDER BY t.requested_at DESC LIMIT 200",
            request.query_params.get("status", ""),
        )
    ]
    return _render(request, "tools.html", p, rows=rows, status=request.query_params.get("status", ""))


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    qp = request.query_params
    rows = await c.audit.query(action=qp.get("action") or None, outcome=qp.get("outcome") or None, limit=300)
    verify = await c.audit.verify() if qp.get("verify") else None
    return _render(request, "audit.html", p, rows=rows, verify=verify, action=qp.get("action", ""), outcome=qp.get("outcome", ""))


@router.get("/model", response_class=HTMLResponse)
async def model(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    c = container(request)
    return _render(
        request,
        "model.html",
        p,
        params=c.runtime.params(),
        health=await c.runtime.provider().health(),
        cpt=c.tokens.chars_per_token,
    )


@router.post("/model")
async def model_save(request: Request) -> Response:
    p = await _guard(request)
    if isinstance(p, Response):
        return p
    f = await _form(request, p)
    c = container(request)
    try:
        for key in ("provider", "temperature", "top_p", "num_ctx", "max_output_tokens"):
            if f.get(key, "") != "":
                await c.runtime.set(f"model.{key}", f[key], p.user_id)
        await c.runtime.set("model.think", f.get("think") == "on", p.user_id)
    except DomainError as e:
        return _redirect("/admin/model", err=e.message)
    return _redirect("/admin/model", msg="Saved.")
