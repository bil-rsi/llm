"""Per-request stage timings (api, db, retrieval, embed, ...) collected through a context variable.

Stages accumulate: two DB queries in one request add up under "db". Nested stages are allowed (e.g. "db" inside
"memory_retrieval"), so stages are a breakdown, not a partition of the total.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

_stages: ContextVar[dict[str, float] | None] = ContextVar("aip_stages", default=None)


def begin() -> dict[str, float]:
    d: dict[str, float] = {}
    _stages.set(d)
    return d


def current() -> dict[str, float]:
    return dict(_stages.get() or {})


def add(name: str, ms: float) -> None:
    d = _stages.get()
    if d is not None:
        d[name] = d.get(name, 0.0) + ms
    RECORDER.record(name, ms)


@contextmanager
def stage(name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        add(name, (time.perf_counter() - t0) * 1000.0)


@dataclass
class _Window:
    values: deque[float] = field(default_factory=lambda: deque(maxlen=2000))


class StageRecorder:
    """Rolling window of recent stage latencies for the dashboard percentiles (process-local, bounded)."""

    def __init__(self) -> None:
        self._w: dict[str, _Window] = {}
        self._lock = Lock()

    def record(self, name: str, ms: float) -> None:
        with self._lock:
            self._w.setdefault(name, _Window()).values.append(ms)

    def summary(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            items = {k: sorted(v.values) for k, v in self._w.items()}
        for k, vals in items.items():
            if not vals:
                continue
            n = len(vals)
            out[k] = {
                "n": n,
                "p50": vals[n // 2],
                "p95": vals[min(n - 1, int(n * 0.95))],
                "p99": vals[min(n - 1, int(n * 0.99))],
                "max": vals[-1],
            }
        return out


RECORDER = StageRecorder()
