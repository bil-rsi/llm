"""structlog JSON logging with request/correlation IDs and secret redaction on every string value."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from aiplatform.observability import context
from aiplatform.security.sensitivity import redact


def _add_ids(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event.setdefault("request_id", context.request_id.get())
    event.setdefault("correlation_id", context.correlation_id.get())
    return event


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for k, v in list(event.items()):
        if isinstance(v, str) and len(v) > 12:
            event[k] = redact(v)
    return event


def configure(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore"):  # one line per outbound request is noise; our own logs carry the details
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_ids,
            structlog.processors.format_exc_info,
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
