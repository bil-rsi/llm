"""Permission repository + service: builds cached snapshots, seeds from permissions.yaml, applies human-only changes.

Only the admin API (human session, CSRF, and step-up auth for sensitive changes) calls the mutating methods.
The tool loop only ever calls `snapshot()`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from uuid import UUID

import yaml

from aiplatform.audit.service import AuditLog
from aiplatform.db import Database
from aiplatform.permissions.models import (
    MODES,
    BypassCapabilities,
    Effect,
    NetworkEntry,
    PermissionSnapshot,
    Root,
    Rule,
)
from aiplatform.shared.errors import NotFound, ValidationFailed

SETTING_KEYS = {"mode", "internet_mode", "taint_escalation", "shell_network", "bypass", "defaults"}
# Changing these needs step-up auth (password re-entry within the elevation window).
SENSITIVE_KEYS = {"mode", "internet_mode", "bypass", "defaults", "shell_network"}
_EFFECTS = {"allow", "confirm", "deny"}
_RISKS = {"read", "write", "destructive", "execute", "network"}

# Host folders that can never be mounted as extra roots (checked on the Windows path, case-insensitive).
_FORBIDDEN_MOUNTS = [
    re.compile(r"^[a-z]:\\?$"),  # drive roots
    re.compile(r"^[a-z]:\\windows(\\|$)"),
    re.compile(r"^[a-z]:\\program files( \(x86\))?(\\|$)"),
    re.compile(r"^[a-z]:\\programdata(\\|$)"),
    re.compile(r"^[a-z]:\\users\\?$"),
    re.compile(r"^[a-z]:\\users\\[^\\]+\\?$"),  # a whole user profile
    re.compile(r"^[a-z]:\\users\\[^\\]+\\appdata(\\|$)"),
    re.compile(r"^[a-z]:\\users\\[^\\]+\\\.(ssh|aws|azure|docker|kube|gnupg)(\\|$)"),
    re.compile(r"^[a-z]:\\\$recycle\.bin(\\|$)"),
    re.compile(r"^[a-z]:\\system volume information(\\|$)"),
    re.compile(r"^\\\\"),  # UNC / device paths
]


def check_mountable(host_path: str, platform_dir: str | None = None) -> str:
    """Validate a Windows folder for mounting as an extra root. Returns the normalised path or raises."""
    p = host_path.strip().replace("/", "\\")
    if not re.fullmatch(r"[A-Za-z]:\\[^<>:\"|?*\x00-\x1f]*", p) or ".." in PureWindowsPath(p).parts:
        raise ValidationFailed("Use an absolute Windows folder like D:\\Projects (no .., wildcards or UNC paths)")
    norm = str(PureWindowsPath(p)).rstrip("\\") or p
    low = norm.lower()
    if any(rx.match(low) for rx in _FORBIDDEN_MOUNTS):
        raise ValidationFailed(f"{norm} is a protected system/profile location and cannot be mounted")
    if platform_dir and (low + "\\").startswith(platform_dir.lower().rstrip("\\") + "\\"):
        raise ValidationFailed("The platform folder (it holds the secrets) cannot be mounted")
    return norm


class PermissionService:
    def __init__(self, db: Database, audit: AuditLog, seed_path: Path, runtime_dir: Path) -> None:
        self.db = db
        self.audit = audit
        self.seed_path = seed_path
        self.runtime_dir = runtime_dir
        self._snap: PermissionSnapshot | None = None
        self._version = 0

    # ───────────── reads ─────────────
    async def snapshot(self) -> PermissionSnapshot:
        if self._snap is None:
            self._snap = await self._load()
        return self._snap

    def invalidate(self) -> None:
        self._snap = None

    async def _load(self) -> PermissionSnapshot:
        settings = {r["key"]: r["value"] for r in await self.db.fetch("SELECT key, value FROM app.permission_settings")}
        rules = tuple(
            Rule(
                str(r["id"]),
                r["tool_pattern"],
                r["mode"],
                r["scope_type"],
                r["scope_pattern"],
                r["effect"],
                r["priority"],
                r["note"],
            )
            for r in await self.db.fetch("SELECT * FROM app.tool_permissions WHERE enabled ORDER BY priority DESC")
        )
        roots = tuple(
            Root(r["name"], r["host_path"], r["container_path"], r["access"], r["kind"], r["enabled"], r["bypass_only"])
            for r in await self.db.fetch("SELECT * FROM app.workspace_roots ORDER BY kind DESC, name")
        )
        network = tuple(
            NetworkEntry(r["kind"], r["host"], r["port"], tuple(r["methods"]))
            for r in await self.db.fetch("SELECT * FROM app.network_allowlist WHERE enabled")
        )
        tools = {r["name"]: r["enabled"] for r in await self.db.fetch("SELECT name, enabled FROM app.tool_definitions")}
        bp = settings.get("bypass") or {}
        self._version += 1
        return PermissionSnapshot(
            version=self._version,
            mode=settings.get("mode", "normal"),
            internet_mode=settings.get("internet_mode", "restricted"),
            taint_escalation=bool(settings.get("taint_escalation", False)),
            shell_network=bool(settings.get("shell_network", False)),
            bypass=BypassCapabilities(**{k: bool(bp.get(k, True)) for k in BypassCapabilities.__dataclass_fields__}),
            defaults=settings.get("defaults") or {},
            rules=rules,
            roots=roots,
            network=network,
            tools_enabled=tools,
        )

    async def list_rules(self) -> list[dict[str, Any]]:
        return [
            dict(r) for r in await self.db.fetch("SELECT * FROM app.tool_permissions ORDER BY mode, priority DESC, tool_pattern")
        ]

    async def list_roots(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch("SELECT * FROM app.workspace_roots ORDER BY kind DESC, name")]

    async def list_network(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch("SELECT * FROM app.network_allowlist ORDER BY kind, host")]

    async def settings(self) -> dict[str, Any]:
        return {r["key"]: r["value"] for r in await self.db.fetch("SELECT key, value FROM app.permission_settings")}

    # ───────────── seed ─────────────
    async def seed_if_empty(self) -> bool:
        if await self.db.fetchval("SELECT count(*) FROM app.permission_settings") > 0:
            return False
        await self.apply_seed(by_user=None)
        return True

    async def apply_seed(self, by_user: UUID | None) -> None:
        data = yaml.safe_load(self.seed_path.read_text(encoding="utf-8")) or {}
        s = data.get("settings", {})
        values: dict[str, Any] = {
            k: s[k] for k in ("mode", "internet_mode", "taint_escalation", "shell_network", "bypass") if k in s
        }
        values["defaults"] = data.get("defaults", {})
        for k, v in values.items():
            _validate_setting(k, v)
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM app.tool_permissions WHERE origin = 'seed'")
            for k, v in values.items():
                await tx.execute(
                    "INSERT INTO app.permission_settings (key, value, updated_by) VALUES ($1, $2, $3) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
                    "updated_by = EXCLUDED.updated_by",
                    k,
                    v,
                    by_user,
                )
            for r in data.get("rules", []):
                scope = r.get("scope") or {}
                st, sp = next(iter(scope.items())) if scope else ("any", "*")
                await tx.execute(
                    "INSERT INTO app.tool_permissions (tool_pattern, mode, scope_type, scope_pattern, effect, priority, "
                    "note, origin) VALUES ($1,$2,$3,$4,$5,$6,$7,'seed')",
                    r["tool"],
                    r.get("mode", "*"),
                    st,
                    str(sp),
                    r["effect"],
                    int(r.get("priority", 100)),
                    r.get("note", ""),
                )
            for root in data.get("roots", []):
                await tx.execute(
                    "INSERT INTO app.workspace_roots (name, host_path, container_path, access, kind) "
                    "VALUES ($1,$2,$3,$4,'zone') ON CONFLICT (name) DO NOTHING",
                    root["name"],
                    root["host"],
                    f"/workspace/{root['host']}",
                    root["access"],
                )
            net = data.get("network", {})
            for d in net.get("public_domains", []):
                await tx.execute(
                    "INSERT INTO app.network_allowlist (kind, host) VALUES ('public_domain', $1) ON CONFLICT DO NOTHING",
                    str(d).lower(),
                )
            for h in net.get("private_hosts", []):
                await tx.execute(
                    "INSERT INTO app.network_allowlist (kind, host, port) VALUES ('private_host', $1, $2) ON CONFLICT DO NOTHING",
                    str(h["host"]).lower(),
                    h.get("port"),
                )
        await self.audit.append(
            "permissions.seed",
            actor_type="user" if by_user else "system",
            actor_id=str(by_user) if by_user else None,
            target=str(self.seed_path),
            outcome="success",
        )
        self.invalidate()

    # ───────────── human-only mutations ─────────────
    async def set_setting(self, key: str, value: Any, by_user: UUID) -> None:
        _validate_setting(key, value)
        old = await self.db.fetchval("SELECT value FROM app.permission_settings WHERE key = $1", key)
        await self.db.execute(
            "INSERT INTO app.permission_settings (key, value, updated_by) VALUES ($1, $2, $3) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
            "updated_by = EXCLUDED.updated_by",
            key,
            value,
            by_user,
        )
        await self.audit.append(
            "permissions.setting",
            actor_type="user",
            actor_id=str(by_user),
            target=key,
            outcome="success",
            detail={"old": old, "new": value},
        )
        if key == "shell_network":
            self._write_runtime_request()
        self.invalidate()

    async def upsert_rule(self, data: dict[str, Any], by_user: UUID, rule_id: UUID | None = None) -> str:
        r = _validate_rule(data)
        if rule_id is None:
            rid = await self.db.fetchval(
                "INSERT INTO app.tool_permissions (tool_pattern, mode, scope_type, scope_pattern, effect, priority, "
                "note, enabled, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
                r["tool_pattern"],
                r["mode"],
                r["scope_type"],
                r["scope_pattern"],
                r["effect"],
                r["priority"],
                r["note"],
                r["enabled"],
                by_user,
            )
        else:
            rid = await self.db.fetchval(
                "UPDATE app.tool_permissions SET tool_pattern=$2, mode=$3, scope_type=$4, scope_pattern=$5, effect=$6, "
                "priority=$7, note=$8, enabled=$9, origin='admin' WHERE id=$1 RETURNING id",
                rule_id,
                r["tool_pattern"],
                r["mode"],
                r["scope_type"],
                r["scope_pattern"],
                r["effect"],
                r["priority"],
                r["note"],
                r["enabled"],
            )
            if rid is None:
                raise NotFound("rule not found")
        await self.audit.append(
            "permissions.rule_upsert", actor_type="user", actor_id=str(by_user), target=str(rid), outcome="success", detail=r
        )
        self.invalidate()
        return str(rid)

    async def delete_rule(self, rule_id: UUID, by_user: UUID) -> None:
        row = await self.db.fetchrow("DELETE FROM app.tool_permissions WHERE id=$1 RETURNING tool_pattern, mode, effect", rule_id)
        if row is None:
            raise NotFound("rule not found")
        await self.audit.append(
            "permissions.rule_delete",
            actor_type="user",
            actor_id=str(by_user),
            target=str(rule_id),
            outcome="success",
            detail=dict(row),
        )
        self.invalidate()

    async def set_tool_enabled(self, name: str, enabled: bool, by_user: UUID) -> None:
        if (
            await self.db.fetchval("UPDATE app.tool_definitions SET enabled=$2 WHERE name=$1 RETURNING name", name, enabled)
            is None
        ):
            raise NotFound("tool not found")
        await self.audit.append(
            "permissions.tool_enabled",
            actor_type="user",
            actor_id=str(by_user),
            target=name,
            outcome="success",
            detail={"enabled": enabled},
        )
        self.invalidate()

    async def add_extra_root(self, name: str, host_path: str, access: str, by_user: UUID, platform_dir: str | None) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name) or name in {"allowed", "projects", "sandbox"}:
            raise ValidationFailed("name: lowercase letters, digits, - or _, max 40 chars, not a default zone name")
        if access not in ("ro", "rw"):
            raise ValidationFailed("access must be ro or rw")
        norm = check_mountable(host_path, platform_dir)
        await self.db.execute(
            "INSERT INTO app.workspace_roots (name, host_path, container_path, access, kind, bypass_only) "
            "VALUES ($1,$2,$3,$4,'extra',true)",
            name,
            norm,
            f"/extra/{name}",
            access,
        )
        await self.audit.append(
            "permissions.root_add",
            actor_type="user",
            actor_id=str(by_user),
            target=norm,
            outcome="success",
            detail={"name": name, "access": access},
        )
        self._write_runtime_request()
        self.invalidate()

    async def remove_root(self, name: str, by_user: UUID) -> None:
        row = await self.db.fetchrow("DELETE FROM app.workspace_roots WHERE name=$1 AND kind='extra' RETURNING host_path", name)
        if row is None:
            raise NotFound("extra folder not found (default zones cannot be removed)")
        await self.audit.append(
            "permissions.root_remove", actor_type="user", actor_id=str(by_user), target=row["host_path"], outcome="success"
        )
        self._write_runtime_request()
        self.invalidate()

    async def add_network(
        self, kind: str, host: str, port: int | None, methods: list[str], by_user: UUID, note: str = ""
    ) -> None:
        h = host.strip().lower()
        if kind not in ("public_domain", "private_host"):
            raise ValidationFailed("kind must be public_domain or private_host")
        if not re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:.]+\]|[0-9.]+", h):
            raise ValidationFailed("host must be a domain (optionally *.domain), an IPv4 address or [IPv6]")
        ms = sorted({m.upper() for m in methods} or {"GET", "HEAD"})
        if not set(ms) <= {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValidationFailed("unsupported method")
        if port is not None and not 1 <= port <= 65535:
            raise ValidationFailed("port out of range")
        await self.db.execute(
            "INSERT INTO app.network_allowlist (kind, host, port, methods, note) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (kind, host, port) DO UPDATE SET methods=EXCLUDED.methods, enabled=true, note=EXCLUDED.note",
            kind,
            h,
            port,
            ms,
            note[:200],
        )
        await self.audit.append(
            "permissions.network_add",
            actor_type="user",
            actor_id=str(by_user),
            target=h,
            outcome="success",
            detail={"kind": kind, "port": port, "methods": ms},
        )
        self.invalidate()

    async def remove_network(self, entry_id: UUID, by_user: UUID) -> None:
        row = await self.db.fetchrow("DELETE FROM app.network_allowlist WHERE id=$1 RETURNING host, kind", entry_id)
        if row is None:
            raise NotFound("entry not found")
        await self.audit.append(
            "permissions.network_remove",
            actor_type="user",
            actor_id=str(by_user),
            target=row["host"],
            outcome="success",
            detail={"kind": row["kind"]},
        )
        self.invalidate()

    def _write_runtime_request(self) -> None:
        """Signal platform-up.ps1 that mounts/network changed (it regenerates compose.mounts.yaml from the DB export)."""
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / "restart-required.json").write_text(json.dumps({"reason": "mounts or shell network changed"}))
        except OSError:
            pass


def _validate_setting(key: str, value: Any) -> None:
    if key not in SETTING_KEYS:
        raise ValidationFailed(f"unknown setting {key}")
    ok = {
        "mode": lambda v: v in MODES,
        "internet_mode": lambda v: v in ("disabled", "restricted", "trusted", "unrestricted"),
        "taint_escalation": lambda v: isinstance(v, bool),
        "shell_network": lambda v: isinstance(v, bool),
        "bypass": lambda v: (
            isinstance(v, dict)
            and set(v) <= set(BypassCapabilities.__dataclass_fields__)
            and all(isinstance(x, bool) for x in v.values())
        ),
        "defaults": lambda v: (
            isinstance(v, dict)
            and set(v) <= set(MODES)
            and all(isinstance(m, dict) and set(m) <= _RISKS and set(m.values()) <= _EFFECTS for m in v.values())
        ),
    }[key](value)
    if not ok:
        raise ValidationFailed(f"invalid value for {key}")


def _validate_rule(d: dict[str, Any]) -> dict[str, Any]:
    tool = str(d.get("tool_pattern", "")).strip()
    if not re.fullmatch(r"\*|[a-z]+\.(\*|[a-z_]+)", tool):
        raise ValidationFailed("tool_pattern: '*', 'category.*' or 'category.name'")
    mode = d.get("mode", "*")
    if mode not in ("*", *MODES):
        raise ValidationFailed("mode: *, normal, autonomous or bypass")
    st = d.get("scope_type", "any")
    if st not in ("any", "path", "domain", "command"):
        raise ValidationFailed("scope_type: any, path, domain or command")
    sp = str(d.get("scope_pattern") or "*").strip()
    if not 1 <= len(sp) <= 500 or "\x00" in sp:
        raise ValidationFailed("scope_pattern length 1..500")
    eff = d.get("effect")
    if eff not in _EFFECTS:
        raise ValidationFailed("effect: allow, confirm or deny")
    try:
        prio = int(d.get("priority", 100))
    except (TypeError, ValueError) as e:
        raise ValidationFailed("priority must be an integer") from e
    return {
        "tool_pattern": tool,
        "mode": mode,
        "scope_type": st,
        "scope_pattern": sp,
        "effect": cast(Effect, eff),
        "priority": max(-1000, min(1000, prio)),
        "note": str(d.get("note", ""))[:300],
        "enabled": bool(d.get("enabled", True)),
    }
