from fastapi import APIRouter, Depends, HTTPException, status

from src.gateway.domain.identity import Principal
from src.gateway.presentation.authorization import require_scope


router = APIRouter(prefix="/v1/files", tags=["files"])


@router.post("/upload", include_in_schema=False)
async def retired_upload(
    principal: Principal = Depends(require_scope("knowledge:write")),
) -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use /api/v1/sources/upload for durable ingestion.",
    )
