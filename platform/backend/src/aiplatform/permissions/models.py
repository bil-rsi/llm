"""Permission domain: modes, rules, roots, network allowlist and the immutable snapshot the engine evaluates.

A PermissionSnapshot is built from the database (human-managed) and never from model output. The engine only ever
receives it read-only; nothing reachable from the tool loop can write permission tables.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

Mode = Literal["normal", "autonomous", "bypass"]
Effect = Literal["allow", "confirm", "deny"]
Risk = Literal["read", "write", "destructive", "execute", "network"]
InternetMode = Literal["disabled", "restricted", "trusted", "unrestricted"]
ScopeType = Literal["any", "path", "domain", "command"]

MODES: tuple[Mode, ...] = ("normal", "autonomous", "bypass")
EFFECT_STRENGTH: dict[str, int] = {"allow": 0, "confirm": 1, "deny": 2}


@dataclass(frozen=True)
class Rule:
    id: str
    tool_pattern: str
    mode: str
    scope_type: ScopeType
    scope_pattern: str
    effect: Effect
    priority: int
    note: str = ""

    def matches_tool(self, tool: str) -> bool:
        if self.tool_pattern in ("*", tool):
            return True
        return self.tool_pattern.endswith(".*") and tool.startswith(self.tool_pattern[:-1])

    def matches_scope(self, scopes: dict[str, str]) -> bool:
        if self.scope_type == "any":
            return True
        value = scopes.get(self.scope_type)
        if value is None:
            return False
        if self.scope_type == "domain":
            p = self.scope_pattern.lower()
            v = value.lower()
            return v == p or fnmatch.fnmatchcase(v, p) or (p.startswith("*.") and v == p[2:])
        # path globs: `**` crosses directories, `*` does not
        return _glob_path(value, self.scope_pattern)

    @property
    def specificity(self) -> int:
        s = 0 if self.tool_pattern == "*" else (1 if self.tool_pattern.endswith(".*") else 2)
        return s + (2 if self.scope_type != "any" else 0) + (1 if self.mode != "*" else 0)


def _glob_path(path: str, pattern: str) -> bool:
    import re

    rx = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                rx = rx[:-2] + "(?:.*/)?"
                i += 1
            continue
        rx += "[^/]*" if c == "*" else ("[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.fullmatch(rx, path) is not None or (pattern.endswith("/**") and path == pattern[:-3])


@dataclass(frozen=True)
class Root:
    name: str
    host_path: str
    container_path: str
    access: Literal["ro", "rw"]
    kind: Literal["zone", "extra"]
    enabled: bool = True
    bypass_only: bool = False


@dataclass(frozen=True)
class NetworkEntry:
    kind: Literal["public_domain", "private_host"]
    host: str
    port: int | None
    methods: tuple[str, ...]

    def matches_host(self, host: str) -> bool:
        h, p = host.lower(), self.host.lower()
        return h == p or (p.startswith("*.") and (h.endswith(p[1:]) or h == p[2:]))


@dataclass(frozen=True)
class BypassCapabilities:
    extra_folders: bool = True
    broad_shell: bool = True
    private_network: bool = True
    unrestricted_internet: bool = True


@dataclass(frozen=True)
class PermissionSnapshot:
    version: int
    mode: Mode
    internet_mode: InternetMode
    taint_escalation: bool
    shell_network: bool
    bypass: BypassCapabilities
    defaults: dict[str, dict[str, Effect]]
    rules: tuple[Rule, ...]
    roots: tuple[Root, ...]
    network: tuple[NetworkEntry, ...]
    tools_enabled: dict[str, bool] = field(default_factory=dict)

    def bypass_on(self, capability: str) -> bool:
        return self.mode == "bypass" and bool(getattr(self.bypass, capability))

    def effective_internet_mode(self) -> InternetMode:
        return "unrestricted" if self.bypass_on("unrestricted_internet") else self.internet_mode

    def public_entry(self, host: str) -> NetworkEntry | None:
        return next((e for e in self.network if e.kind == "public_domain" and e.matches_host(host)), None)

    def private_entry(self, host: str, port: int) -> NetworkEntry | None:
        return next(
            (e for e in self.network if e.kind == "private_host" and e.matches_host(host) and (e.port is None or e.port == port)),
            None,
        )

    def usable_roots(self) -> tuple[Root, ...]:
        return tuple(
            r for r in self.roots if r.enabled and ((not r.bypass_only and r.kind == "zone") or self.bypass_on("extra_folders"))
        )


@dataclass(frozen=True)
class Decision:
    effect: Effect
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


@dataclass(frozen=True)
class SettingsChange:
    key: str
    value: object
    by_user: UUID
