

from contextvars import ContextVar, Token
import base64
from functools import lru_cache
from enum import Enum
from ipaddress import ip_network
import json
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _env_aware_config() -> SettingsConfigDict:

    return SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


class RuntimeEnvironment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"

class LLMSettings(BaseSettings):

    model_config = _env_aware_config()

    url: str = Field(
        default="http://localhost:8888",
        validation_alias=AliasChoices("LLM_URL", "llm_url", "url"),
        description="Base URL of OpenAI-compatible LLM endpoint",
    )
    model_id: str = Field(
        default="default",
        validation_alias=AliasChoices("LLM_MODEL_ID", "llm_model_id", "model_id"),
        description="Default backend LLM model identifier",
    )
    fallback_model_ids: Union[List[str], str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "LLM_FALLBACK_MODEL_IDS",
            "llm_fallback_model_ids",
            "fallback_model_ids",
        ),
        description="Ordered fallback LLM model identifiers for transient default-model failures",
    )
    api_key: str = Field(
        default="EMPTY",
        repr=False,
        validation_alias=AliasChoices("LLM_API_KEY", "llm_api_key", "api_key"),
        description="API key for backend LLM endpoint",
    )
    context_window: int = Field(
        default=8192,
        gt=0,
        validation_alias=AliasChoices("CONTEXT_WINDOW", "LLM_CONTEXT_WINDOW", "context_window"),
        description="Maximum context window size in tokens",
    )
    timeout_seconds: float = Field(
        default=120.0,
        gt=0.0,
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "llm_timeout_seconds", "timeout_seconds"),
        description="HTTP timeout for LLM inference in seconds",
    )
    retry_attempts: int = Field(
        default=3, ge=1, le=5, validation_alias=AliasChoices("LLM_RETRY_ATTEMPTS", "retry_attempts")
    )
    retry_backoff_seconds: float = Field(
        default=0.2, ge=0.0, le=10.0, validation_alias=AliasChoices("LLM_RETRY_BACKOFF_SECONDS", "retry_backoff_seconds")
    )
    max_concurrency: int = Field(
        default=20, ge=1, le=500, validation_alias=AliasChoices("LLM_MAX_CONCURRENCY", "max_concurrency")
    )
    bulkhead_timeout_seconds: float = Field(
        default=1.0, gt=0.0, le=30.0, validation_alias=AliasChoices("LLM_BULKHEAD_TIMEOUT_SECONDS", "bulkhead_timeout_seconds")
    )
    circuit_failure_threshold: int = Field(
        default=5, ge=1, le=100, validation_alias=AliasChoices("LLM_CIRCUIT_FAILURE_THRESHOLD", "circuit_failure_threshold")
    )
    circuit_recovery_seconds: float = Field(
        default=30.0, gt=0.0, le=600.0, validation_alias=AliasChoices("LLM_CIRCUIT_RECOVERY_SECONDS", "circuit_recovery_seconds")
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        validation_alias=AliasChoices("LLM_TEMPERATURE", "llm_temperature", "temperature"),
        description="Default sampling temperature",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("LLM_MAX_TOKENS", "llm_max_tokens", "max_tokens"),
        description="Default max completion tokens",
    )
    extra_headers: Union[Dict[str, str], str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("LLM_EXTRA_HEADERS", "llm_extra_headers", "extra_headers"),
        description="Additional HTTP headers sent to the LLM provider",
    )

    @field_validator("extra_headers", mode="before")
    @classmethod
    def parse_extra_headers(cls, value: Any) -> Dict[str, str]:
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            value = parsed
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        return {}

    @field_validator("fallback_model_ids", mode="before")
    @classmethod
    def parse_fallback_model_ids(cls, value: Any) -> List[str]:
        if isinstance(value, str):
            if value.startswith("[") and value.endswith("]"):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            if isinstance(value, str):
                value = value.split(",")
        if isinstance(value, (list, tuple, set)):
            unique: List[str] = []
            for item in value:
                candidate = str(item).strip()
                if candidate and candidate not in unique:
                    unique.append(candidate)
            return unique
        return []

