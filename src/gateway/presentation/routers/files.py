from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.gateway.application.services.permission_service import require_profile_route
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal
from src.gateway.presentation.authorization import require_scope


router = APIRouter(prefix="/v1/files", tags=["files"])


@router.post("/upload", include_in_schema=False)
async def retired_upload(
    request: Request,
    principal: Principal = Depends(require_scope("knowledge:write")),
) -> None:
    authenticated = getattr(request.state, "principal", None)
    if authenticated is None:
        raise AuthorizationException()
    require_profile_route(authenticated, "source.upload")
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use /api/v1/sources/upload for durable ingestion.",
    )
