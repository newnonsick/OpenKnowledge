import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from src.gateway.domain.exceptions import ConcurrencyConflictException
from src.gateway.main import create_app
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.schemas.management_responses import ErrorEnvelope


MANAGEMENT_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/ai-",
    "/api/v1/api-keys",
    "/api/v1/audit-events",
    "/api/v1/ingestion-jobs",
    "/api/v1/knowledge",
    "/api/v1/me",
    "/api/v1/members",
    "/api/v1/operations/summary",
    "/api/v1/retrieval/search",
    "/api/v1/sessions",
    "/api/v1/settings",
    "/api/v1/sources",
    "/api/v1/spaces",
)
MANAGEMENT_ERROR_STATUSES = {"401", "403", "404", "409", "413", "422", "429", "500", "502"}


class ValidationProbe(BaseModel):
    limit: int = Field(ge=1)


def test_management_success_responses_have_authoritative_openapi_schemas() -> None:
    schema = create_app().openapi()
    missing: list[str] = []
    generic: list[str] = []
    for path, path_item in schema["paths"].items():
        if not path.startswith(MANAGEMENT_PREFIXES):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            success = next(
                (response for status, response in operation["responses"].items() if status.startswith("2")),
                None,
            )
            if success is None or success.get("description") == "Successful Response" and not success.get("content"):
                continue
            response_schema = success.get("content", {}).get("application/json", {}).get("schema")
            label = f"{method.upper()} {path}"
            if response_schema is None:
                missing.append(label)
            elif response_schema.get("additionalProperties") is True:
                generic.append(label)
    assert not missing
    assert not generic


def test_committed_openapi_contract_matches_application() -> None:
    committed = json.loads(
        Path("web/lib/generated/openapi.json").read_text(encoding="utf-8")
    )
    assert committed == create_app().openapi()


def test_openapi_declares_public_and_protected_authentication_contracts() -> None:
    schema = create_app().openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["cookieAuth"] == {"type": "apiKey", "in": "cookie", "name": "__Host-openknowledge-access"}
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]
    assert schema["paths"]["/api/v1/auth/step-up"]["post"]["security"] == [{"cookieAuth": []}]
    assert schema["paths"]["/api/v1/spaces"]["get"]["security"] == [
        {"cookieAuth": []},
        {"bearerAuth": []},
    ]
    assert schema["paths"]["/v1/chat/completions"]["post"]["security"] == [{"bearerAuth": []}]


def test_management_errors_use_the_runtime_error_envelope() -> None:
    schema = create_app().openapi()
    mismatches: list[str] = []
    for path, path_item in schema["paths"].items():
        if not path.startswith(MANAGEMENT_PREFIXES):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            responses = operation["responses"]
            if not MANAGEMENT_ERROR_STATUSES.issubset(responses):
                mismatches.append(f"{method.upper()} {path}: missing error statuses")
                continue
            for status in MANAGEMENT_ERROR_STATUSES:
                response_schema = responses[status]["content"]["application/json"]["schema"]
                if response_schema != {"$ref": "#/components/schemas/ErrorEnvelope"}:
                    mismatches.append(f"{method.upper()} {path}: {status}")
    assert not mismatches


def test_documented_management_and_health_responses_are_runtime_validated() -> None:
    router_files = (
        "src/gateway/presentation/routers/health.py",
        "src/gateway/presentation/routers/management.py",
        "src/gateway/presentation/routers/management_auth.py",
    )
    bypasses = [
        path
        for path in router_files
        if "JSONResponse" in Path(path).read_text(encoding="utf-8")
    ]
    assert not bypasses


def test_ai_tool_responses_are_typed_by_execution_shape() -> None:
    schema = create_app().openapi()
    response_schema = schema["paths"]["/api/v1/ai-tools/{tool_name}"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    variants = response_schema["anyOf"]
    assert len(variants) == 9
    assert {variant["$ref"] for variant in variants} == {
        "#/components/schemas/AIConfirmationRequired",
        "#/components/schemas/AIIngestionJobsExecution",
        "#/components/schemas/AIKnowledgeExecution",
        "#/components/schemas/AIRetrievalExecution",
        "#/components/schemas/AISettingsExecution",
        "#/components/schemas/AISourcesExecution",
        "#/components/schemas/AISpaceCreateExecution",
        "#/components/schemas/AISpaceListExecution",
        "#/components/schemas/AISpaceMembersExecution",
    }


def test_management_validation_errors_include_safe_field_details() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/api/v1/probe")
    async def probe(payload: ValidationProbe) -> None:
        return None

    response = TestClient(app).post("/api/v1/probe", json={"limit": "do-not-echo"})
    payload = response.json()
    validated = ErrorEnvelope.model_validate(payload)

    assert response.status_code == 422
    assert validated.error.code == "invalid_payload"
    assert validated.error.details is not None
    assert [field.model_dump() for field in validated.error.details.fields] == [
        {"field": "body.limit", "code": "int_parsing"}
    ]
    assert "do-not-echo" not in response.text


def test_management_conflicts_include_allowlisted_structured_details() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/v1/conflict")
    async def conflict() -> None:
        raise ConcurrencyConflictException("item-1", expected_version=3, actual_version=4)

    response = TestClient(app).get("/api/v1/conflict")
    validated = ErrorEnvelope.model_validate(response.json())

    assert response.status_code == 409
    assert validated.error.code == "version_mismatch"
    assert validated.error.details is not None
    assert validated.error.details.item_id == "item-1"
    assert validated.error.details.expected_version == 3
    assert validated.error.details.actual_version == 4