class EmbeddingSettings(BaseSettings):

    model_config = _env_aware_config()

    url: str = Field(
        default="http://localhost:7997",
        validation_alias=AliasChoices("EMBEDDING_URL", "embedding_url", "url"),
        description="Base URL of OpenAI-compatible embedding endpoint",
    )
    model_id: str = Field(
        default="default",
        validation_alias=AliasChoices("EMBEDDING_MODEL_ID", "embedding_model_id", "model_id"),
        description="Embedding model identifier",
    )
    api_key: str = Field(
        default="EMPTY",
        repr=False,
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "embedding_api_key", "api_key"),
        description="API key for embedding endpoint",
    )
    dimension: int = Field(
        default=1024,
        gt=0,
        validation_alias=AliasChoices("EMBEDDING_DIMENSION", "embedding_dimension", "dimension"),
        description="Vector dimension size for pgvector columns",
    )
    batch_size: int = Field(
        default=32,
        gt=0,
        validation_alias=AliasChoices("EMBEDDING_BATCH_SIZE", "embedding_batch_size", "batch_size"),
        description="Maximum batch size for vector generation",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        validation_alias=AliasChoices("EMBEDDING_TIMEOUT_SECONDS", "embedding_timeout_seconds", "timeout_seconds"),
        description="HTTP timeout for embedding generation in seconds",
    )
    retry_attempts: int = Field(
        default=3, ge=1, le=5, validation_alias=AliasChoices("EMBEDDING_RETRY_ATTEMPTS", "retry_attempts")
    )
    retry_backoff_seconds: float = Field(
        default=0.2, ge=0.0, le=10.0, validation_alias=AliasChoices("EMBEDDING_RETRY_BACKOFF_SECONDS", "retry_backoff_seconds")
    )
    max_concurrency: int = Field(
        default=20, ge=1, le=500, validation_alias=AliasChoices("EMBEDDING_MAX_CONCURRENCY", "max_concurrency")
    )
    bulkhead_timeout_seconds: float = Field(
        default=1.0, gt=0.0, le=30.0, validation_alias=AliasChoices("EMBEDDING_BULKHEAD_TIMEOUT_SECONDS", "bulkhead_timeout_seconds")
    )
    circuit_failure_threshold: int = Field(
        default=5, ge=1, le=100, validation_alias=AliasChoices("EMBEDDING_CIRCUIT_FAILURE_THRESHOLD", "circuit_failure_threshold")
    )
    circuit_recovery_seconds: float = Field(
        default=30.0, gt=0.0, le=600.0, validation_alias=AliasChoices("EMBEDDING_CIRCUIT_RECOVERY_SECONDS", "circuit_recovery_seconds")
    )

class DatabaseSettings(BaseSettings):

    model_config = _env_aware_config()

    url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_db",
        repr=False,
        validation_alias=AliasChoices("DATABASE_URL", "DB_URL", "database_url", "db_url", "url"),
        description="SQLAlchemy async database connection URL",
    )
    migration_url: Optional[str] = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices(
            "MIGRATION_DATABASE_URL",
            "migration_database_url",
            "migration_url",
        ),
        description="Privileged database URL used only by explicit migration commands",
    )
    worker_url: Optional[str] = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices(
            "WORKER_DATABASE_URL",
            "worker_database_url",
            "worker_url",
        ),
        description="Dedicated database URL used only by background workers",
    )
    pool_size: int = Field(
        default=20,
        gt=0,
        validation_alias=AliasChoices("DB_POOL_SIZE", "pool_size"),
        description="Connection pool size",
    )
    max_overflow: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("DB_MAX_OVERFLOW", "max_overflow"),
        description="Max overflow connections beyond pool size",
    )
    pool_timeout: float = Field(
        default=30.0,
        gt=0.0,
        validation_alias=AliasChoices("DB_POOL_TIMEOUT", "pool_timeout"),
        description="Connection pool acquisition timeout in seconds",
    )
    pool_recycle: int = Field(
        default=1800,
        gt=0,
        validation_alias=AliasChoices("DB_POOL_RECYCLE", "pool_recycle"),
        description="Recycle connections after N seconds",
    )
    echo: bool = Field(
        default=False,
        validation_alias=AliasChoices("DB_ECHO", "echo"),
        description="Echo SQL queries to log output",
    )

