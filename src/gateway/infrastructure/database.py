

from collections.abc import AsyncGenerator
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.gateway.config import get_settings

logger = logging.getLogger(__name__)

def normalize_database_url(url: str) -> str:

    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None

def get_engine() -> AsyncEngine:

    global _engine, _session_factory
    current_settings = get_settings()
    db_url = normalize_database_url(current_settings.database.url)
    if _engine is not None:
        if str(_engine.url) != db_url:
            _engine = None
            _session_factory = None

    if _engine is None:
        if "sqlite" in db_url:
            from sqlalchemy.pool import StaticPool
            _engine = create_async_engine(
                db_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
                echo=current_settings.gateway.log_level.upper() == "DEBUG" or getattr(current_settings.database, "echo", False),
            )
        else:
            _engine = create_async_engine(
                db_url,
                pool_size=getattr(current_settings.database, "pool_size", 20),
                max_overflow=getattr(current_settings.database, "max_overflow", 10),
                pool_pre_ping=True,
                pool_recycle=getattr(current_settings.database, "pool_recycle", 1800),
                pool_timeout=getattr(current_settings.database, "pool_timeout", 30.0),
                echo=current_settings.gateway.log_level.upper() == "DEBUG" or getattr(current_settings.database, "echo", False),
            )
    return _engine

def get_session_factory() -> async_sessionmaker[AsyncSession]:

    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _session_factory

def set_session_factory(factory: Optional[async_sessionmaker[AsyncSession]]) -> None:

    global _session_factory
    _session_factory = factory

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def close_db_engine() -> None:

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connection pool disposed.")
