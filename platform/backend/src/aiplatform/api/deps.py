"""Request authentication/authorisation dependencies (default-deny: every router declares what it needs).

Browser sessions (cookie) must send the CSRF token (X-CSRF-Token header or csrf form field) on state-changing requests;
bearer tokens are immune to CSRF because browsers never attach them automatically. The model has no principal at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Depends, Request

from aiplatform.security.auth import Principal
from aiplatform.shared.errors import PermissionDenied, Unauthorized

if TYPE_CHECKING:
    from aiplatform.main import AppContainer

SESSION_COOKIE = "aip_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def container(request: Request) -> AppContainer:
    c: AppContainer = request.app.state.container
    return c


async def current_principal(request: Request) -> Principal | None:
    if getattr(request.state, "principal_loaded", False):
        cached: Principal | None = request.state.principal
        return cached
    c = container(request)
    principal: Principal | None = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        principal = await c.auth.token_principal(auth[7:].strip())
        if principal is None:
            raise Unauthorized("invalid or expired API token")
    else:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            found = await c.auth.session_principal(token)
            if found is not None:
                principal, csrf_hash = found
                request.state.csrf_hash = csrf_hash
    request.state.principal = principal
    request.state.principal_loaded = True
    return principal


async def _csrf_check(request: Request, p: Principal, mode: str) -> None:
    if p.via != "session" or request.method in SAFE_METHODS:
        return
    if mode == "origin":
        # fetch()/XHR from our own pages (the web UI) always sends Origin on POST; the middleware already rejected
        # foreign origins, so here we only require that it is present and same-origin.
        c = container(request)
        origin = (request.headers.get("origin") or "").lower().rstrip("/")
        allowed = {o.lower().rstrip("/") for o in c.settings.server.allowed_origins}
        if origin in allowed and request.headers.get("sec-fetch-site", "same-origin") == "same-origin":
            return
        raise PermissionDenied("same-origin request required")
    presented = request.headers.get("x-csrf-token")
    if presented is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        presented = str((await request.form()).get("csrf") or "")
    if not container(request).auth.csrf_ok(getattr(request.state, "csrf_hash", b""), presented):
        raise PermissionDenied("missing or invalid CSRF token")


def require(
    scope: str, *, elevated: bool = False, allow_password_change_pending: bool = False, csrf: str = "token"
) -> Callable[[Request], Awaitable[Principal]]:
    async def dep(request: Request) -> Principal:
        p = await current_principal(request)
        if p is None:
            raise Unauthorized("login required")
        if not p.has(scope):
            raise PermissionDenied(f"'{scope}' scope required")
        if p.must_change_password and not allow_password_change_pending:
            raise PermissionDenied("change the bootstrap password first (admin console → Account)")
        await _csrf_check(request, p, csrf)
        if elevated and p.via == "session" and not p.elevated:
            raise PermissionDenied("re-enter your password to change this (elevation required)")
        if elevated and p.via == "token":
            raise PermissionDenied("this change can only be made from the admin console")
        return p

    return dep


ChatUser = Depends(require("chat", csrf="origin"))
Reader = Depends(require("read"))
Admin = Depends(require("admin"))
AdminElevated = Depends(require("admin", elevated=True))
