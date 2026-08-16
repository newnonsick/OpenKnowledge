

from functools import lru_cache
import json
from typing import Any, List, Optional, Union
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _env_aware_config() -> SettingsConfigDict:

    return SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

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
    api_key: str = Field(
        default="EMPTY",
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
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "embedding_api_key", "api_key"),
        description="API key for embedding endpoint",
    )
    dimension: int = Field(
        default=768,
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

class DatabaseSettings(BaseSettings):

    model_config = _env_aware_config()

    url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_db",
        validation_alias=AliasChoices("DATABASE_URL", "DB_URL", "database_url", "db_url", "url"),
        description="SQLAlchemy async database connection URL",
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
        default="0.0.0.0",
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
    storage_dir: str = Field(
        default="./data/storage",
        validation_alias=AliasChoices("STORAGE_DIR", "storage_dir"),
        description="Local disk storage directory for uploaded files",
    )
    api_keys: Union[List[str], str] = Field(
        default_factory=lambda: ["sk-gateway-default-key"],
        validation_alias=AliasChoices("GATEWAY_API_KEYS", "gateway_api_keys", "api_keys"),
        description="Allowed API keys for client authentication",
    )
    cors_origins: Union[List[str], str] = Field(
        default_factory=lambda: ["*"],
        validation_alias=AliasChoices("CORS_ORIGINS", "cors_origins"),
        description="Allowed CORS origins",
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

    @property
    def gateway_api_keys(self) -> List[str]:

        if isinstance(self.api_keys, list):
            return self.api_keys
        return [self.api_keys]

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
            object.__setattr__(self, "llm", LLMSettings(**llm_val))
        elif isinstance(llm_val, LLMSettings):
            object.__setattr__(self, "llm", llm_val)

        if isinstance(emb_val, dict):
            object.__setattr__(self, "embedding", EmbeddingSettings(**emb_val))
        elif isinstance(emb_val, EmbeddingSettings):
            object.__setattr__(self, "embedding", emb_val)

        if isinstance(db_val, dict):
            object.__setattr__(self, "database", DatabaseSettings(**db_val))
        elif isinstance(db_val, DatabaseSettings):
            object.__setattr__(self, "database", db_val)

        if isinstance(gw_val, dict):
            object.__setattr__(self, "gateway", GatewaySettings(**gw_val))
        elif isinstance(gw_val, GatewaySettings):
            object.__setattr__(self, "gateway", gw_val)

Settings = AppSettings

@lru_cache()
def get_settings() -> AppSettings:

    return AppSettings()

settings: AppSettings = get_settings()
