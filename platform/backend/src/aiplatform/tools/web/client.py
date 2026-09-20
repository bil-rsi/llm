"""Guarded HTTP client: connects to the vetted IP (SNI/Host = original name), re-validates every redirect,
enforces content-type, size and time limits, and rate-limits per domain and globally."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from aiplatform.config import WebSettings
from aiplatform.tools.base import GuardrailViolation, ToolInputError
from aiplatform.tools.web.ssrf import InternalNetworks, Target, build_target, resolve_checked

REDIRECTS = {301, 302, 303, 307, 308}
SAFE_REQUEST_HEADERS = {"accept", "accept-language", "content-type"}
USER_AGENT = "Mozilla/5.0 (compatible; aiplatform-local/0.1; +https://127.0.0.1)"


class RateLimiter:
    """Sliding-window limiter (per minute): global and per domain."""

    def __init__(self, per_minute: int, per_domain: int) -> None:
        self.per_minute = per_minute
        self.per_domain = per_domain
        self._all: deque[float] = deque()
        self._by: dict[str, deque[float]] = {}

    def check(self, domain: str) -> None:
        now = time.monotonic()
        for q in (self._all, self._by.setdefault(domain, deque())):
            while q and now - q[0] > 60:
                q.popleft()
        if len(self._all) >= self.per_minute:
            raise ToolInputError("web rate limit reached (per minute); wait a little")
        if len(self._by[domain]) >= self.per_domain:
            raise ToolInputError(f"rate limit for {domain} reached; wait a little")
        self._all.append(now)
        self._by[domain].append(now)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool
    ip: str
    ms: float
    redirects: list[str] = field(default_factory=list)


class GuardedClient:
    def __init__(
        self, settings: WebSettings, internal: InternalNetworks, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.s = settings
        self.internal = internal
        self.limiter = RateLimiter(settings.rate_per_minute, settings.rate_per_domain_per_minute)
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(settings.total_timeout_s, connect=settings.connect_timeout_s),
        )

    async def fetch(
        self,
        target: Target,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        private_ok: bool,
        listed_private: Callable[[str, int], bool],
        accept_types: list[str] | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        t0 = time.perf_counter()
        cap = max_bytes or self.s.max_bytes
        types = accept_types or self.s.content_types
        redirects: list[str] = []
        current = target
        for _ in range(self.s.max_redirects + 1):
            self.limiter.check(current.host)
            ip = await resolve_checked(current, self.internal, private_ok=private_ok)
            netloc_ip = f"[{ip}]" if ":" in ip else ip
            url = f"{current.scheme}://{netloc_ip}:{current.port}{current.path_query}"
            hdrs = {
                "Host": current.host if current.port in (80, 443) else f"{current.host}:{current.port}",
                "User-Agent": USER_AGENT,
                "Accept": ", ".join(types) + ", */*;q=0.1",
                "Accept-Encoding": "gzip, deflate",
            }
            hdrs.update({k: v for k, v in (headers or {}).items() if k.lower() in SAFE_REQUEST_HEADERS})
            ext = {"sni_hostname": current.host} if current.scheme == "https" else {}
            try:
                req = self._http.build_request(method, url, headers=hdrs, content=body, extensions=ext)
                resp = await self._http.send(req, stream=True)
            except httpx.TimeoutException as e:
                raise ToolInputError(f"{current.host} timed out") from e
            except httpx.HTTPError as e:
                raise ToolInputError(f"request to {current.host} failed: {type(e).__name__}") from e
            try:
                if resp.status_code in REDIRECTS and "location" in resp.headers:
                    nxt = urljoin(current.url, resp.headers["location"])
                    if current.scheme == "https" and nxt.lower().startswith("http:"):
                        raise GuardrailViolation("redirect from HTTPS to HTTP blocked", "downgrade")
                    redirects.append(nxt)
                    current = build_target(nxt, self.s.allowed_ports, listed_private=listed_private)
                    if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                        method, body = "GET", None
                    continue
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if (
                    method != "HEAD"
                    and ctype
                    and not any(ctype == t or (t.endswith("/*") and ctype.startswith(t[:-1])) for t in types)
                ):
                    raise ToolInputError(f"content type {ctype!r} is not allowed")
                buf = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > cap:
                        truncated = True
                        del buf[cap:]
                        break
                return FetchResult(
                    target.url,
                    current.url,
                    resp.status_code,
                    ctype,
                    bytes(buf),
                    truncated,
                    ip,
                    (time.perf_counter() - t0) * 1000,
                    redirects,
                )
            finally:
                await resp.aclose()
        raise ToolInputError(f"too many redirects (>{self.s.max_redirects})")

    async def aclose(self) -> None:
        await self._http.aclose()
