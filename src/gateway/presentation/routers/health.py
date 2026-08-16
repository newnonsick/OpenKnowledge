

import logging
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.infrastructure.database import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

@router.get("/health")
@router.get("/v1/health")
async def health_check(session: AsyncSession = Depends(get_db_session)):

    try:
        await session.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
        }
    except Exception as exc:
        logger.warning(f"Database health check failed: {exc}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "error": str(exc),
            },
        )
