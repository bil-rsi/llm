"""HTTP middleware: request/correlation IDs, Host + Origin allowlists, security headers, timing and metrics.

Pure ASGI (not BaseHTTPMiddleware) so streaming responses are not buffered and overhead stays in microseconds.
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiplatform.observability import context
from aiplatform.observability.metrics import Metrics
from aiplatform.shared import timing

CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), usb=()"),
    (b"content-security-policy", CSP.encode()),
]
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def _route_label(path: str) -> str:
    for prefix in (
        "/v1/chat",
        "/v1/models",
        "/api/memories",
        "/api/conversations",
        "/api/permissions",
        "/api/approvals",
        "/api/audit",
        "/api/tools",
        "/api/model",
        "/api/auth",
        "/api/metrics",
        "/admin",
        "/health",
        "/metrics",
        "/props",
        "/_app",
        "/docs",
        "/openapi",
    ):
        if path.startswith(prefix):
            return prefix
    return "/other"


class GuardMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: list[str], allowed_origins: list[str], metrics: Metrics) -> None:
        self.app = app
        self.hosts = {h.lower() for h in allowed_hosts}
        self.origins = {o.lower().rstrip("/") for o in allowed_origins}
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        path: str = scope["path"]
        method: str = scope["method"]
        rid = context.new_id()
        cid = context.valid_external_id(headers.get("x-correlation-id")) or rid
        context.request_id.set(rid)
        context.correlation_id.set(cid)
        timing.begin()
        t0 = time.perf_counter()
        label = _route_label(path)

        # Host allowlist → blocks DNS-rebinding (health checks from inside the container use 127.0.0.1:8090).
        host = headers.get("host", "").lower()
        if host not in self.hosts and not (path.startswith("/health") and host.startswith(("127.0.0.1", "localhost"))):
            await self._reject(send, 421, "unexpected Host header", rid)
            return
        # Origin allowlist for state-changing requests → blocks drive-by requests from other websites.
        origin = headers.get("origin")
        if method in STATE_CHANGING and origin is not None and origin.lower().rstrip("/") not in self.origins:
            await self._reject(send, 403, "cross-origin request blocked", rid)
            return
        if headers.get("sec-fetch-site") == "cross-site" and method in STATE_CHANGING:
            await self._reject(send, 403, "cross-site request blocked", rid)
            return

        status_holder: dict[str, Any] = {"status": 500, "first": None}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["first"] = (time.perf_counter() - t0) * 1000
                hdrs = list(message.get("headers", []))
                present = {k.lower() for k, _ in hdrs}
                hdrs += [(k, v) for k, v in SECURITY_HEADERS if k not in present]
                hdrs.append((b"x-request-id", rid.encode()))
                hdrs.append((b"x-correlation-id", cid.encode()))
                st = timing.current()
                if st:
                    hdrs.append((b"server-timing", ", ".join(f"{k};dur={v:.2f}" for k, v in st.items()).encode()))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = status_holder["first"] or (time.perf_counter() - t0) * 1000
            self.metrics.http_requests.labels(label, method, str(status_holder["status"])).inc()
            self.metrics.http_latency.labels(label).observe(elapsed)
            if label not in ("/v1/chat", "/_app", "/other"):
                timing.add("api_total", elapsed)
            for k, v in timing.current().items():
                self.metrics.stage_ms.labels(k).observe(v)

    @staticmethod
    async def _reject(send: Send, status: int, msg: str, rid: str) -> None:
        body = json.dumps({"error": {"code": "rejected", "message": msg, "request_id": rid}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *SECURITY_HEADERS,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
