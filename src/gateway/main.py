

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from src.gateway.config import get_settings
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.database import close_db_engine, get_session_factory
from src.gateway.infrastructure.migrations import run_migrations_async
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.routers import (
    chat_completions_router,
    files_router,
    health_router,
    messages_router,
    models_router,
)

logger = logging.getLogger(__name__)

async def bootstrap_global_workspace() -> None:

    session_factory = get_session_factory()
    default_ws_id = get_settings().gateway.default_workspace_id
    async with session_factory() as session:
        async with session.begin():
            stmt = select(Workspace).where(Workspace.id == default_ws_id)
            result = await session.execute(stmt)
            workspace = result.scalar_one_or_none()
            if workspace is None:
                logger.info("Bootstrapping default 'global' workspace...")
                workspace = Workspace(
                    id=default_ws_id,
                    name="Global Workspace",
                )
                session.add(workspace)
                await session.flush()
                logger.info("Default 'global' workspace bootstrapped successfully.")
            else:
                logger.debug("Default 'global' workspace already exists.")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:

    logger.info("Starting AI Gateway infrastructure initialization...")

    try:
        logger.info("Applying automated Alembic database migrations...")
        await run_migrations_async()
        logger.info("Alembic database migrations applied.")
    except Exception as exc:
        logger.error(f"Fatal error running database migrations during startup: {exc}", exc_info=True)
        raise exc

    try:
        logger.info("Verifying default 'global' workspace...")
        await bootstrap_global_workspace()
        logger.info("Workspace verification complete.")
    except Exception as exc:
        logger.error(f"Fatal error bootstrapping global workspace: {exc}", exc_info=True)
        raise exc

    logger.info("AI Gateway startup completed successfully.")
    yield

    logger.info("Shutting down AI Gateway...")
    await close_db_engine()
    await HttpLLMClient.close_shared_client()
    await HTTPEmbeddingClient.close_shared_client()
    logger.info("AI Gateway shutdown complete.")

def create_app() -> FastAPI:

    app = FastAPI(
        title="Local AI Gateway with Internal Shared Knowledge",
        version="0.1.0",
        description="Production-grade API-first Local AI Gateway with hybrid retrieval and shared knowledge",
        lifespan=lifespan,
    )

    current_settings = get_settings()
    cors_origins = (
        current_settings.gateway.cors_origins
        if isinstance(current_settings.gateway.cors_origins, list)
        else [current_settings.gateway.cors_origins]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(APIKeyAuthMiddleware)

    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(chat_completions_router)
    app.include_router(messages_router)
    app.include_router(files_router)

    return app

app = create_app()
