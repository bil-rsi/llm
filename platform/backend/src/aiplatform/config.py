"""Application settings: config/app.yaml (read-only mount) + environment + Docker secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Strict):
    allowed_hosts: list[str] = ["127.0.0.1:8090", "localhost:8090"]
    allowed_origins: list[str] = ["http://127.0.0.1:8090", "http://localhost:8090"]
    session_hours: int = 12
    elevation_minutes: int = 10


class OllamaSettings(_Strict):
    base_url: str = "http://host.docker.internal:11434"
    model: str = "qwen3.6-35b-a3b"


class OpenAICompatSettings(_Strict):
    base_url: str = "http://host.docker.internal:8080/v1"
    model: str = "qwen3.6-35b-a3b"
    api_key_secret: str | None = None


class ProviderSettings(_Strict):
    active: Literal["ollama", "openai_compat"] = "ollama"
    ollama: OllamaSettings = OllamaSettings()
    openai_compat: OpenAICompatSettings = OpenAICompatSettings()
    num_ctx: int = Field(16384, ge=2048, le=262144)
    temperature: float = Field(0.7, ge=0, le=2)
    top_p: float = Field(0.8, gt=0, le=1)
    max_output_tokens: int = Field(4096, ge=64, le=32768)
    think: bool = False
    connect_timeout_s: float = 3
    first_token_timeout_s: float = 180
    idle_timeout_s: float = 90
    retries: int = Field(2, ge=0, le=5)


class EmbeddingSettings(_Strict):
    base_url: str = "http://embed:8081"
    model: str = "qwen3-embedding-0.6b"
    dims: int = 1024
    query_instruction: str = ""
    cache_size: int = 2048


class ContextSettings(_Strict):
    reserve_output_tokens: int = 2048
    memory_share: float = Field(0.15, ge=0, le=0.5)
    short_term_share: float = Field(0.10, ge=0, le=0.5)
    min_recent_turns: int = 2
    summarise_after_messages: int = 24


class RetrievalSettings(_Strict):
    top_k: int = 8
    max_injected: int = 6
    min_score: float = 0.012
    min_cosine: float = 0.35
    rrf_k: int = 60
    recency_half_life_days: float = 90
    ef_search: int = 40


class MemorySettings(_Strict):
    retrieval: RetrievalSettings = RetrievalSettings()
    dedupe_cosine: float = 0.92
    extraction: Literal["auto", "explicit_only", "off"] = "auto"
    auto_activate_confidence: float = 0.7
    allow_sensitive: bool = False
    store_personal: bool = False
    default_ttl_days: dict[str, int] = {"context": 30}
    short_term_ttl_minutes: int = 720
    sweep_interval_s: int = 60


class ToolSettings(_Strict):
    max_rounds: int = 8
    max_calls_per_turn: int = 16
    result_max_chars: int = 12000
    approval_timeout_s: int = 180


class FilesystemSettings(_Strict):
    max_read_bytes: int = 1_048_576
    max_write_bytes: int = 2_097_152
    trash_retention_days: int = 30
    versions_keep: int = 10
    write_denied_extensions: list[str] = [
        ".exe",
        ".dll",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".psd1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msi",
        ".msp",
        ".scr",
        ".lnk",
        ".reg",
        ".com",
        ".sys",
        ".cpl",
        ".hta",
        ".pif",
        ".jar",
    ]


class ShellSettings(_Strict):
    default_timeout_s: int = 10
    max_timeout_s: int = 60
    max_output_bytes: int = 65536


class SearchSettings(_Strict):
    provider: Literal["duckduckgo", "searxng", "none"] = "duckduckgo"
    searxng_url: str = "http://searxng:8080"


class WebSettings(_Strict):
    connect_timeout_s: float = 5
    total_timeout_s: float = 20
    max_bytes: int = 2_097_152
    max_redirects: int = 3
    allowed_ports: list[int] = [80, 443]
    rate_per_minute: int = 30
    rate_per_domain_per_minute: int = 10
    content_types: list[str] = ["text/html", "text/plain", "application/json"]
    search: SearchSettings = SearchSettings()


class RateLimitSettings(_Strict):
    login_per_minute: int = 5
    chat_per_minute: int = 30


class LoggingSettings(_Strict):
    level: str = "INFO"


class Settings(_Strict):
    server: ServerSettings = ServerSettings()
    provider: ProviderSettings = ProviderSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    context: ContextSettings = ContextSettings()
    memory: MemorySettings = MemorySettings()
    tools: ToolSettings = ToolSettings()
    filesystem: FilesystemSettings = FilesystemSettings()
    shell: ShellSettings = ShellSettings()
    web: WebSettings = WebSettings()
    rate_limits: RateLimitSettings = RateLimitSettings()
    logging: LoggingSettings = LoggingSettings()


class Environment(_Strict):
    """Deployment wiring that differs between the container and tests (not user-facing configuration)."""

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "aiplatform"
    db_user: str = "aimem_app"
    secrets_dir: Path = Path("/run/secrets")
    permissions_seed: Path = Path("/opt/config/permissions.yaml")
    runtime_dir: Path = Path("/opt/runtime")
    runner_socket: Path = Path("/run/aip/runner.sock")
    runner_broad_socket: Path = Path("/run/aip/runner-broad.sock")
    workspace_host_dir: str = "C:/AIWorkspace"
    migrations_dir: Path = Path("/opt/migrations")
    platform_host_dir: str | None = None

    @classmethod
    def from_env(cls) -> Environment:
        e = os.environ
        return cls(
            db_host=e.get("AIP_DB_HOST", "postgres"),
            db_port=int(e.get("AIP_DB_PORT", "5432")),
            db_name=e.get("AIP_DB_NAME", "aiplatform"),
            db_user=e.get("AIP_DB_USER", "aimem_app"),
            secrets_dir=Path(e.get("AIP_SECRETS_DIR", "/run/secrets")),
            permissions_seed=Path(e.get("AIP_PERMISSIONS_SEED", "/opt/config/permissions.yaml")),
            runtime_dir=Path(e.get("AIP_RUNTIME_DIR", "/opt/runtime")),
            runner_socket=Path(e.get("AIP_RUNNER_SOCKET", "/run/aip/runner.sock")),
            runner_broad_socket=Path(e.get("AIP_RUNNER_BROAD_SOCKET", "/run/aip/runner-broad.sock")),
            workspace_host_dir=e.get("AIP_WORKSPACE_HOST_DIR", "C:/AIWorkspace"),
            migrations_dir=Path(e.get("AIP_MIGRATIONS_DIR", "/opt/migrations")),
            platform_host_dir=e.get("AIP_PLATFORM_HOST_DIR") or None,
        )

    def secret(self, name: str) -> str:
        """Read a Docker secret. Names are fixed identifiers from code/config, never user or model input."""
        if not name.replace("_", "").isalnum():
            raise ValueError(f"invalid secret name: {name!r}")
        return (self.secrets_dir / name).read_text(encoding="utf-8").strip()


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    p = Path(path or os.environ.get("AIP_CONFIG_FILE", "/opt/config/app.yaml"))
    if not p.exists():
        return Settings()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
