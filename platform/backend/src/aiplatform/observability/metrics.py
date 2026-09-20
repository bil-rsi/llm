"""Prometheus metrics (served at /metrics). One registry per app instance so tests stay isolated."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_MS_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.http_requests = Counter("aip_http_requests_total", "HTTP requests", ["route", "method", "status"], registry=r)
        self.http_latency = Histogram(
            "aip_http_request_ms", "HTTP request latency (ms, excl. streaming body)", ["route"], buckets=_MS_BUCKETS, registry=r
        )
        self.stage_ms = Histogram(
            "aip_stage_ms", "Per-stage latency within a request (ms)", ["stage"], buckets=_MS_BUCKETS, registry=r
        )
        self.model_tokens = Counter("aip_model_tokens_total", "Model tokens", ["kind"], registry=r)
        self.model_ttft = Histogram("aip_model_ttft_ms", "Time to first token (ms)", buckets=_MS_BUCKETS, registry=r)
        self.model_tps = Histogram(
            "aip_model_tokens_per_second", "Generation speed", buckets=(1, 2, 4, 6, 8, 10, 15, 20, 30, 50), registry=r
        )
        self.tool_calls = Counter("aip_tool_calls_total", "Tool calls", ["tool", "status"], registry=r)
        self.blocked = Counter("aip_blocked_total", "Blocked operations", ["tool", "reason"], registry=r)
        self.memory_ops = Counter("aip_memory_ops_total", "Memory lifecycle outcomes", ["outcome"], registry=r)
        self.memories = Gauge("aip_memories", "Long-term memories by status", ["status"], registry=r)
        self.approvals_pending = Gauge("aip_approvals_pending", "Pending approvals", registry=r)
