"""Composition root: builds every service once (constructor injection), wires routes, runs background loops."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from aiplatform import __version__
from aiplatform.admin import views as admin_views
from aiplatform.api import openai_compat, routes, webui
from aiplatform.api.deps import Reader, container
from aiplatform.api.middleware import GuardMiddleware
from aiplatform.audit.service import AuditLog
from aiplatform.config import Environment, Settings, load_settings
from aiplatform.conversation.repository import ConversationRepository
from aiplatform.db import Database
from aiplatform.memory.context_builder import ContextBuilder
from aiplatform.memory.extraction import BackgroundModelJobs, ModelGate
from aiplatform.memory.lifecycle import MemoryPipeline
from aiplatform.memory.repository import LongTermMemoryRepository, ShortTermMemoryRepository
from aiplatform.memory.retrieval import MemoryRetriever
from aiplatform.memory.tools import memory_tools
from aiplatform.model.embedding import LlamaCppEmbeddingProvider
from aiplatform.model.runtime import ModelRuntime
from aiplatform.model.types import ProviderError
from aiplatform.observability.dashboard import DashboardService
from aiplatform.observability.logging import configure as configure_logging
from aiplatform.observability.metrics import Metrics
from aiplatform.orchestrator.chat_service import ChatService
from aiplatform.permissions.engine import PermissionEngine
from aiplatform.permissions.service import PermissionService
from aiplatform.security.auth import AuthService, Principal
from aiplatform.shared.errors import DomainError
from aiplatform.shared.events import EventBus, MemoryChanged, TurnCompleted
from aiplatform.shared.text import TokenEstimator
from aiplatform.tools.approvals import ApprovalBroker
from aiplatform.tools.executor import ToolExecutor
from aiplatform.tools.filesystem.tools import FsContext, filesystem_tools, purge_trash
from aiplatform.tools.registry import ToolRegistry
from aiplatform.tools.shell.tools import RunnerClient, ShellContext, ShellExecute
from aiplatform.tools.web.client import GuardedClient
from aiplatform.tools.web.ssrf import InternalNetworks
from aiplatform.tools.web.tools import WebContext, web_tools

log = structlog.get_logger("app")


class AppContainer:
    """Holds the wired object graph for one app instance (tests build their own with fakes)."""

    def __init__(self, settings: Settings, env: Environment, db: Database) -> None:
        self.settings, self.env, self.db = settings, env, db
        s = settings
        self.metrics = Metrics()
        self.events = EventBus()
        self.audit = AuditLog(db)
        self.auth = AuthService(
            db,
            self.audit,
            session_hours=s.server.session_hours,
            elevation_minutes=s.server.elevation_minutes,
            login_per_minute=s.rate_limits.login_per_minute,
        )
        self.permissions = PermissionService(db, self.audit, env.permissions_seed, env.runtime_dir)
        self.permissions_mode_cache = "normal"
        self.platform_host_dir: str | None = env.platform_host_dir
        self.tokens = TokenEstimator()
        self.embedder = LlamaCppEmbeddingProvider(
            s.embedding.base_url,
            s.embedding.model,
            s.embedding.dims,
            query_instruction=s.embedding.query_instruction,
            cache_size=s.embedding.cache_size,
        )
        self.runtime = ModelRuntime(db, self.audit, s.provider, env)
        self.conversations = ConversationRepository(db)
        self.ltm = LongTermMemoryRepository(db, s.embedding.model)
        self.stm = ShortTermMemoryRepository(db, s.memory.short_term_ttl_minutes)
        self.pipeline = MemoryPipeline(db, self.ltm, self.embedder, s.memory, self.audit, self.metrics, self.events)
        self.retriever = MemoryRetriever(self.ltm, self.embedder, s.memory.retrieval)
        self.builder = ContextBuilder(s.context, self.tokens)
        self.gate = ModelGate()
        self.runner = RunnerClient(env.runner_socket)
        self.runner_broad = RunnerClient(env.runner_broad_socket)
        self.web_client = GuardedClient(s.web, InternalNetworks())
        self.registry = ToolRegistry(
            [
                *filesystem_tools(FsContext(s.filesystem, env.workspace_host_dir)),
                ShellExecute(ShellContext(s.shell, self.runner, env.workspace_host_dir, self.runner_broad)),
                *web_tools(WebContext(s.web, self.web_client)),
                *memory_tools(self.retriever, self.pipeline),
            ]
        )
        self.approvals = ApprovalBroker(db, self.audit, self.metrics)
        self.executor = ToolExecutor(db, self.registry, PermissionEngine(), self.approvals, self.audit, self.metrics, s.tools)
        self.chat = ChatService(
            db=db,
            settings=s,
            conversations=self.conversations,
            retriever=self.retriever,
            pipeline=self.pipeline,
            stm=self.stm,
            builder=self.builder,
            runtime=self.runtime,
            registry=self.registry,
            executor=self.executor,
            permissions=self.permissions,
            events=self.events,
            metrics=self.metrics,
            gate=self.gate,
            tokens=self.tokens,
        )
        self.jobs = BackgroundModelJobs(
            self.runtime.provider, self.runtime.params, db, self.pipeline, self.conversations, s, self.gate
        )
        self.dashboard = DashboardService(db)
        self.ui = webui.UiProxy(s.embedding.base_url)
        self.chat_limiter = openai_compat.ChatLimiter(s.rate_limits.chat_per_minute)
        self.events.subscribe(TurnCompleted, self.jobs.on_turn_completed)
        self.events.subscribe(MemoryChanged, self._on_memory_changed)
        self._loops: list[asyncio.Task[None]] = []

    async def _on_memory_changed(self, ev: MemoryChanged) -> None:
        self.retriever.invalidate(ev.user_id)

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(12)

    async def start(self) -> None:
        await self.registry.sync_definitions(self.db)
        if await self.permissions.seed_if_empty():
            log.info("permissions_seeded", seed=str(self.env.permissions_seed))
        try:
            if await self.auth.bootstrap_admin(self.env.secret("admin_bootstrap")):
                log.info("admin_bootstrapped", username="admin")
        except FileNotFoundError:
            log.warning("no_admin_bootstrap_secret")
        await self.runtime.load()
        await self.approvals.expire_stale()
        self.jobs.start()
        self._loops.append(asyncio.create_task(self._sweeper(), name="sweeper"))

    async def _sweeper(self) -> None:
        """TTL cleanup: short-term memory, expired long-term memories, sessions, trash; embeds memories missing a vector."""
        while True:
            await asyncio.sleep(self.settings.memory.sweep_interval_s)
            try:
                n_stm = await self.stm.sweep()
                n_exp = await self.ltm.expire_due()
                await self.ltm.purge_deleted()
                await self.auth.sweep()
                await self.approvals.expire_stale()
                for mid, content in await self.ltm.missing_embeddings():
                    try:
                        vec = (await self.embedder.embed([content]))[0]
                        await self.ltm.replace_embedding(mid, vec)
                    except ProviderError:
                        break
                snap = await self.permissions.snapshot()
                await asyncio.to_thread(
                    purge_trash, [r.container_path for r in snap.roots], self.settings.filesystem.trash_retention_days
                )
                for status, n in await self.db.fetch("SELECT status, count(*) AS n FROM app.long_term_memories GROUP BY status"):
                    self.metrics.memories.labels(status).set(n)
                if n_stm or n_exp:
                    log.info("sweep", short_term_removed=n_stm, long_term_expired=n_exp)
            except Exception:
                log.exception("sweep_failed")

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await self.jobs.stop()
        await self.events.drain()
        for closer in (self.runtime.aclose(), self.embedder.aclose(), self.web_client.aclose(), self.ui.aclose()):
            with contextlib.suppress(Exception):
                await closer
        await self.db.close()


def create_app(settings: Settings | None = None, env: Environment | None = None, container_factory: Any = None) -> FastAPI:
    settings = settings or load_settings()
    env = env or Environment.from_env()
    configure_logging(settings.logging.level)
    metrics_holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container_factory is not None:
            c = await container_factory(settings, env)
        else:
            db = await Database.connect(
                host=env.db_host,
                port=env.db_port,
                database=env.db_name,
                user=env.db_user,
                password=env.secret("pg_app"),
                ef_search=settings.memory.retrieval.ef_search,
            )
            c = AppContainer(settings, env, db)
        app.state.container = c
        metrics_holder["m"] = c.metrics
        await c.start()
        log.info("started", version=__version__, provider=c.runtime.params().provider, model=c.runtime.params().model)
        try:
            yield
        finally:
            await c.stop()

    app = FastAPI(
        title="Local AI Platform",
        version=__version__,
        lifespan=lifespan,
        description="Memory, permission-controlled tools and observability around a local Qwen3.6 model. "
        "All endpoints need a session (admin console login) or a Bearer API token.",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message, **({"detail": exc.detail} if exc.detail else {})}},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg")} for e in exc.errors()[:10]]
        return JSONResponse({"error": {"code": "invalid", "message": "request validation failed", "detail": errs}}, 422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse({"error": {"code": "internal", "message": "internal error (see server logs)"}}, 500)

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> Response:
        c = container(request)
        checks: dict[str, Any] = {}
        try:
            await c.db.fetchval("SELECT 1")
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"fail: {type(e).__name__}"
        checks["embedding"] = "ok" if await c.embedder.health() else "down"
        checks["shell_sandbox"] = "ok" if await c.runner.health() else "down"
        h = await c.runtime.provider().health()
        checks["model"] = "ok" if h.ok else h.detail
        ok = checks["database"] == "ok"
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics", tags=["health"])
    async def metrics(request: Request, p: Principal = Reader) -> Response:
        return PlainTextResponse(generate_latest(container(request).metrics.registry).decode(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(openai_compat.router)
    app.include_router(routes.router)
    app.include_router(admin_views.router)
    app.mount("/admin/static", admin_views.static, name="admin-static")
    app.include_router(webui.router)  # last: catch-all for UI assets

    class _LazyMetrics:
        """The middleware is built before lifespan; resolve the container's registry lazily."""

        def __getattr__(self, name: str) -> Any:
            m = metrics_holder.get("m")
            if m is None:
                metrics_holder["m"] = m = Metrics()
            return getattr(m, name)

    app.add_middleware(
        GuardMiddleware,
        allowed_hosts=settings.server.allowed_hosts,
        allowed_origins=settings.server.allowed_origins,
        metrics=cast(Metrics, _LazyMetrics()),
    )
    return app
