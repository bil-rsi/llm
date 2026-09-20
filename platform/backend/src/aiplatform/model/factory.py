"""Provider factory: picks the active LLMProvider from settings (runtime override from the admin console wins)."""

from __future__ import annotations

from aiplatform.config import Environment, ProviderSettings
from aiplatform.model.ollama import OllamaProvider
from aiplatform.model.openai_compat import OpenAICompatProvider
from aiplatform.model.types import LLMProvider


def create_provider(s: ProviderSettings, env: Environment, active: str | None = None) -> LLMProvider:
    which = active or s.active
    read_timeout = max(s.first_token_timeout_s, s.idle_timeout_s)
    if which == "ollama":
        return OllamaProvider(
            s.ollama.base_url, s.ollama.model, connect_timeout=s.connect_timeout_s, read_timeout=read_timeout, retries=s.retries
        )
    if which == "openai_compat":
        key = env.secret(s.openai_compat.api_key_secret) if s.openai_compat.api_key_secret else None
        return OpenAICompatProvider(
            s.openai_compat.base_url,
            s.openai_compat.model,
            api_key=key,
            connect_timeout=s.connect_timeout_s,
            read_timeout=read_timeout,
            retries=s.retries,
        )
    raise ValueError(f"unknown provider {which}")
