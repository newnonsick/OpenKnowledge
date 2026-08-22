

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.gateway.config import (
    AppSettings,
    RuntimeEnvironment,
    get_settings,
    reset_runtime_settings,
    set_runtime_settings,
)
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.database import close_db_engine, get_session_factory, validate_runtime_database_role
from src.gateway.infrastructure.migrations import get_schema_status_async
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.readiness import ReadinessProbe
from src.gateway.observability import configure_logging
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.request_context import RequestContextMiddleware
from src.gateway.presentation.request_limits import RequestBodyLimitMiddleware
from src.gateway.presentation.metrics import MetricsMiddleware, MetricsRegistry
from src.gateway.presentation.security_headers import SecurityHeadersMiddleware
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from src.gateway.presentation.routers import (
    chat_completions_router,
    files_router,
    health_router,
    management_auth_router,
    management_router,
    messages_router,
    models_router,
)

logger = logging.getLogger(__name__)

async def bootstrap_global_workspace(
    app_settings: Optional[AppSettings] = None,
) -> None:

    current_settings = app_settings or get_settings()
    token = set_runtime_settings(current_settings)
    try:
        session_factory = get_session_factory()
        default_ws_id = current_settings.gateway.default_workspace_id
        async with session_factory() as session:
            async with session.begin():
                stmt = select(Workspace).where(Workspace.id == default_ws_id)
                result = await session.execute(stmt)
                workspace = result.scalar_one_or_none()
                if workspace is None:
                    logger.info("Bootstrapping default workspace")
                    workspace = Workspace(
                        id=default_ws_id,
                        name="Global Workspace",
                    )
                    session.add(workspace)
                    await session.flush()
                    logger.info("Default workspace bootstrapped successfully")
                else:
                    logger.debug("Default workspace already exists")
    finally:
        reset_runtime_settings(token)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:

    settings_token = set_runtime_settings(app.state.settings)
    logger.info("Starting AI Gateway infrastructure initialization...")
    try:
        status = await get_schema_status_async(
            app.state.settings.database.url,
            expected_embedding_dimension=app.state.settings.embedding.dimension,
        )
        app.state.schema_revision = status.current_revision
        app.state.schema_compatible = status.compatible
    except Exception as exc:
        app.state.schema_revision = None
        app.state.schema_compatible = False
        logger.error(
            "Database schema compatibility check failed",
            extra={"exception_class": type(exc).__name__},
        )

    if not app.state.schema_compatible:
        logger.error("Database schema is incompatible; readiness is disabled")
    elif app.state.settings.gateway.environment is RuntimeEnvironment.PRODUCTION:
        try:
            await validate_runtime_database_role()
        except Exception as exc:
            app.state.schema_compatible = False
            logger.error(
                "Runtime database role validation failed",
                extra={"exception_class": type(exc).__name__},
            )

    logger.info("AI Gateway startup completed successfully.")
    try:
        yield
    finally:
        logger.info("Shutting down AI Gateway...")
        await close_db_engine()
        await HttpLLMClient.close_shared_client()
        await HTTPEmbeddingClient.close_shared_client()
        reset_runtime_settings(settings_token)
        logger.info("AI Gateway shutdown complete.")

def create_app(app_settings: Optional[AppSettings] = None) -> FastAPI:

    current_settings = app_settings or get_settings()
    current_settings.validate_runtime_safety()
    if current_settings.gateway.environment is not RuntimeEnvironment.TEST:
        configure_logging(current_settings.gateway.log_level)

    app = FastAPI(
        title="Local AI Gateway with Internal Shared Knowledge",
        version="0.1.0",
        description="Production-grade API-first Local AI Gateway with hybrid retrieval and shared knowledge",
        lifespan=lifespan,
    )
    app.state.settings = current_settings
    app.state.schema_compatible = None
    app.state.readiness_probe = ReadinessProbe(current_settings.database.url)
    app.state.metrics = MetricsRegistry()
    register_exception_handlers(app)

    cors_origins = (
        current_settings.gateway.cors_origins
        if isinstance(current_settings.gateway.cors_origins, list)
        else [current_settings.gateway.cors_origins]
    )
    app.add_middleware(
        APIKeyAuthMiddleware,
        allowed_keys=current_settings.gateway.gateway_api_keys,
        api_key_peppers=current_settings.gateway.api_key_peppers,
        active_api_key_pepper_version=(
            current_settings.gateway.active_api_key_pepper_version
        ),
        legacy_api_keys_enabled=current_settings.gateway.legacy_api_keys_enabled,
        require_persisted_legacy_principals=(
            current_settings.gateway.environment is not RuntimeEnvironment.TEST
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=current_settings.gateway.trusted_hosts,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SettingsContextMiddleware)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=current_settings.gateway.max_request_body_bytes,
    )
    app.add_middleware(MetricsMiddleware, registry=app.state.metrics)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            app.state.metrics.render(),
            media_type="text/plain; version=0.0.4",
        )

    app.include_router(health_router)
    app.include_router(management_auth_router)
    app.include_router(management_router)
    app.include_router(models_router)
    app.include_router(chat_completions_router)
    app.include_router(messages_router)
    app.include_router(files_router)

    def authoritative_openapi() -> dict:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            summary=app.summary,
            description=app.description,
            terms_of_service=app.terms_of_service,
            contact=app.contact,
            license_info=app.license_info,
            routes=app.routes,
            webhooks=app.webhooks.routes,
            tags=app.openapi_tags,
            servers=app.servers,
            separate_input_output_schemas=app.separate_input_output_schemas,
            external_docs=app.openapi_external_docs,
        )
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "cookieAuth": {
                "type": "apiKey",
                "in": "cookie",
                "name": "__Host-aigw-access",
            },
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
            },
        }
        public_operations = {
            ("/api/v1/auth/login", "post"),
            ("/api/v1/auth/refresh", "post"),
            ("/health", "get"),
            ("/healthz/live", "get"),
            ("/healthz/ready", "get"),
            ("/v1/health", "get"),
        }
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if method not in {"delete", "get", "patch", "post", "put"}:
                    continue
                if (path, method) in public_operations:
                    operation.pop("security", None)
                elif path.startswith("/api/v1/auth/"):
                    operation["security"] = [{"cookieAuth": []}]
                elif path.startswith("/v1/"):
                    operation["security"] = [{"bearerAuth": []}]
                else:
                    operation["security"] = [
                        {"cookieAuth": []},
                        {"bearerAuth": []},
                    ]
        app.openapi_schema = schema
        return schema

    app.openapi = authoritative_openapi

    return app

app = create_app()
