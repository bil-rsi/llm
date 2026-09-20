"""web.fetch, web.search and web.request (non-GET only where enabled). All traffic goes through GuardedClient."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qs, quote_plus, urlsplit

from pydantic import Field
from selectolax.parser import HTMLParser

from aiplatform.config import WebSettings
from aiplatform.permissions.engine import PermissionRequest
from aiplatform.permissions.models import Decision, PermissionSnapshot, Risk
from aiplatform.security.sensitivity import redact, scan
from aiplatform.tools.base import GuardrailViolation, Prepared, ToolArgs, ToolContext, ToolInputError, ToolResult
from aiplatform.tools.web.client import GuardedClient
from aiplatform.tools.web.extract import body_to_text
from aiplatform.tools.web.ssrf import Target, build_target

SEARCH_HOST = "html.duckduckgo.com"


@dataclass
class WebContext:
    settings: WebSettings
    client: GuardedClient


def _listed(snap: PermissionSnapshot) -> Any:
    def f(host: str, port: int) -> bool:
        return snap.bypass_on("private_network") and snap.private_entry(host, port) is not None

    return f


def _capability(snap: PermissionSnapshot, t: Target, method: str) -> tuple[Decision | None, bool]:
    """Returns (capability decision, taint_sensitive)."""
    mode = snap.effective_internet_mode()
    if mode == "disabled":
        return Decision("deny", "capability:internet_disabled", "internet access is disabled"), True
    if t.private_intent:
        return None, method not in ("GET", "HEAD")  # listed LAN/localhost host in Bypass
    entry = snap.public_entry(t.host)
    write = method not in ("GET", "HEAD")
    if write:
        if snap.bypass_on("unrestricted_internet"):
            return None, True
        if entry is None or method not in entry.methods:
            return Decision(
                "deny",
                "capability:method",
                f"{method} to {t.host} is not enabled (add the method to the domain in the admin console)",
            ), True
        return None, True
    if entry is not None:
        return None, False
    if mode == "restricted":
        return Decision("deny", "capability:domain", f"{t.host} is not on the internet allowlist (restricted mode)"), True
    if mode == "trusted":
        return Decision("confirm", "capability:unlisted_domain", f"{t.host} is not on the allowlist"), True
    return None, True  # unrestricted


def _check_exfiltration(url: str) -> None:
    """Refuse URLs that carry secrets (e.g. an API key read from a file pasted into a query string)."""
    if any(f.level == "secret" for f in scan(url)):
        raise GuardrailViolation("the URL contains what looks like a secret/credential", "secret_in_url")


class FetchArgs(ToolArgs):
    url: str = Field(..., min_length=8, max_length=4096)
    max_chars: int = Field(20000, ge=500, le=100000)


class WebFetch:
    name = "web.fetch"
    category: ClassVar[Literal["web"]] = "web"
    description = "Fetch a web page or API URL (GET) and return readable text, title and links."
    risk: ClassVar[Risk] = "network"
    Args = FetchArgs
    timeout_s: ClassVar[float] = 40.0

    def __init__(self, w: WebContext) -> None:
        self.w = w

    def prepare(self, args: FetchArgs, ctx: ToolContext) -> Prepared:
        _check_exfiltration(args.url)
        t = build_target(args.url, self.w.settings.allowed_ports, listed_private=_listed(ctx.snapshot))
        cap, sensitive = _capability(ctx.snapshot, t, "GET")
        return Prepared(
            PermissionRequest(self.name, "network", {"domain": t.host}, cap, sensitive),
            {"url": redact(t.url), "max_chars": args.max_chars},
            f"fetch {redact(t.url)[:150]}",
            t,
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        t: Target = prepared.payload
        r = await self.w.client.fetch(
            t, private_ok=ctx.snapshot.bypass_on("private_network"), listed_private=_listed(ctx.snapshot)
        )
        content = body_to_text(r.body, r.content_type, r.final_url, prepared.canonical.get("max_chars", 20000))
        return ToolResult(
            200 <= r.status < 400,
            {"url": r.final_url, "status": r.status, "content_type": r.content_type, **content, "body_truncated": r.truncated},
            f"{r.status} {urlsplit(r.final_url).netloc} ({len(r.body)} bytes, {r.ms:.0f} ms)",
            untrusted_source=f"web:{t.host}",
            detail_kind="web",
            detail={
                "method": "GET",
                "url": redact(r.final_url)[:2000],
                "host": t.host,
                "ip": r.ip,
                "status_code": r.status,
                "bytes": len(r.body),
                "content_type": r.content_type,
                "duration_ms": r.ms,
            },
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(True, {"dry_run": True, **prepared.canonical}, f"dry run: {prepared.summary}")


class SearchArgs(ToolArgs):
    query: str = Field(..., min_length=2, max_length=300)
    max_results: int = Field(8, ge=1, le=20)


class WebSearch:
    name = "web.search"
    category: ClassVar[Literal["web"]] = "web"
    description = "Search the web (DuckDuckGo). Returns titles, URLs and snippets; use web_fetch to read a result."
    risk: ClassVar[Risk] = "network"
    Args = SearchArgs
    timeout_s: ClassVar[float] = 30.0

    def __init__(self, w: WebContext) -> None:
        self.w = w

    def prepare(self, args: SearchArgs, ctx: ToolContext) -> Prepared:
        if self.w.settings.search.provider == "none":
            raise ToolInputError("web search is not configured (web.search.provider = none)")
        if ctx.snapshot.effective_internet_mode() == "disabled":
            cap: Decision | None = Decision("deny", "capability:internet_disabled", "internet access is disabled")
        else:
            cap = None
        if any(f.level == "secret" for f in scan(args.query)):
            raise GuardrailViolation("the search query contains what looks like a secret", "secret_in_query")
        t = build_target(f"https://{SEARCH_HOST}/html/?q={quote_plus(args.query)}", [443], listed_private=lambda h, p: False)
        return Prepared(
            PermissionRequest(self.name, "network", {"domain": SEARCH_HOST}, cap, False),
            {"query": args.query, "max_results": args.max_results},
            f"search “{args.query[:80]}”",
            (t, args),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        t, args = prepared.payload
        r = await self.w.client.fetch(t, private_ok=False, listed_private=lambda h, p: False, accept_types=["text/html"])
        results = parse_duckduckgo(r.body.decode("utf-8", "replace"), args.max_results)
        return ToolResult(
            True,
            {"query": args.query, "results": results},
            f"{len(results)} results",
            untrusted_source="web:search",
            detail_kind="web",
            detail={
                "method": "GET",
                "url": f"https://{SEARCH_HOST}/html/?q=[query]",
                "host": SEARCH_HOST,
                "ip": r.ip,
                "status_code": r.status,
                "bytes": len(r.body),
                "content_type": r.content_type,
                "duration_ms": r.ms,
            },
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(True, {"dry_run": True, **prepared.canonical}, f"dry run: {prepared.summary}")


def parse_duckduckgo(html: str, limit: int) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    out: list[dict[str, str]] = []
    for res in tree.css(".result"):
        a = res.css_first(".result__a")
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        if "uddg=" in href:
            href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
        if not href.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in href:
            continue
        snip = res.css_first(".result__snippet")
        out.append(
            {
                "title": a.text(strip=True)[:200],
                "url": href[:1000],
                "snippet": re.sub(r"\s+", " ", snip.text(strip=True))[:400] if snip else "",
            }
        )
        if len(out) >= limit:
            break
    return out


class RequestArgs(ToolArgs):
    url: str = Field(..., min_length=8, max_length=4096)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict, max_length=10)
    body: str | None = Field(None, max_length=100_000)
    dry_run: bool = False


class WebRequest:
    name = "web.request"
    category: ClassVar[Literal["web"]] = "web"
    description = (
        "HTTP request with a method (GET/HEAD/POST/PUT/PATCH/DELETE) and optional JSON/text body. "
        "Non-GET methods only work for domains where you enabled them (or in Bypass mode)."
    )
    risk: ClassVar[Risk] = "network"
    Args = RequestArgs
    timeout_s: ClassVar[float] = 40.0

    def __init__(self, w: WebContext) -> None:
        self.w = w

    def prepare(self, args: RequestArgs, ctx: ToolContext) -> Prepared:
        _check_exfiltration(args.url)
        bad = [k for k in args.headers if k.lower() not in {"accept", "accept-language", "content-type"}]
        if bad:
            raise ToolInputError(f"headers not allowed: {', '.join(bad)} (allowed: accept, accept-language, content-type)")
        t = build_target(args.url, self.w.settings.allowed_ports, listed_private=_listed(ctx.snapshot))
        cap, sensitive = _capability(ctx.snapshot, t, args.method)
        risk: Risk = "network" if args.method in ("GET", "HEAD") else ("destructive" if args.method == "DELETE" else "write")
        size = len(args.body.encode()) if args.body else 0
        return Prepared(
            PermissionRequest(self.name, risk, {"domain": t.host}, cap, sensitive),
            {"method": args.method, "url": redact(t.url), "body_bytes": size},
            f"{args.method} {redact(t.url)[:150]}" + (f" ({size} bytes)" if size else ""),
            (t, args),
        )

    async def run(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        t, args = prepared.payload
        r = await self.w.client.fetch(
            t,
            method=args.method,
            headers=args.headers,
            body=args.body.encode() if args.body else None,
            private_ok=ctx.snapshot.bypass_on("private_network"),
            listed_private=_listed(ctx.snapshot),
        )
        content = body_to_text(r.body, r.content_type, r.final_url, 20000)
        return ToolResult(
            200 <= r.status < 400,
            {"url": r.final_url, "status": r.status, "content_type": r.content_type, **content},
            f"{args.method} → {r.status}",
            untrusted_source=f"web:{t.host}",
            detail_kind="web",
            detail={
                "method": args.method,
                "url": redact(r.final_url)[:2000],
                "host": t.host,
                "ip": r.ip,
                "status_code": r.status,
                "bytes": len(r.body),
                "content_type": r.content_type,
                "duration_ms": r.ms,
            },
        )

    async def preview(self, prepared: Prepared, ctx: ToolContext) -> ToolResult:
        return ToolResult(True, {"dry_run": True, **prepared.canonical}, f"dry run: {prepared.summary}")


def web_tools(w: WebContext) -> list[Any]:
    return [WebFetch(w), WebSearch(w), WebRequest(w)]
