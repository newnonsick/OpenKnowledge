from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from src.gateway.presentation.request_context import get_request_id

router = APIRouter(tags=["Health"])

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["live", "ready", "not_ready", "healthy"]
    request_id: str
    code: Literal["schema_incompatible"] | None = None


@router.get("/healthz/live", response_model=HealthResponse, response_model_exclude_none=True)
async def liveness_check(request: Request):
    return {"status": "live", "request_id": get_request_id(request)}


@router.get(
    "/healthz/ready",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    responses={503: {"model": HealthResponse}},
)
async def readiness_check(
    request: Request,
    response: Response,
):
    if getattr(request.app.state, "schema_compatible", None) is False:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "code": "schema_incompatible",
            "request_id": get_request_id(request),
        }

    if await request.app.state.readiness_probe.check():
        return {
            "status": "ready",
            "request_id": get_request_id(request),
        }
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "not_ready",
        "request_id": get_request_id(request),
    }


@router.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
@router.get("/v1/health", response_model=HealthResponse, response_model_exclude_none=True)
async def compatibility_health_check(
    request: Request,
):
    return {
        "status": "healthy",
        "request_id": get_request_id(request),
    }
