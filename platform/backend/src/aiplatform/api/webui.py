"""Serves the unmodified llama.cpp web UI (from the embed container's llama-server build) on our origin.

Only GET/HEAD for the UI shell and static assets are proxied; everything else the UI calls (/props, /v1/*) is served by
our own routes. `/cors-proxy` (the UI's MCP proxy) is refused: it would be an open proxy around the SSRF guard.
"""

from __future__ import annotations

import base64
import hashlib
import re

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from aiplatform.api.deps import container, current_principal
from aiplatform.api.middleware import CSP

router = APIRouter(include_in_schema=False)
_ASSET = re.compile(
    r"^/(favicon\.(ico|svg)|manifest\.webmanifest|build\.json|[A-Za-z0-9._-]+\.png|"
    r"_app/[A-Za-z0-9._/-]+\.(js|css|json|woff2?|svg|png|wasm|map))$"
)
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_PASS_HEADERS = (
    "content-type",
    "etag",
    "cache-control",
    "last-modified",
    "cross-origin-embedder-policy",
    "cross-origin-opener-policy",
)


class UiProxy:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(10, connect=3))
        self._csp: str | None = None

    async def get(self, path: str, request: Request) -> Response:
        hdrs = {"Accept": request.headers.get("accept", "*/*") or "*/*", "Accept-Encoding": "gzip"}
        # never revalidate the shell: a 304 has no body to hash, so it would carry the hash-less default CSP and the browser
        # would apply it to the cached HTML, blocking the UI's inline bootstrap script
        if (inm := request.headers.get("if-none-match")) and path != "/":
            hdrs["If-None-Match"] = inm
        try:
            r = await self.http.get(path, headers=hdrs)
        except httpx.HTTPError:
            return Response("web UI assets unavailable (embed container not running)", status_code=503)
        out = {k: v for k, v in r.headers.items() if k.lower() in _PASS_HEADERS}
        body = r.content  # httpx already decoded the gzip transfer
        if path == "/" and r.status_code == 200:
            out["content-security-policy"] = self._html_csp(r)
        return Response(body, status_code=r.status_code, headers=out)

    def _html_csp(self, r: httpx.Response) -> str:
        """Allow exactly the UI's inline bootstrap scripts (by hash), nothing else inline."""
        html = r.content
        hashes = " ".join(
            f"'sha256-{base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode()}'" for m in _INLINE_SCRIPT.finditer(html)
        )
        return CSP.replace("script-src 'self'", f"script-src 'self' {hashes}")

    async def aclose(self) -> None:
        await self.http.aclose()


@router.get("/")
async def ui_root(request: Request) -> Response:
    if await current_principal(request) is None:
        return RedirectResponse("/admin/login?next=/", status_code=303)
    return await container(request).ui.get("/", request)


@router.api_route("/cors-proxy", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def cors_proxy() -> Response:
    return Response(
        '{"error":"disabled: the platform does not proxy arbitrary URLs for the browser"}',
        status_code=403,
        media_type="application/json",
    )


@router.get("/{path:path}")
async def ui_asset(path: str, request: Request) -> Response:
    full = "/" + path
    if not _ASSET.match(full) or ".." in full:
        return Response('{"error":{"code":"not_found","message":"not found"}}', status_code=404, media_type="application/json")
    return await container(request).ui.get(full, request)
