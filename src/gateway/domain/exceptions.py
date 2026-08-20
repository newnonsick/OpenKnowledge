

from typing import Any, Dict, Optional

class GatewayException(Exception):

    def __init__(
        self,
        message: str = "A gateway error occurred.",
        status_code: int = 500,
        error_type: str = "gateway_error",
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code or error_type
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:

        payload: Dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
        }
        if self.details:
            payload["details"] = self.details
        return payload

class AuthenticationException(GatewayException):

    def __init__(
        self,
        message: str = "Invalid or missing API key.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
            details=details,
        )

class AuthorizationException(GatewayException):

    def __init__(
        self,
        message: str = "Resource is unavailable.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=404,
            error_type="authorization_error",
            code="resource_unavailable",
            details=details,
        )


class CSRFException(GatewayException):

    def __init__(self) -> None:
        super().__init__(
            message="Request verification failed.",
            status_code=403,
            error_type="request_verification_error",
            code="csrf_verification_failed",
        )

class ResourceConflictException(GatewayException):

    def __init__(
        self,
        message: str = "Resource state conflict.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=409,
            error_type="conflict_error",
            code="resource_conflict",
            details=details,
        )

class ItemNotFoundException(GatewayException):

    def __init__(
        self,
        message: str = "Requested resource not found.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=404,
            error_type="not_found_error",
            code="resource_not_found",
            details=details,
        )

class ConcurrencyConflictException(GatewayException):

    def __init__(
        self,
        message_or_item_id: str = "Version conflict.",
        expected_version: Optional[int] = None,
        actual_version: Optional[int] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        det = details.copy() if details else {}
        if expected_version is not None and actual_version is not None:
            msg = message or (
                f"Version conflict on item {message_or_item_id}: expected version {expected_version}, "
                f"but current version is {actual_version}. Please re-read and retry."
            )
            det.update({
                "item_id": message_or_item_id,
                "expected_version": expected_version,
                "actual_version": actual_version,
            })
        else:
            msg = message or message_or_item_id

        super().__init__(
            message=msg,
            status_code=409,
            error_type="concurrency_conflict_error",
            code="version_mismatch",
            details=det,
        )

class JobLeaseLostException(GatewayException):

    def __init__(self) -> None:
        super().__init__(
            message="The ingestion job lease is no longer valid.",
            status_code=409,
            error_type="concurrency_conflict_error",
            code="job_lease_lost",
        )

class ModelNotFoundException(GatewayException):

    def __init__(
        self,
        requested_model_or_message: str = "Model not found.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        det = details.copy() if details else {}
        if not det and not requested_model_or_message.startswith("The model "):
            det["requested_model"] = requested_model_or_message
        msg = (
            requested_model_or_message
            if " " in requested_model_or_message
            else f"The model '{requested_model_or_message}' does not exist or is not available."
        )
        super().__init__(
            message=msg,
            status_code=404,
            error_type="not_found_error",
            code="model_not_found",
            details=det,
        )

class ToolExecutionException(GatewayException):

    def __init__(
        self,
        tool_name_or_message: str = "Tool execution failed.",
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        det = details.copy() if details else {}
        if message is not None:
            tool_name = tool_name_or_message
            det["tool_name"] = tool_name
            full_msg = f"Tool execution failed for '{tool_name}': {message}"
        else:
            full_msg = tool_name_or_message

        super().__init__(
            message=full_msg,
            status_code=500,
            error_type="tool_execution_error",
            code="tool_failed",
            details=det,
        )

class StorageException(GatewayException):

    def __init__(
        self,
        message: str = "Storage operation failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=500,
            error_type="storage_error",
            code="storage_operation_failed",
            details=details,
        )

class UploadTooLargeException(GatewayException):

    def __init__(self) -> None:
        super().__init__(
            message="The uploaded file exceeds the configured size limit.",
            status_code=413,
            error_type="validation_error",
            code="upload_too_large",
        )

class ParserTimeoutException(GatewayException):

    def __init__(self) -> None:
        super().__init__(
            message="Document parsing exceeded the configured time limit.",
            status_code=422,
            error_type="validation_error",
            code="parser_timeout",
        )

class ValidationException(GatewayException):

    def __init__(
        self,
        message: str = "Validation failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=422,
            error_type="validation_error",
            code="invalid_payload",
            details=details,
        )

class EmbeddingException(GatewayException):

    def __init__(
        self,
        message: str = "Embedding provider failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=502,
            error_type="embedding_error",
            code="embedding_provider_error",
            details=details,
        )

class LLMProviderException(GatewayException):

    def __init__(
        self,
        message: str = "LLM provider failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=502,
            error_type="llm_provider_error",
            code="llm_service_failed",
            details=details,
        )
