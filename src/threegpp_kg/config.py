from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config_directory() -> Path:
    configured = os.getenv("THREEGPP_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = (
        Path.cwd() / "config",
        PROJECT_ROOT / "config",
        Path(__file__).resolve().parent / "config_files",
    )
    return next((path for path in candidates if (path / "defaults.yaml").is_file()), candidates[-1])


class AppConfig(BaseModel):
    name: str
    environment: str
    log_level: str


class DatabaseConfig(BaseModel):
    mode: Literal["fixture", "sql"] = "fixture"
    url: str
    preview_dataset_version: str | None = None
    echo: bool = False
    pool_size: int = Field(20, ge=1)
    max_overflow: int = Field(30, ge=0)


class ObjectStoreConfig(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local_path: Path = Path("./data/artifacts")
    endpoint: str | None = None
    bucket: str = "threegpp-artifacts"
    region: str = "us-east-1"
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_s3(self) -> ObjectStoreConfig:
        if self.backend == "s3" and not self.endpoint:
            raise ValueError("object_store.endpoint is required for the s3 backend")
        return self


class HttpConfig(BaseModel):
    user_agent: str
    timeout_seconds: float = Field(30, gt=0)
    retries: int = Field(4, ge=0)
    requests_per_second: float = Field(20.0, gt=0)
    max_concurrency: int = Field(20, ge=1)
    max_download_bytes: int = Field(100 * 1024 * 1024, ge=1024)
    max_archive_members: int = Field(200, ge=1)
    max_archive_uncompressed_bytes: int = Field(500 * 1024 * 1024, ge=1024)


class IngestionConfig(BaseModel):
    history_months: int = Field(24, ge=1)
    active_poll_minutes: int = Field(120, ge=5)
    recent_poll_minutes: int = Field(360, ge=5)
    historical_poll_minutes: int = Field(10080, ge=60)
    job_lease_seconds: int = Field(900, ge=30)
    max_attempts: int = Field(5, ge=1)


class ChunkingConfig(BaseModel):
    min_tokens: int = Field(300, ge=1)
    target_tokens: int = Field(500, ge=1)
    max_tokens: int = Field(700, ge=1)
    neighbor_blocks: int = Field(1, ge=0, le=10)

    @model_validator(mode="after")
    def ordered_sizes(self) -> ChunkingConfig:
        if not self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("chunk token limits must satisfy min <= target <= max")
        return self


class EvidenceBlockConfig(BaseModel):
    target_tokens: int = Field(1000, ge=1)
    max_tokens: int = Field(1400, ge=1)

    @model_validator(mode="after")
    def ordered_sizes(self) -> EvidenceBlockConfig:
        if self.target_tokens > self.max_tokens:
            raise ValueError("evidence block token limits must satisfy target <= max")
        return self


class ParserConfig(BaseModel):
    max_workbook_rows: int = Field(100_000, ge=1)
    max_document_blocks: int = Field(75_000, ge=1)
    max_archive_members: int = Field(200, ge=1)
    max_archive_uncompressed_bytes: int = Field(500 * 1024 * 1024, ge=1024)
    max_archive_depth: int = Field(1, ge=0, le=5)
    document_workers: int = Field(10, ge=1, le=32)
    reject_macros: bool = True
    legacy_converter: Literal["disabled", "auto", "libreoffice", "textutil"] = "disabled"
    legacy_conversion_timeout_seconds: int = Field(60, ge=1)
    max_converted_bytes: int = Field(100 * 1024 * 1024, ge=1024)


class RetrievalConfig(BaseModel):
    default_last_k_meetings: int = Field(3, ge=1)
    default_top_k: int = Field(10, ge=1)
    max_top_k: int = Field(100, ge=1)
    graph_hops: int = Field(2, ge=0, le=3)
    rrf_k: int = Field(60, ge=1)
    vector_weight: float = Field(1.0, ge=0)
    lexical_weight: float = Field(1.0, ge=0)


class ModelEndpointConfig(BaseModel):
    provider: Literal["openai_compatible", "onnx"] = "openai_compatible"
    base_url: str | None = None
    model: str | None = None
    dimensions: int | None = Field(None, ge=1)
    api_key: SecretStr | None = None
    batch_size: int = Field(32, ge=1)


class ModelsConfig(BaseModel):
    timeout_seconds: float = Field(60, gt=0)
    retries: int = Field(2, ge=0)
    embedding: ModelEndpointConfig
    rerank: ModelEndpointConfig
    generation: ModelEndpointConfig


class FeatureConfig(BaseModel):
    topic_extraction_enabled: bool = False
    newsletter_generation_enabled: bool = False
    topic_min_confidence: float = Field(0.65, ge=0, le=1)


class GraphConfig(BaseModel):
    default_node_limit: int = Field(300, ge=1)
    max_node_limit: int = Field(1000, ge=1)
    max_edges: int = Field(5000, ge=1)
    max_meeting_nodes: int = Field(15_000, ge=1)
    max_meeting_edges: int = Field(75_000, ge=1)
    document_block_page_size: int = Field(500, ge=1)
    max_document_block_page_size: int = Field(2000, ge=1)
    document_section_page_size: int = Field(200, ge=1)
    max_document_section_page_size: int = Field(1000, ge=1)


class SecurityConfig(BaseModel):
    allowed_source_hosts: list[str]
    oidc_issuer: str | None = None
    oidc_audience: str
    auth_required: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


class ObservabilityConfig(BaseModel):
    service_name: str = "threegpp-evidence-graph"
    json_logs: bool = True
    metrics_enabled: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppConfig
    database: DatabaseConfig
    object_store: ObjectStoreConfig
    http: HttpConfig
    ingestion: IngestionConfig
    evidence_blocks: EvidenceBlockConfig
    chunking: ChunkingConfig
    parsers: ParserConfig
    retrieval: RetrievalConfig
    models: ModelsConfig
    features: FeatureConfig
    graph: GraphConfig
    security: SecurityConfig
    observability: ObservabilityConfig

    @model_validator(mode="after")
    def validate_production_dependencies(self) -> Settings:
        if self.app.environment != "production":
            return self
        errors: list[str] = []
        if self.database.mode != "sql":
            errors.append("production database mode must be sql")
        if not self.database.url.startswith("postgresql+"):
            errors.append("production database must use PostgreSQL")
        if not self.security.auth_required or not self.security.oidc_issuer:
            errors.append("production requires OIDC authentication")
        if self.database.preview_dataset_version:
            errors.append("production cannot read a building preview dataset")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for section in ("embedding", "rerank", "generation"):
            if data["models"][section].get("api_key"):
                data["models"][section]["api_key"] = "***"
        if data["object_store"].get("access_key"):
            data["object_store"]["access_key"] = "***"
        if data["object_store"].get("secret_key"):
            data["object_store"]["secret_key"] = "***"
        return data


class WorkingGroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    tsg: str
    root_url: str
    meeting_pattern: str
    tdoc_prefix: str
    directories: dict[str, list[str]]
    artifact_patterns: dict[str, str]
    validation_meetings: list[str] = Field(default_factory=list)


ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "THREEGPP_DATABASE_MODE": ("database", "mode"),
    "THREEGPP_DATABASE_URL": ("database", "url"),
    "THREEGPP_DATABASE_PREVIEW_DATASET_VERSION": (
        "database",
        "preview_dataset_version",
    ),
    "THREEGPP_OBJECT_STORE_ENDPOINT": ("object_store", "endpoint"),
    "THREEGPP_OBJECT_STORE_BACKEND": ("object_store", "backend"),
    "THREEGPP_OBJECT_STORE_REGION": ("object_store", "region"),
    "THREEGPP_OBJECT_STORE_LOCAL_PATH": ("object_store", "local_path"),
    "THREEGPP_OBJECT_STORE_BUCKET": ("object_store", "bucket"),
    "THREEGPP_OBJECT_STORE_ACCESS_KEY": ("object_store", "access_key"),
    "THREEGPP_OBJECT_STORE_SECRET_KEY": ("object_store", "secret_key"),
    "THREEGPP_HTTP_REQUESTS_PER_SECOND": ("http", "requests_per_second"),
    "THREEGPP_HTTP_MAX_CONCURRENCY": ("http", "max_concurrency"),
    "THREEGPP_PARSER_DOCUMENT_WORKERS": ("parsers", "document_workers"),
    "THREEGPP_LEGACY_CONVERTER": ("parsers", "legacy_converter"),
    "THREEGPP_EMBEDDING_PROVIDER": ("models", "embedding", "provider"),
    "THREEGPP_EMBEDDING_BASE_URL": ("models", "embedding", "base_url"),
    "THREEGPP_EMBEDDING_MODEL": ("models", "embedding", "model"),
    "THREEGPP_EMBEDDING_DIMENSIONS": ("models", "embedding", "dimensions"),
    "THREEGPP_EMBEDDING_API_KEY": ("models", "embedding", "api_key"),
    "THREEGPP_RERANK_PROVIDER": ("models", "rerank", "provider"),
    "THREEGPP_RERANK_BASE_URL": ("models", "rerank", "base_url"),
    "THREEGPP_RERANK_MODEL": ("models", "rerank", "model"),
    "THREEGPP_RERANK_API_KEY": ("models", "rerank", "api_key"),
    "THREEGPP_GENERATION_PROVIDER": ("models", "generation", "provider"),
    "THREEGPP_GENERATION_BASE_URL": ("models", "generation", "base_url"),
    "THREEGPP_GENERATION_MODEL": ("models", "generation", "model"),
    "THREEGPP_GENERATION_API_KEY": ("models", "generation", "api_key"),
    "THREEGPP_TOPIC_EXTRACTION_ENABLED": ("features", "topic_extraction_enabled"),
    "THREEGPP_NEWSLETTER_GENERATION_ENABLED": (
        "features",
        "newsletter_generation_enabled",
    ),
    "THREEGPP_OIDC_ISSUER": ("security", "oidc_issuer"),
    "THREEGPP_OIDC_AUDIENCE": ("security", "oidc_audience"),
    "THREEGPP_AUTH_REQUIRED": ("security", "auth_required"),
}


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def _coerce(value: str) -> str | int | float | bool:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        pass
    return value


