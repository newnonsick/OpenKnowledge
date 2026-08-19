

from __future__ import annotations

import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from src.gateway.application.services.model_registry import (
    ModelRegistryService,
    get_model_registry,
)
from src.gateway.domain.exceptions import ModelNotFoundException
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIErrorDetail,
    OpenAIErrorResponse,
    OpenAIModelListResponse,
    OpenAIModelObject,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1",
    tags=["Models"],
    dependencies=[Depends(require_scope("chat:write"))],
)

@router.get("/models", response_model=OpenAIModelListResponse)
async def list_models(
    registry: ModelRegistryService = Depends(get_model_registry),
) -> Dict[str, Any]:

    models = registry.list_models()
    return {
        "object": "list",
        "data": models,
    }

@router.get("/models/{model_id:path}")
async def get_model(
    model_id: str,
    registry: ModelRegistryService = Depends(get_model_registry),
) -> Dict[str, Any]:

    try:
        model_info = registry.get_model_info(model_id)
        return model_info
    except ModelNotFoundException as exc:
        logger.warning(f"Model not found: {model_id}")
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "message": exc.message,
                    "type": exc.error_type,
                    "code": exc.code,
                }
            },
        )
