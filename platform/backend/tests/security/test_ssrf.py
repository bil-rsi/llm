"""SSRF guard: URL forms, IP classification, resolver-based blocking and redirect re-validation."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import pytest

from aiplatform.config import WebSettings
from aiplatform.tools.base import GuardrailViolation, ToolInputError
from aiplatform.tools.web.client import GuardedClient
from aiplatform.tools.web.ssrf import InternalNetworks, build_target, classify, resolve_checked

NOT_LISTED = lambda h, p: False  # noqa: E731

BLOCKED_URLS = [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://localhost:8090/admin",
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/",
    "http://host.docker.internal:11434/",
    "http://postgres:5432/",
    "http://embed:8081/v1/embeddings",
    "http://backend:8090/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:25/",
    "ftp://example.com/",
    "http://user:pass@example.com/",
    "http://example.com@127.0.0.1/",
    "http://example.com:22/",
    "http://printer.local/",
    "http://app.localhost/",
    "javascript:alert(1)",
    "data:text/html,hi",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_blocked_by_url_rules(url: str) -> None:
    with pytest.raises((GuardrailViolation, ToolInputError)):
        build_target(url, [80, 443], listed_private=NOT_LISTED)


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("8.8.8.8", "public"),
        ("1.1.1.1", "public"),
        ("10.0.0.5", "private"),
        ("192.168.1.10", "private"),
        ("172.16.3.4", "private"),
        ("100.64.0.1", "private"),
        ("127.0.0.1", "blocked"),
        ("169.254.169.254", "blocked"),
        ("0.0.0.0", "blocked"),
        ("224.0.0.1", "blocked"),
        ("::1", "blocked"),
        ("fe80::1", "blocked"),
        ("fc00::1", "private"),
        ("::ffff:127.0.0.1", "blocked"),
        ("::ffff:10.0.0.1", "private"),
        ("2002:7f00:1::", "blocked"),
        ("64:ff9b::a9fe:a9fe", "blocked"),
        ("2606:4700:4700::1111", "public"),
    ],
)
def test_classify(ip: str, expected: str) -> None:
    assert classify(ipaddress.ip_address(ip), []) == expected


def test_internal_docker_subnet_blocked() -> None:
    assert classify(ipaddress.ip_address("172.18.0.5"), [ipaddress.ip_network("172.18.0.0/16")]) == "blocked"


class FixedResolver:
    """Patches loop.getaddrinfo to simulate DNS answers (incl. rebinding to internal addresses)."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers

    async def __call__(self, host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        if host not in self.answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in self.answers[host]]


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> FixedResolver:
    import asyncio

    r = FixedResolver(
        {
            "example.com": ["93.184.216.34"],
            "evil-rebind.test": ["93.184.216.34", "127.0.0.1"],
            "lan-name.test": ["192.168.1.20"],
            "meta.test": ["169.254.169.254"],
            "host.docker.internal": ["192.168.65.254"],
        }
    )
    loop = asyncio.get_event_loop_policy().get_event_loop()
    monkeypatch.setattr(type(loop), "getaddrinfo", lambda self, *a, **k: r(*a, **k))
    return r


class NoNets(InternalNetworks):
    async def get(self) -> list[Any]:
        return []


async def test_dns_to_internal_blocked(dns: FixedResolver) -> None:
    for host in ("evil-rebind.test", "lan-name.test", "meta.test"):
        t = build_target(f"https://{host}/", [80, 443], listed_private=NOT_LISTED)
        with pytest.raises(GuardrailViolation):
            await resolve_checked(t, NoNets(), private_ok=False)


async def test_public_resolves(dns: FixedResolver) -> None:
    t = build_target("https://example.com/x?q=1", [80, 443], listed_private=NOT_LISTED)
    assert await resolve_checked(t, NoNets(), private_ok=False) == "93.184.216.34"


async def test_listed_private_host_only_in_bypass(dns: FixedResolver) -> None:
    listed = lambda h, p: h == "lan-name.test"  # noqa: E731
    t = build_target("http://lan-name.test/", [80, 443], listed_private=listed)
    assert await resolve_checked(t, NoNets(), private_ok=True) == "192.168.1.20"
    with pytest.raises(GuardrailViolation):
        await resolve_checked(t, NoNets(), private_ok=False)


def test_localhost_listed_maps_to_host_gateway_but_protected_ports_blocked() -> None:
    listed = lambda h, p: h == "localhost"  # noqa: E731
    t = build_target("http://localhost:3000/", [80, 443], listed_private=listed)
    assert t.connect_host == "host.docker.internal" and t.private_intent
    for port in (11434, 8080, 8081, 8090, 5432):
        with pytest.raises(GuardrailViolation):
            build_target(f"http://localhost:{port}/", [80, 443], listed_private=listed)


async def test_redirect_to_internal_blocked(dns: FixedResolver) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    client = GuardedClient(WebSettings(), NoNets(), transport=httpx.MockTransport(handler))
    t = build_target("http://example.com/start", [80, 443], listed_private=NOT_LISTED)
    with pytest.raises((GuardrailViolation, ToolInputError)):
        await client.fetch(t, private_ok=False, listed_private=NOT_LISTED)


async def test_size_and_content_type_limits(dns: FixedResolver) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/big":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5000)
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01")

    s = WebSettings(max_bytes=1000, content_types=["text/plain"])
    client = GuardedClient(s, NoNets(), transport=httpx.MockTransport(handler))
    r = await client.fetch(
        build_target("http://example.com/big", [80], listed_private=NOT_LISTED), private_ok=False, listed_private=NOT_LISTED
    )
    assert r.truncated and len(r.body) == 1000
    with pytest.raises(ToolInputError):
        await client.fetch(
            build_target("http://example.com/bin", [80], listed_private=NOT_LISTED), private_ok=False, listed_private=NOT_LISTED
        )


async def test_connects_to_vetted_ip_with_original_host(dns: FixedResolver) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["hdr"] = request.headers["host"]
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hi")

    client = GuardedClient(WebSettings(content_types=["text/plain"]), NoNets(), transport=httpx.MockTransport(handler))
    await client.fetch(
        build_target("http://example.com/", [80], listed_private=NOT_LISTED), private_ok=False, listed_private=NOT_LISTED
    )
    assert seen == {"host": "93.184.216.34", "hdr": "example.com"}