@lru_cache(maxsize=1)
def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or _config_directory() / "defaults.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for env_name, field_path in ENV_OVERRIDES.items():
        if value := os.getenv(env_name):
            _set_nested(raw, field_path, _coerce(value))
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def load_working_groups(config_dir: Path | None = None) -> dict[str, WorkingGroupConfig]:
    directory = config_dir or _config_directory() / "working_groups"
    groups: dict[str, WorkingGroupConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        group = WorkingGroupConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if group.id in groups:
            raise ValueError(f"duplicate working-group id {group.id}")
        groups[group.id] = group
    if not groups:
        raise ValueError(f"no working-group configurations found in {directory}")
    return groups


@lru_cache(maxsize=1)
def load_organization_aliases(path: Path | None = None) -> dict[str, str]:
    config_path = path or _config_directory() / "organization_aliases.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("organization alias configuration must be a mapping")
    aliases: dict[str, str] = {}
    for canonical, values in raw.items():
        if not isinstance(canonical, str) or not isinstance(values, list):
            raise ValueError("organization aliases must map a canonical name to a list")
        aliases[canonical.casefold()] = canonical
        for value in values:
            if not isinstance(value, str):
                raise ValueError("organization aliases must be strings")
            existing = aliases.get(value.casefold())
            if existing and existing != canonical:
                raise ValueError(f"organization alias {value!r} maps to multiple names")
            aliases[value.casefold()] = canonical
    return aliases
