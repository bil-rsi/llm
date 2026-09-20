"""Request and correlation IDs carried through the async call chain (logs, audit rows, model requests)."""

from __future__ import annotations

import secrets
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("aip_request_id", default="-")
correlation_id: ContextVar[str] = ContextVar("aip_correlation_id", default="-")


def new_id() -> str:
    return secrets.token_hex(8)


def valid_external_id(value: str | None) -> str | None:
    """Accept a client-supplied correlation id only if it is short and plain (it ends up in logs and the DB)."""
    if value and 1 <= len(value) <= 64 and all(c.isalnum() or c in "-_." for c in value):
        return value
    return None
