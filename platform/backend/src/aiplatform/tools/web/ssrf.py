"""SSRF guard: URL validation, name/IP classification and the always-on hard blocks.

Hard blocks (every mode, including Bypass): container loopback (the backend itself), link-local / cloud metadata,
the platform's own Docker networks and service names, multicast/reserved space, and the host ports of the model
runtimes and the platform (11434, 8080, 8081, 8090, 5432). Private/LAN/localhost targets are reachable only in Bypass
mode with private_network on AND an explicit allowlist entry. `localhost:<port>` in the allowlist means *your PC*
(reached through host.docker.internal), never the container.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiplatform.tools.base import GuardrailViolation, ToolInputError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

SERVICE_NAMES = {"postgres", "embed", "tool-runner", "backend", "migrate", "backup", "searxng", "aiplatform"}
BLOCKED_NAMES = re.compile(
    r"^(localhost|.*\.localhost|host\.docker\.internal|gateway\.docker\.internal|"
    r"kubernetes\.docker\.internal|.*\.internal|metadata|metadata\.google\.internal|"
    r"instance-data|.*\.local)$"
)
LOCALHOST_ALIASES = {"localhost", "127.0.0.1", "::1", "[::1]"}
HOST_GATEWAY = "host.docker.internal"
PROTECTED_HOST_PORTS = {5432, 8080, 8081, 8090, 11434}
_OBFUSCATED_IP = re.compile(r"^((0x[0-9a-f]+|\d+)\.){0,3}(0x[0-9a-f]+|\d+)$", re.IGNORECASE)
_ALWAYS_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::/32",
    )
]


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # normalised (IDNA, lowercase), as the user/model wrote it
    port: int
    path_query: str
    connect_host: str  # what we resolve/connect to (host.docker.internal for localhost aliases)
    private_intent: bool  # the caller asked for an allowlisted private/LAN/localhost target

    @property
    def url(self) -> str:
        default = (self.scheme == "https" and self.port == 443) or (self.scheme == "http" and self.port == 80)
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        return urlunsplit((self.scheme, netloc if default else f"{netloc}:{self.port}", self.path_query or "/", "", ""))


def parse_url(url: str, allowed_ports: list[int]) -> tuple[str, str, int, str]:
    if not isinstance(url, str) or len(url) > 4096 or any(c in url for c in "\x00\r\n\t "):
        raise ToolInputError("invalid URL")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError as e:
        raise ToolInputError(f"invalid URL: {e}") from e
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise GuardrailViolation(f"scheme '{scheme or '?'}' is not allowed (http/https only)", "scheme")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise GuardrailViolation("credentials in URLs are not allowed", "userinfo")
    raw_host = (u.hostname or "").rstrip(".")
    if not raw_host:
        raise ToolInputError("URL has no host")
    try:
        host = raw_host.encode("idna").decode("ascii").lower() if not _is_ip(raw_host) else raw_host.lower()
    except UnicodeError as e:
        raise ToolInputError("invalid international domain name") from e
    if not _is_ip(host) and _OBFUSCATED_IP.match(host):
        raise GuardrailViolation("numeric/hex/octal host forms are not allowed; use a normal IP or name", "ip_obfuscation")
    port = port or (443 if scheme == "https" else 80)
    path_query = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return scheme, host, port, path_query


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def build_target(url: str, allowed_ports: list[int], *, listed_private: Callable[[str, int], bool]) -> Target:
    """Validate the URL and apply name-level hard blocks. IP-level checks happen at connect time (resolve_checked).

    `listed_private(host, port)` is True only in Bypass mode (private_network on) for hosts on your LAN allowlist.
    """
    scheme, host, port, pq = parse_url(url, allowed_ports)
    listed = listed_private(host, port)
    if host in SERVICE_NAMES:
        raise GuardrailViolation("platform-internal services are never reachable from tools", "internal_service")
    if host == HOST_GATEWAY or host.endswith(".docker.internal"):
        raise GuardrailViolation("Docker host gateway names are blocked; list localhost:<port> instead", "internal_name")
    if host in LOCALHOST_ALIASES or host.endswith(".localhost"):
        if not listed:
            raise GuardrailViolation("localhost is blocked (only listed ports, in Bypass mode, are reachable)", "localhost")
        if port in PROTECTED_HOST_PORTS:
            raise GuardrailViolation(
                f"port {port} on your PC is a model/platform endpoint and is always blocked", "protected_port"
            )
        return Target(scheme, host, port, pq, HOST_GATEWAY, True)
    if _is_ip(host):
        kind = classify(ipaddress.ip_address(host.strip("[]")), [])
        if kind == "blocked":
            raise GuardrailViolation(
                f"{host} is a blocked address (loopback, link-local/metadata or reserved)", "blocked_address"
            )
        if kind == "private" and not listed:
            raise GuardrailViolation(f"{host} is a private/LAN address; only listed hosts in Bypass mode", "private_address")
    if BLOCKED_NAMES.match(host) and not listed:
        raise GuardrailViolation(f"{host} is an internal name and is blocked", "internal_name")
    if port not in allowed_ports and not listed:
        raise GuardrailViolation(f"port {port} is not allowed (allowed: {allowed_ports})", "port")
    return Target(scheme, host, port, pq, host, listed)


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip.teredo:
            return ip.teredo[1]
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return ipaddress.IPv4Address(ip.packed[-4:])
    return ip


def classify(ip: IPAddress, internal_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> str:
    """'public' | 'private' | 'blocked'."""
    ip = _unwrap(ip)
    if (
        any(ip in n for n in _ALWAYS_BLOCKED_NETS)
        or any(ip in n for n in internal_nets)
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.is_reserved and not ip.is_private)
    ):
        return "blocked"
    return "public" if ip.is_global else "private"


class InternalNetworks:
    """Subnets this container is attached to (Docker networks) + IPs of platform services. Cached briefly."""

    def __init__(self, route_file: Path = Path("/proc/net/route")) -> None:
        self.route_file = route_file
        self._nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None

    async def get(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        if self._nets is None:
            nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
            try:
                for line in self.route_file.read_text().splitlines()[1:]:
                    f = line.split()
                    dest, mask = int(f[1], 16), int(f[7], 16)
                    if dest and mask:
                        nets.append(
                            ipaddress.ip_network((socket.inet_ntoa(struct.pack("<L", dest)), bin(mask).count("1")), strict=False)
                        )
            except (OSError, ValueError, IndexError):
                pass
            loop = asyncio.get_running_loop()
            for name in SERVICE_NAMES:
                try:
                    for info in await asyncio.wait_for(loop.getaddrinfo(name, None), 1):
                        nets.append(ipaddress.ip_network(info[4][0]))
                except (OSError, TimeoutError):
                    continue
            self._nets = nets
        return self._nets


async def resolve_checked(t: Target, internal: InternalNetworks, *, private_ok: bool) -> str:
    """Resolve once and return the IP to connect to. Every resolved address must pass; no rebinding window."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(t.connect_host, t.port, type=socket.SOCK_STREAM), 5)
    except (OSError, TimeoutError) as e:
        raise ToolInputError(f"cannot resolve {t.host}") from e
    nets = await internal.get()
    ips = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not ips:
        raise ToolInputError(f"cannot resolve {t.host}")
    for s in ips:
        kind = classify(ipaddress.ip_address(s.split("%")[0]), [] if t.connect_host == HOST_GATEWAY else nets)
        if kind == "blocked" and t.connect_host == HOST_GATEWAY and _unwrap(ipaddress.ip_address(s)).is_private:
            kind = "private"  # Docker Desktop's host gateway lives in a private range; allowed only for listed ports
        if kind == "blocked":
            raise GuardrailViolation(f"{t.host} resolves to a blocked internal address", "blocked_address")
        if kind == "private" and not (private_ok and t.private_intent):
            raise GuardrailViolation(
                f"{t.host} resolves to a private/LAN address; only listed hosts in Bypass mode", "private_address"
            )
    return ips[0]