class GatewaySettings(BaseSettings):

    model_config = _env_aware_config()

    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("HOST", "host"),
        description="Gateway bind host",
    )
    port: int = Field(
        default=8000,
        gt=0,
        le=65535,
        validation_alias=AliasChoices("PORT", "port"),
        description="Gateway bind port",
    )
    log_level: str = Field(
        default="DEBUG",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
        description="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    log_format: str = Field(
        default="auto",
        pattern="^(auto|console|json)$",
        validation_alias=AliasChoices("LOG_FORMAT", "log_format"),
        description="Log output format (auto, console, or JSON)",
    )
    storage_dir: str = Field(
        default="./data/storage",
        validation_alias=AliasChoices("STORAGE_DIR", "storage_dir"),
        description="Local disk storage directory for uploaded files",
    )
    storage_backend: str = Field(
        default="local",
        pattern="^(local|s3)$",
        validation_alias=AliasChoices("STORAGE_BACKEND", "storage_backend"),
        description="Versioned object storage backend: local disk or S3-compatible endpoint",
    )
    storage_s3_endpoint: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("STORAGE_S3_ENDPOINT", "storage_s3_endpoint"),
        description="S3-compatible endpoint URL used when STORAGE_BACKEND is s3",
    )
    storage_s3_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("STORAGE_S3_BUCKET", "storage_s3_bucket"),
        description="S3 bucket holding versioned storage objects",
    )
    storage_s3_region: str = Field(
        default="",
        validation_alias=AliasChoices("STORAGE_S3_REGION", "storage_s3_region"),
        description="S3 signing region for the storage bucket",
    )
    storage_s3_access_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("STORAGE_S3_ACCESS_KEY", "storage_s3_access_key"),
        description="S3 access key id for the storage bucket",
    )
    storage_s3_secret_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("STORAGE_S3_SECRET_KEY", "storage_s3_secret_key"),
        description="S3 secret access key for the storage bucket",
    )
    storage_s3_session_token: Optional[str] = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("STORAGE_S3_SESSION_TOKEN", "storage_s3_session_token"),
        description="Optional S3 session token for temporary credentials",
    )
    storage_s3_prefix: str = Field(
        default="",
        validation_alias=AliasChoices("STORAGE_S3_PREFIX", "storage_s3_prefix"),
        description="Optional key prefix inside the S3 bucket",
    )
    oidc_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OIDC_ENABLED", "oidc_enabled"),
        description="Enable OIDC single sign-on for management authentication",
    )
    oidc_issuer: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OIDC_ISSUER", "oidc_issuer"),
        description="OIDC issuer URL used for discovery and token verification",
    )
    oidc_client_id: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("OIDC_CLIENT_ID", "oidc_client_id"),
        description="OIDC client identifier registered with the identity provider",
    )
    oidc_client_secret: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("OIDC_CLIENT_SECRET", "oidc_client_secret"),
        description="OIDC client secret used for the authorization code exchange",
    )
    oidc_redirect_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OIDC_REDIRECT_URL", "oidc_redirect_url"),
        description="Public callback URL registered with the identity provider",
    )
    oidc_group_claim: str = Field(
        default="groups",
        validation_alias=AliasChoices("OIDC_GROUP_CLAIM", "oidc_group_claim"),
        description="ID token claim carrying group memberships",
    )
    oidc_username_claim: str = Field(
        default="email",
        validation_alias=AliasChoices("OIDC_USERNAME_CLAIM", "oidc_username_claim"),
        description="ID token claim used as the member username",
    )
    oidc_role_mapping: Union[Dict[str, str], str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("OIDC_ROLE_MAPPING", "oidc_role_mapping"),
        description="JSON mapping of IdP group name to system role",
    )
    oidc_group_space_map: Union[Dict[str, List[Dict[str, str]]], str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("OIDC_GROUP_SPACE_MAP", "oidc_group_space_map"),
        description="JSON mapping of IdP group name to space membership grants",
    )
    oidc_deprovision_disable: bool = Field(
        default=True,
        validation_alias=AliasChoices("OIDC_DEPROVISION_DISABLE", "oidc_deprovision_disable"),
        description="Disable members whose OIDC-mapped grants are fully removed",
    )
    api_keys: Union[List[str], str] = Field(
        default_factory=list,
        repr=False,
        validation_alias=AliasChoices("GATEWAY_API_KEYS", "gateway_api_keys", "api_keys"),
        description="Allowed API keys for client authentication",
    )
    cors_origins: Union[List[str], str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"],
        validation_alias=AliasChoices("CORS_ORIGINS", "cors_origins"),
        description="Allowed CORS origins",
    )
    environment: RuntimeEnvironment = Field(
        default=RuntimeEnvironment.DEVELOPMENT,
        validation_alias=AliasChoices("ENVIRONMENT", "environment"),
    )
    public_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "public_base_url"),
    )
    trusted_hosts: Union[List[str], str] = Field(
        default_factory=lambda: [
            "localhost",
            "127.0.0.1",
            "[::1]",
            "testserver",
            "test",
            "gateway-test",
        ],
        validation_alias=AliasChoices("TRUSTED_HOSTS", "trusted_hosts"),
    )
    trusted_proxy_cidrs: Union[List[str], str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("TRUSTED_PROXY_CIDRS", "trusted_proxy_cidrs"),
    )
    legacy_api_keys_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LEGACY_API_KEYS_ENABLED",
            "legacy_api_keys_enabled",
        ),
    )
    api_key_peppers: dict[int, str] = Field(
        default_factory=dict,
        repr=False,
        validation_alias=AliasChoices("API_KEY_PEPPERS", "api_key_peppers"),
    )
    active_api_key_pepper_version: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "ACTIVE_API_KEY_PEPPER_VERSION",
            "active_api_key_pepper_version",
        ),
    )
    mfa_encryption_keys: dict[int, str] = Field(
        default_factory=dict,
        repr=False,
        validation_alias=AliasChoices(
            "MFA_ENCRYPTION_KEYS",
            "mfa_encryption_keys",
        ),
    )
    active_mfa_encryption_key_version: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "ACTIVE_MFA_ENCRYPTION_KEY_VERSION",
            "active_mfa_encryption_key_version",
        ),
    )
    webhook_encryption_keys: dict[int, str] = Field(
        default_factory=dict,
        repr=False,
        validation_alias=AliasChoices(
            "WEBHOOK_ENCRYPTION_KEYS",
            "webhook_encryption_keys",
        ),
    )
    active_webhook_encryption_key_version: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "ACTIVE_WEBHOOK_ENCRYPTION_KEY_VERSION",
            "active_webhook_encryption_key_version",
        ),
    )

    @property
    def csrf_allowed_origins(self) -> set[str]:
        if self.public_base_url:
            parsed = urlparse(self.public_base_url)
            return {f"{parsed.scheme}://{parsed.netloc}"}
        if self.environment is RuntimeEnvironment.PRODUCTION:
            return set()
        return {"http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3100", "http://127.0.0.1:3100"}
    max_request_body_bytes: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
        validation_alias=AliasChoices(
            "MAX_REQUEST_BODY_BYTES",
            "max_request_body_bytes",
        ),
    )
    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        validation_alias=AliasChoices("MAX_UPLOAD_BYTES", "max_upload_bytes"),
    )
    parser_memory_limit_bytes: int = Field(
        default=128 * 1024 * 1024,
        gt=0,
        validation_alias=AliasChoices(
            "PARSER_MEMORY_LIMIT_BYTES",
            "parser_memory_limit_bytes",
        ),
    )
    parser_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        validation_alias=AliasChoices(
            "PARSER_TIMEOUT_SECONDS",
            "parser_timeout_seconds",
        ),
    )
    parser_cpu_seconds: int = Field(
        default=30,
        gt=0,
        validation_alias=AliasChoices("PARSER_CPU_SECONDS", "parser_cpu_seconds"),
    )
    parser_max_pages: int = Field(
        default=500,
        gt=0,
        validation_alias=AliasChoices("PARSER_MAX_PAGES", "parser_max_pages"),
    )
    parser_max_output_characters: int = Field(
        default=5_000_000,
        gt=0,
        validation_alias=AliasChoices(
            "PARSER_MAX_OUTPUT_CHARACTERS",
            "parser_max_output_characters",
        ),
    )
    ingestion_chunk_size: int = Field(
        default=2000,
        gt=0,
        validation_alias=AliasChoices("INGESTION_CHUNK_SIZE", "ingestion_chunk_size"),
    )
    ingestion_chunk_overlap: int = Field(
        default=200,
        ge=0,
        validation_alias=AliasChoices("INGESTION_CHUNK_OVERLAP", "ingestion_chunk_overlap"),
    )
    ingestion_max_chunks: int = Field(
        default=10_000,
        gt=0,
        validation_alias=AliasChoices("INGESTION_MAX_CHUNKS", "ingestion_max_chunks"),
    )
    worker_lease_seconds: int = Field(
        default=60,
        gt=1,
        validation_alias=AliasChoices("WORKER_LEASE_SECONDS", "worker_lease_seconds"),
    )
    worker_heartbeat_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias=AliasChoices(
            "WORKER_HEARTBEAT_SECONDS",
            "worker_heartbeat_seconds",
        ),
    )
    worker_idle_delay_seconds: float = Field(
        default=0.5,
        gt=0,
        validation_alias=AliasChoices(
            "WORKER_IDLE_DELAY_SECONDS",
            "worker_idle_delay_seconds",
        ),
    )
    storage_reconcile_interval_seconds: int = Field(
        default=3600,
        gt=0,
        validation_alias=AliasChoices(
            "STORAGE_RECONCILE_INTERVAL_SECONDS",
            "storage_reconcile_interval_seconds",
        ),
    )
    storage_staging_ttl_seconds: int = Field(
        default=86_400,
        gt=0,
        validation_alias=AliasChoices(
            "STORAGE_STAGING_TTL_SECONDS",
            "storage_staging_ttl_seconds",
        ),
    )
    storage_orphan_grace_seconds: int = Field(
        default=86_400,
        gt=0,
        validation_alias=AliasChoices(
            "STORAGE_ORPHAN_GRACE_SECONDS",
            "storage_orphan_grace_seconds",
        ),
    )
    retention_purge_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("RETENTION_PURGE_ENABLED", "retention_purge_enabled"),
    )
    retention_interval_seconds: int = Field(
        default=86_400,
        ge=300,
        validation_alias=AliasChoices("RETENTION_INTERVAL_SECONDS", "retention_interval_seconds"),
    )
    retention_archive_days: int = Field(
        default=365,
        ge=365,
        validation_alias=AliasChoices("RETENTION_ARCHIVE_DAYS", "retention_archive_days"),
    )
    retention_revision_days: int = Field(
        default=1095,
        ge=1095,
        validation_alias=AliasChoices("RETENTION_REVISION_DAYS", "retention_revision_days"),
    )
    retention_operational_days: int = Field(
        default=30,
        ge=30,
        validation_alias=AliasChoices("RETENTION_OPERATIONAL_DAYS", "retention_operational_days"),
    )
    retention_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias=AliasChoices("RETENTION_BATCH_SIZE", "retention_batch_size"),
    )
    retention_max_batches_per_cycle: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias=AliasChoices(
            "RETENTION_MAX_BATCHES_PER_CYCLE",
            "retention_max_batches_per_cycle",
        ),
    )
    worker_metrics_interval_seconds: int = Field(
        default=60,
        ge=5,
        validation_alias=AliasChoices(
            "WORKER_METRICS_INTERVAL_SECONDS",
            "worker_metrics_interval_seconds",
        ),
    )
    worker_metrics_file: str | None = Field(
        default="/data/storage/metrics/worker.prom",
        validation_alias=AliasChoices(
            "WORKER_METRICS_FILE",
            "worker_metrics_file",
        ),
    )
    embedding_reembed_batch_size: int = Field(
        default=64,
        ge=1,
        le=512,
        validation_alias=AliasChoices(
            "EMBEDDING_REEMBED_BATCH_SIZE",
            "embedding_reembed_batch_size",
        ),
        description="Rows re-embedded per batch after an embedding dimension change",
    )
    stale_after_days: int = Field(
        default=90,
        ge=1,
        validation_alias=AliasChoices(
            "STALE_AFTER_DAYS",
            "stale_after_days",
        ),
        description="Age in days after which untouched knowledge is reported as stale",
    )
    git_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias=AliasChoices(
            "GIT_TIMEOUT_SECONDS",
            "git_timeout_seconds",
        ),
    )
    default_workspace_id: str = Field(
        default="global",
        validation_alias=AliasChoices("DEFAULT_WORKSPACE_ID", "default_workspace_id"),
        description="Default workspace identifier",
    )
    max_tool_iterations: int = Field(
        default=10,
        gt=0,
        validation_alias=AliasChoices("MAX_TOOL_ITERATIONS", "max_tool_iterations"),
        description="Maximum recursive tool iterations before terminating",
    )
    tool_timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        validation_alias=AliasChoices("TOOL_TIMEOUT_SECONDS", "tool_timeout_seconds"),
        description="Timeout for internal tool execution in seconds",
    )
    max_internal_tool_calls: int = Field(
        default=32,
        gt=0,
        validation_alias=AliasChoices("MAX_INTERNAL_TOOL_CALLS", "max_internal_tool_calls"),
        description="Maximum internal tool calls across one gateway request",
    )
    max_repeated_tool_signatures: int = Field(
        default=2,
        gt=0,
        validation_alias=AliasChoices(
            "MAX_REPEATED_TOOL_SIGNATURES",
            "max_repeated_tool_signatures",
        ),
        description="Maximum executions of one identical internal tool signature",
    )
    max_tool_wall_clock_seconds: float = Field(
        default=180.0,
        gt=0,
        validation_alias=AliasChoices(
            "MAX_TOOL_WALL_CLOCK_SECONDS",
            "max_tool_wall_clock_seconds",
        ),
        description="Maximum wall-clock duration for one orchestrated chat request",
    )
    quota_requests_per_minute: int = Field(
        default=120,
        gt=0,
        validation_alias=AliasChoices(
            "QUOTA_REQUESTS_PER_MINUTE",
            "quota_requests_per_minute",
        ),
        description="Requests allowed per minute per credential and space",
    )
    quota_concurrent_requests: int = Field(
        default=8,
        gt=0,
        validation_alias=AliasChoices(
            "QUOTA_CONCURRENT_REQUESTS",
            "quota_concurrent_requests",
        ),
        description="Concurrent in-flight requests allowed per credential and space",
    )
    quota_tokens_per_minute: int = Field(
        default=60000,
        gt=0,
        validation_alias=AliasChoices(
            "QUOTA_TOKENS_PER_MINUTE",
            "quota_tokens_per_minute",
        ),
        description="Chat tokens allowed per minute per credential and space",
    )
    quota_storage_bytes: int = Field(
        default=1073741824,
        gt=0,
        validation_alias=AliasChoices(
            "QUOTA_STORAGE_BYTES",
            "quota_storage_bytes",
        ),
        description="Stored upload bytes allowed per credential and space",
    )
    quota_burst_requests: int = Field(
        default=20,
        gt=0,
        validation_alias=AliasChoices(
            "QUOTA_BURST_REQUESTS",
            "quota_burst_requests",
        ),
        description="Burst requests absorbed before per-credential rate limiting engages",
    )
    knowledge_system_prompt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "KNOWLEDGE_SYSTEM_PROMPT_ENABLED",
            "knowledge_system_prompt_enabled",
        ),
        description="Whether to inject knowledge system prompt directives into conversations",
    )
    knowledge_system_prompt_custom: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "KNOWLEDGE_SYSTEM_PROMPT_CUSTOM",
            "knowledge_system_prompt_custom",
        ),
        description="Custom override for knowledge system prompt directive",
    )
    chat_context_max_sources: int = Field(
        default=5,
        gt=0,
        le=20,
        validation_alias=AliasChoices(
            "CHAT_CONTEXT_MAX_SOURCES",
            "chat_context_max_sources",
        ),
        description="Maximum sources assembled into internal chat-proxy search results",
    )
    chat_context_max_snippet_chars: int = Field(
        default=600,
        gt=0,
        le=4000,
        validation_alias=AliasChoices(
            "CHAT_CONTEXT_MAX_SNIPPET_CHARS",
            "chat_context_max_snippet_chars",
        ),
        description="Maximum characters per snippet in internal chat-proxy search results",
    )
    chat_context_max_total_chars: int = Field(
        default=8000,
        gt=0,
        le=60000,
        validation_alias=AliasChoices(
            "CHAT_CONTEXT_MAX_TOTAL_CHARS",
            "chat_context_max_total_chars",
        ),
        description="Maximum total characters across internal chat-proxy search results",
    )
    chat_context_max_total_tokens: int = Field(
        default=2000,
        gt=0,
        le=15000,
        validation_alias=AliasChoices(
            "CHAT_CONTEXT_MAX_TOTAL_TOKENS",
            "chat_context_max_total_tokens",
        ),
        description="Maximum estimated tokens across internal chat-proxy search results",
    )

    @field_validator("api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(k).strip() for k in parsed if str(k).strip()]
                except Exception:
                    pass
            return [k.strip() for k in v.split(",") if k.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(k).strip() for k in v if str(k).strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(o).strip() for o in parsed if str(o).strip()]
                except Exception:
                    pass
            return [o.strip() for o in v.split(",") if o.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(o).strip() for o in v if str(o).strip()]
        return v

    @field_validator("trusted_hosts", "trusted_proxy_cidrs", mode="before")
    @classmethod
    def parse_string_list(cls, value: Any) -> List[str]:
        if isinstance(value, str):
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    pass
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return value

    @field_validator("oidc_role_mapping", mode="before")
    @classmethod
    def parse_oidc_role_mapping(cls, value: Any) -> Dict[str, str]:
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"OIDC_ROLE_MAPPING is not valid JSON: {exc}") from exc
            value = parsed
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        return {}

    @field_validator("oidc_group_space_map", mode="before")
    @classmethod
    def parse_oidc_group_space_map(cls, value: Any) -> Dict[str, List[Dict[str, str]]]:
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"OIDC_GROUP_SPACE_MAP is not valid JSON: {exc}") from exc
            value = parsed
        if isinstance(value, dict):
            normalized: Dict[str, List[Dict[str, str]]] = {}
            for group, grants in value.items():
                items = grants if isinstance(grants, list) else [grants]
                cleaned = []
                for grant in items:
                    if isinstance(grant, dict) and grant.get("space_id") and grant.get("role"):
                        cleaned.append({"space_id": str(grant["space_id"]), "role": str(grant["role"])})
                normalized[str(group)] = cleaned
            return normalized
        return {}

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_proxy_cidrs(cls, values: List[str]) -> List[str]:
        for value in values:
            ip_network(value, strict=False)
        return values

    @model_validator(mode="after")
    def validate_upload_fits_body_limit(self) -> "GatewaySettings":
        if self.max_upload_bytes > self.max_request_body_bytes:
            raise ValueError("MAX_UPLOAD_BYTES must not exceed MAX_REQUEST_BODY_BYTES")
        return self

    @property
    def gateway_api_keys(self) -> List[str]:

        if isinstance(self.api_keys, list):
            return self.api_keys
        return [self.api_keys]

    def validate_runtime_safety(
        self,
        database: Optional[DatabaseSettings] = None,
    ) -> None:
        if self.environment is not RuntimeEnvironment.PRODUCTION:
            return

        errors = []

        def add_error(location: tuple[str, ...], message: str) -> None:
            errors.append(
                {
                    "type": "value_error",
                    "loc": location,
                    "input": "<redacted>",
                    "ctx": {"error": ValueError(message)},
                }
            )

        if "*" in self.cors_origins:
            add_error(("gateway", "cors_origins"), "wildcard CORS is forbidden in production")
        if not self.public_base_url or urlparse(self.public_base_url).scheme != "https":
            add_error(("gateway", "public_base_url"), "an HTTPS public base URL is required in production")
        if not self.trusted_hosts or "*" in self.trusted_hosts:
            add_error(("gateway", "trusted_hosts"), "explicit trusted hosts are required in production")
        if self.log_level.upper() == "DEBUG":
            add_error(("gateway", "log_level"), "debug logging is forbidden in production")
        if self.gateway_api_keys:
            add_error(("gateway", "api_keys"), "legacy static API keys are forbidden in production")
        if self.legacy_api_keys_enabled:
            add_error(("gateway", "legacy_api_keys_enabled"), "legacy API key compatibility is forbidden in production")
        if not self.api_key_peppers or any(
            len(pepper.encode("utf-8")) < 32
            for pepper in self.api_key_peppers.values()
        ):
            add_error(
                ("gateway", "api_key_peppers"),
                "at least one 32-byte API key pepper is required in production",
            )
        elif self.active_api_key_pepper_version not in self.api_key_peppers:
            add_error(
                ("gateway", "active_api_key_pepper_version"),
                "the active API key pepper version must exist",
            )
        valid_mfa_keys = bool(self.mfa_encryption_keys)
        for value in self.mfa_encryption_keys.values():
            try:
                decoded = base64.b64decode(value, altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                valid_mfa_keys = False
                break
            if len(decoded) != 32:
                valid_mfa_keys = False
                break
        if not valid_mfa_keys:
            add_error(
                ("gateway", "mfa_encryption_keys"),
                "at least one valid Fernet MFA encryption key is required in production",
            )
        elif self.active_mfa_encryption_key_version not in self.mfa_encryption_keys:
            add_error(
                ("gateway", "active_mfa_encryption_key_version"),
                "the active MFA encryption key version must exist",
            )
        if database and database.echo:
            add_error(("database", "echo"), "database echo is forbidden in production")

        if errors:
            raise ValidationError.from_exception_data("RuntimeSafety", errors)

    def validate_storage_backend(self) -> None:
        if self.storage_backend not in {"local", "s3"}:
            raise ValueError("STORAGE_BACKEND must be 'local' or 's3'.")
        if self.storage_backend != "s3":
            return
        missing = [
            name
            for name, value in (
                ("STORAGE_S3_ENDPOINT", self.storage_s3_endpoint),
                ("STORAGE_S3_BUCKET", self.storage_s3_bucket),
                ("STORAGE_S3_REGION", self.storage_s3_region),
                ("STORAGE_S3_ACCESS_KEY", self.storage_s3_access_key),
                ("STORAGE_S3_SECRET_KEY", self.storage_s3_secret_key),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise ValueError(
                "STORAGE_BACKEND=s3 requires " + ", ".join(missing) + "."
            )

    def validate_oidc_settings(self) -> None:
        if not self.oidc_enabled:
            return
        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER", self.oidc_issuer),
                ("OIDC_CLIENT_ID", self.oidc_client_id),
                ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ("OIDC_REDIRECT_URL", self.oidc_redirect_url),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise ValueError(
                "OIDC_ENABLED=true requires " + ", ".join(missing) + "."
            )
        issuer = (self.oidc_issuer or "").strip()
        try:
            parsed = urlparse(issuer)
        except ValueError as exc:
            raise ValueError("OIDC_ISSUER is not a valid URL.") from exc
        is_https = parsed.scheme == "https" and bool(parsed.hostname)
        is_localhost_http = parsed.scheme == "http" and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1"}
        if not (is_https or is_localhost_http):
            raise ValueError("OIDC_ISSUER must be an HTTPS URL, except exact http://localhost[:port] or http://127.0.0.1[:port].")
        valid_roles = {"member", "super_admin"}
        valid_space_roles = {"owner", "editor", "reader"}
        for group, role in (self.oidc_role_mapping or {}).items():
            if role not in valid_roles:
                raise ValueError(
                    f"OIDC_ROLE_MAPPING group '{group}' must map to member or super_admin."
                )
        for group, grants in (self.oidc_group_space_map or {}).items():
            for grant in grants:
                if grant.get("role") not in valid_space_roles or not grant.get("space_id"):
                    raise ValueError(
                        f"OIDC_GROUP_SPACE_MAP group '{group}' grants require space_id and owner/editor/reader role."
                    )

class AppSettings(BaseSettings):

    model_config = _env_aware_config()

    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)

    def __init__(self, **values: Any) -> None:
        llm_val = values.pop("llm", None)
        emb_val = values.pop("embedding", None)
        db_val = values.pop("database", None)
        gw_val = values.pop("gateway", None)

        super().__init__(**values)

        if isinstance(llm_val, dict):
            object.__setattr__(self, "llm", LLMSettings(_env_file=None, **llm_val))
        elif isinstance(llm_val, LLMSettings):
            object.__setattr__(self, "llm", llm_val)

        if isinstance(emb_val, dict):
            object.__setattr__(self, "embedding", EmbeddingSettings(_env_file=None, **emb_val))
        elif isinstance(emb_val, EmbeddingSettings):
            object.__setattr__(self, "embedding", emb_val)

        if isinstance(db_val, dict):
            object.__setattr__(self, "database", DatabaseSettings(_env_file=None, **db_val))
        elif isinstance(db_val, DatabaseSettings):
            object.__setattr__(self, "database", db_val)

        if isinstance(gw_val, dict):
            object.__setattr__(self, "gateway", GatewaySettings(_env_file=None, **gw_val))
        elif isinstance(gw_val, GatewaySettings):
            object.__setattr__(self, "gateway", gw_val)

    def validate_runtime_safety(self) -> None:
        self.gateway.validate_runtime_safety(self.database)

Settings = AppSettings

@lru_cache()
def _get_cached_settings() -> AppSettings:

    return AppSettings()

_runtime_settings: ContextVar[Optional[AppSettings]] = ContextVar(
    "gateway_runtime_settings",
    default=None,
)

def get_settings() -> AppSettings:

    return _runtime_settings.get() or _get_cached_settings()

def set_runtime_settings(value: AppSettings) -> Token:

    return _runtime_settings.set(value)

def reset_runtime_settings(token: Token) -> None:

    _runtime_settings.reset(token)

get_settings.cache_clear = _get_cached_settings.cache_clear
get_settings.cache_info = _get_cached_settings.cache_info

settings: AppSettings = get_settings()
