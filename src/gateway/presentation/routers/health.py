

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from src.gateway.presentation.request_context import get_request_id

router = APIRouter(tags=["Health"])

@router.get("/healthz/live")
async def liveness_check(request: Request):
    return {"status": "live", "request_id": get_request_id(request)}


@router.get("/healthz/ready")
async def readiness_check(
    request: Request,
):

    if getattr(request.app.state, "schema_compatible", None) is False:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "code": "schema_incompatible",
                "request_id": get_request_id(request),
            },
        )

    if await request.app.state.readiness_probe.check():
        return {
            "status": "ready",
            "request_id": get_request_id(request),
        }
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "not_ready",
            "request_id": get_request_id(request),
        },
    )


@router.get("/health")
@router.get("/v1/health")
async def compatibility_health_check(
    request: Request,
):
    return {
        "status": "healthy",
        "request_id": get_request_id(request),
    }
