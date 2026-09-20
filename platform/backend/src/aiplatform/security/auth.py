"""Authentication: argon2id passwords, server-side sessions (+ CSRF + step-up elevation) and scoped API tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from aiplatform.audit.service import AuditLog
from aiplatform.db import Database
from aiplatform.shared.errors import RateLimited, Unauthorized, ValidationFailed

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_DUMMY_HASH = _ph.hash("timing-equaliser-not-a-password")
TOKEN_PREFIX = "aip_"  # noqa: S105 - a public prefix, not a secret
SCOPES = ("chat", "read", "admin")


def _h(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    username: str
    scopes: frozenset[str]
    via: str  # "session" | "token"
    session_id: UUID | None = None
    elevated: bool = False
    must_change_password: bool = False

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes


def validate_password(pw: str) -> None:
    if len(pw) < 12 or len(pw) > 256:
        raise ValidationFailed("password must be 12..256 characters")
    classes = sum(any(f(c) for c in pw) for f in (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    if classes < 3:
        raise ValidationFailed("use at least three of: lowercase, uppercase, digits, symbols")


class LoginThrottle:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < 60]
        if len(hits) >= self.per_minute:
            raise RateLimited("too many login attempts; wait a minute")
        hits.append(now)
        self._hits[key] = hits


class AuthService:
    def __init__(
        self, db: Database, audit: AuditLog, *, session_hours: int, elevation_minutes: int, login_per_minute: int
    ) -> None:
        self.db = db
        self.audit = audit
        self.session_ttl = timedelta(hours=session_hours)
        self.elevation = timedelta(minutes=elevation_minutes)
        self.throttle = LoginThrottle(login_per_minute)

    async def bootstrap_admin(self, password: str) -> bool:
        if await self.db.fetchval("SELECT count(*) FROM app.users") > 0:
            return False
        await self.db.execute(
            "INSERT INTO app.users (username, password_hash, must_change_password) VALUES ('admin', $1, true)", _ph.hash(password)
        )
        await self.audit.append("auth.bootstrap_admin", actor_type="system", target="admin", outcome="success")
        return True

    async def _verify(self, username: str, password: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT id, username, password_hash, must_change_password FROM app.users "
            "WHERE lower(username)=lower($1) AND disabled_at IS NULL",
            username,
        )
        try:
            _ph.verify(row["password_hash"] if row else _DUMMY_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        return dict(row) if row else None

    async def login(self, username: str, password: str, *, client_key: str, user_agent: str) -> tuple[str, str, bool]:
        """Returns (session token, csrf token, must_change_password)."""
        self.throttle.check(client_key)
        row = await self._verify(username[:64], password[:256])
        if row is None:
            await self.audit.append("auth.login", actor_type="user", target=username[:64], outcome="failure")
            raise Unauthorized("wrong username or password")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO app.sessions (user_id, token_hash, csrf_hash, expires_at, user_agent) VALUES ($1,$2,$3,$4,$5)",
            row["id"],
            _h(token),
            _h(csrf),
            datetime.now(UTC) + self.session_ttl,
            user_agent[:300],
        )
        await self.audit.append(
            "auth.login", actor_type="user", actor_id=str(row["id"]), target=row["username"], outcome="success"
        )
        return token, csrf, bool(row["must_change_password"])

    async def session_principal(self, token: str) -> tuple[Principal, bytes] | None:
        row = await self.db.fetchrow(
            "UPDATE app.sessions s SET last_seen_at=now() FROM app.users u WHERE s.token_hash=$1 AND s.expires_at > now() "
            "AND u.id=s.user_id AND u.disabled_at IS NULL RETURNING s.id, s.csrf_hash, s.elevated_until, u.id AS uid, "
            "u.username, u.must_change_password",
            _h(token),
        )
        if row is None:
            return None
        elevated = row["elevated_until"] is not None and row["elevated_until"] > datetime.now(UTC)
        return Principal(
            row["uid"], row["username"], frozenset(SCOPES), "session", row["id"], elevated, row["must_change_password"]
        ), bytes(row["csrf_hash"])

    @staticmethod
    def csrf_ok(csrf_hash: bytes, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(csrf_hash, _h(presented or ""))

    async def token_principal(self, token: str) -> Principal | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        row = await self.db.fetchrow(
            "UPDATE app.api_tokens t SET last_used_at=now() FROM app.users u WHERE t.token_hash=$1 AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > now()) AND u.id=t.user_id AND u.disabled_at IS NULL "
            "RETURNING u.id AS uid, u.username, t.scopes",
            _h(token),
        )
        if row is None:
            return None
        return Principal(row["uid"], row["username"], frozenset(row["scopes"]), "token")

    async def elevate(self, p: Principal, password: str) -> None:
        if p.session_id is None:
            raise Unauthorized("elevation needs a browser session")
        self.throttle.check(f"elevate:{p.user_id}")
        if await self._verify(p.username, password) is None:
            await self.audit.append("auth.elevate", actor_type="user", actor_id=str(p.user_id), outcome="failure")
            raise Unauthorized("wrong password")
        await self.db.execute(
            "UPDATE app.sessions SET elevated_until=$2 WHERE id=$1", p.session_id, datetime.now(UTC) + self.elevation
        )
        await self.audit.append("auth.elevate", actor_type="user", actor_id=str(p.user_id), outcome="success")

    async def change_password(self, p: Principal, current: str, new: str) -> None:
        if await self._verify(p.username, current) is None:
            raise Unauthorized("current password is wrong")
        validate_password(new)
        if current == new:
            raise ValidationFailed("choose a new password")
        async with self.db.transaction() as tx:
            await tx.execute(
                "UPDATE app.users SET password_hash=$2, must_change_password=false, password_changed_at=now() WHERE id=$1",
                p.user_id,
                _ph.hash(new),
            )
            await tx.execute("DELETE FROM app.sessions WHERE user_id=$1 AND id <> $2", p.user_id, p.session_id)
        await self.audit.append("auth.password_change", actor_type="user", actor_id=str(p.user_id), outcome="success")

    async def logout(self, token: str) -> None:
        await self.db.execute("DELETE FROM app.sessions WHERE token_hash=$1", _h(token))

    async def create_token(self, p: Principal, name: str, scopes: list[str], days: int | None) -> str:
        if not scopes or not set(scopes) <= set(SCOPES):
            raise ValidationFailed(f"scopes must be a subset of {SCOPES}")
        token = TOKEN_PREFIX + secrets.token_hex(32)
        expires = datetime.now(UTC) + timedelta(days=days) if days else None
        await self.db.execute(
            "INSERT INTO app.api_tokens (user_id, name, token_hash, scopes, expires_at) VALUES ($1,$2,$3,$4,$5)",
            p.user_id,
            name[:100] or "token",
            _h(token),
            sorted(set(scopes)),
            expires,
        )
        await self.audit.append(
            "auth.token_create",
            actor_type="user",
            actor_id=str(p.user_id),
            target=name[:100],
            outcome="success",
            detail={"scopes": scopes, "days": days},
        )
        return token

    async def list_tokens(self, user_id: UUID) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.db.fetch(
                "SELECT id, name, scopes, created_at, last_used_at, expires_at, revoked_at FROM app.api_tokens "
                "WHERE user_id=$1 ORDER BY created_at DESC",
                user_id,
            )
        ]

    async def revoke_token(self, p: Principal, token_id: UUID) -> None:
        await self.db.execute("UPDATE app.api_tokens SET revoked_at=now() WHERE id=$1 AND user_id=$2", token_id, p.user_id)
        await self.audit.append(
            "auth.token_revoke", actor_type="user", actor_id=str(p.user_id), target=str(token_id), outcome="success"
        )

    async def sweep(self) -> None:
        await self.db.execute("DELETE FROM app.sessions WHERE expires_at <= now() OR last_seen_at < now() - interval '12 hours'")
