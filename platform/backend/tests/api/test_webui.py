from __future__ import annotations

import base64
import hashlib

import httpx
from starlette.requests import Request

from aiplatform.api.webui import UiProxy

BOOT = b"window.__boot = 1;"
HTML = b"<html><head><script>" + BOOT + b"</script><script src='/_app/x.js'></script></head></html>"


def _req(**headers: str) -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    )


async def _proxy(seen: list[httpx.Request]) -> UiProxy:
    def upstream(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html", "etag": '"v1"'})

    p = UiProxy("http://upstream")
    await p.http.aclose()
    p.http = httpx.AsyncClient(base_url="http://upstream", transport=httpx.MockTransport(upstream))
    return p


async def test_shell_csp_allows_only_inline_bootstrap_hash() -> None:
    p = await _proxy([])
    r = await p.get("/", _req())
    digest = base64.b64encode(hashlib.sha256(BOOT).digest()).decode()
    assert r.status_code == 200 and f"script-src 'self' 'sha256-{digest}'" in r.headers["content-security-policy"]


async def test_shell_revalidation_never_yields_hashless_304() -> None:
    seen: list[httpx.Request] = []
    p = await _proxy(seen)
    r = await p.get("/", _req(**{"if-none-match": '"v1"'}))
    assert "if-none-match" not in seen[0].headers
    assert r.status_code == 200 and "'sha256-" in r.headers["content-security-policy"]


async def test_assets_still_revalidate() -> None:
    seen: list[httpx.Request] = []
    p = await _proxy(seen)
    r = await p.get("/_app/x.js", _req(**{"if-none-match": '"v1"'}))
    assert r.status_code == 304
