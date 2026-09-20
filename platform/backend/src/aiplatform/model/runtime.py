"""Runtime model settings (admin console) layered over app.yaml, and a holder for the active provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aiplatform.audit.service import AuditLog
from aiplatform.config import Environment, ProviderSettings
from aiplatform.db import Database
from aiplatform.model.factory import create_provider
from aiplatform.model.types import LLMProvider
from aiplatform.shared.errors import ValidationFailed

_CHOICES: dict[str, tuple[str, ...]] = {"model.provider": ("ollama", "openai_compat")}
_RANGES: dict[str, tuple[type[int] | type[float], float, float]] = {
    "model.temperature": (float, 0.0, 2.0),
    "model.top_p": (float, 0.05, 1.0),
    "model.num_ctx": (int, 2048, 131072),
    "model.max_output_tokens": (int, 64, 32768),
}
_FLAGS = {"model.think"}


@dataclass(frozen=True)
class ModelParams:
    provider: str
    model: str
    temperature: float
    top_p: float
    num_ctx: int
    max_output_tokens: int
    think: bool


class ModelRuntime:
    def __init__(self, db: Database, audit: AuditLog, base: ProviderSettings, env: Environment) -> None:
        self.db = db
        self.audit = audit
        self.base = base
        self.env = env
        self._overrides: dict[str, Any] = {}
        self._provider: LLMProvider | None = None
        self._provider_name = ""

    async def load(self) -> None:
        self._overrides = {
            r["key"]: r["value"]
            for r in await self.db.fetch("SELECT key, value FROM app.runtime_settings WHERE key LIKE 'model.%'")
        }

    def params(self) -> ModelParams:
        o = self._overrides
        provider = str(o.get("model.provider", self.base.active))
        model = self.base.ollama.model if provider == "ollama" else self.base.openai_compat.model
        return ModelParams(
            provider,
            model,
            float(o.get("model.temperature", self.base.temperature)),
            float(o.get("model.top_p", self.base.top_p)),
            int(o.get("model.num_ctx", self.base.num_ctx)),
            int(o.get("model.max_output_tokens", self.base.max_output_tokens)),
            bool(o.get("model.think", self.base.think)),
        )

    def provider(self) -> LLMProvider:
        name = self.params().provider
        if self._provider is None or self._provider_name != name:
            self._provider = create_provider(self.base, self.env, name)
            self._provider_name = name
        return self._provider

    async def set(self, key: str, value: Any, by_user: UUID) -> None:
        v: Any
        if key in _CHOICES:
            v = str(value)
            if v not in _CHOICES[key]:
                raise ValidationFailed(f"{key}: one of {_CHOICES[key]}")
        elif key in _RANGES:
            typ, lo, hi = _RANGES[key]
            try:
                v = typ(value)
            except (TypeError, ValueError) as e:
                raise ValidationFailed(f"{key}: expected a number") from e
            if not lo <= v <= hi:
                raise ValidationFailed(f"{key}: between {lo} and {hi}")
        elif key in _FLAGS:
            v = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
        else:
            raise ValidationFailed(f"unknown model setting {key}")
        await self.db.execute(
            "INSERT INTO app.runtime_settings (key, value, updated_by) VALUES ($1,$2,$3) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by=EXCLUDED.updated_by",
            key,
            v,
            by_user,
        )
        await self.audit.append(
            "model.setting",
            actor_type="user",
            actor_id=str(by_user),
            target=key,
            outcome="success",
            detail={"old": self._overrides.get(key), "new": v},
        )
        self._overrides[key] = v

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()
