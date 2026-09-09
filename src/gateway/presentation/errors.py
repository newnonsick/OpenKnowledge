from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.gateway.domain.exceptions import GatewayException
from src.gateway.presentation.request_context import get_request_id
from src.gateway.presentation.security_headers import apply_security_headers


logger = logging.getLogger(__name__)


def protocol_error_response(
    request: Request,
    status_code: int,
    error_type: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = get_request_id(request)
    if request.url.path.startswith("/v1/messages"):
        content = {
            "type": "error",
            "error": {"type": error_type, "message": message},
            "request_id": request_id,
        }
    else:
        error = {"message": message, "type": error_type, "code": code}
        if details:
            error["details"] = details
        content = {
            "error": error,
            "request_id": request_id,
        }
    response = JSONResponse(status_code=status_code, content=content)
    response.headers["X-Request-ID"] = request_id
    if status_code == 429:
        response.headers["Retry-After"] = str(max(1, int(getattr(request.state, "retry_after_seconds", 1))))
    return apply_security_headers(response, request.url.path)


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    fields: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for error in exc.errors()[:20]:
        field = ".".join(str(part) for part in error.get("loc", ()))[:255]
        code = str(error.get("type", "invalid"))[:120]
        key = (field, code)
        if key not in seen:
            seen.add(key)
            fields.append({"field": field, "code": code})
    return {"fields": fields}


def _gateway_details(exc: GatewayException) -> dict[str, Any] | None:
    details: dict[str, Any] = {}
    retry_after = exc.details.get("retry_after_seconds")
    if isinstance(retry_after, int) and not isinstance(retry_after, bool):
        details["retry_after_seconds"] = max(1, retry_after)
    item_id = exc.details.get("item_id")
    if isinstance(item_id, str):
        details["item_id"] = item_id[:255]
    for key in ("expected_version", "actual_version"):
        value = exc.details.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            details[key] = value
    return details or None


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return protocol_error_response(
            request,
            422,
            "invalid_request_error",
            "invalid_payload",
            "The request payload is invalid.",
            details=_validation_details(exc),
        )

    @app.exception_handler(GatewayException)
    async def gateway_error_handler(
        request: Request,
        exc: GatewayException,
    ) -> JSONResponse:
        if exc.status_code == 429:
            request.state.retry_after_seconds = exc.details.get("retry_after_seconds", 1)
        message = exc.message if exc.status_code < 500 else "A gateway dependency failed."
        return protocol_error_response(
            request,
            exc.status_code,
            exc.error_type,
            exc.code,
            message,
            details=_gateway_details(exc),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else "Request failed."
        return protocol_error_response(
            request,
            exc.status_code,
            "request_error",
            "http_error",
            message,
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return protocol_error_response(
            request,
            404,
            "not_found_error",
            "route_not_found",
            "The requested route does not exist.",
        )

    @app.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return protocol_error_response(
            request,
            405,
            "invalid_request_error",
            "method_not_allowed",
            "The HTTP method is not allowed for the requested route.",
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.error(
            "Unhandled request error",
            extra={
                "request_id": get_request_id(request),
                "exception_class": type(exc).__name__,
            },
        )
        return protocol_error_response(
            request,
            500,
            "api_error" if request.url.path.startswith("/v1/messages") else "internal_server_error",
            "internal_error",
            "An internal server error occurred.",
        )
