from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from src.gateway.config import (
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    get_settings,
    LLMSettings,
    RuntimeEnvironment,
    Settings,
)
from src.gateway.main import create_app


def production_settings(**gateway_overrides) -> Settings:
    gateway = {
        "environment": RuntimeEnvironment.PRODUCTION,
        "host": "0.0.0.0",
        "log_level": "INFO",
        "public_base_url": "https://gateway.example.com",
        "trusted_hosts": ["gateway.example.com"],
        "cors_origins": ["https://gateway.example.com"],
        "trusted_proxy_cidrs": [],
        "api_keys": [],
        "legacy_api_keys_enabled": False,
        "api_key_peppers": {1: "p" * 32},
        "active_api_key_pepper_version": 1,
        "mfa_encryption_keys": {1: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        "active_mfa_encryption_key_version": 1,
    }
    gateway.update(gateway_overrides)
    return Settings(gateway=gateway, database={"echo": False})


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"cors_origins": ["*"]}, "cors_origins"),
        ({"public_base_url": "http://gateway.example.com"}, "public_base_url"),
        ({"trusted_hosts": []}, "trusted_hosts"),
        ({"trusted_hosts": ["*"]}, "trusted_hosts"),
        ({"log_level": "DEBUG"}, "log_level"),
        ({"api_keys": ["sk-gateway-default-key"]}, "api_keys"),
        ({"api_keys": ["sentinel-production-key"]}, "api_keys"),
        ({"legacy_api_keys_enabled": True}, "legacy_api_keys_enabled"),
        ({"api_key_peppers": {}}, "api_key_peppers"),
        ({"api_key_peppers": {1: "short"}}, "api_key_peppers"),
        ({"active_api_key_pepper_version": 2}, "active_api_key_pepper_version"),
        ({"mfa_encryption_keys": {}}, "mfa_encryption_keys"),
        ({"mfa_encryption_keys": {1: "invalid"}}, "mfa_encryption_keys"),
        ({"active_mfa_encryption_key_version": 2}, "active_mfa_encryption_key_version"),
    ],
)
def test_production_rejects_unsafe_gateway_configuration(overrides, field_name):
    settings = production_settings(**overrides)

    with pytest.raises(ValidationError) as exc_info:
        settings.validate_runtime_safety()

    assert any(error["loc"][-1] == field_name for error in exc_info.value.errors())
    assert "sentinel-production-key" not in str(exc_info.value)


def test_production_rejects_database_echo():
    settings = production_settings()
    settings.database = DatabaseSettings(echo=True)

    with pytest.raises(ValidationError) as exc_info:
        settings.validate_runtime_safety()

    assert any(error["loc"] == ("database", "echo") for error in exc_info.value.errors())


def test_valid_production_profile_composes_application():
    settings = production_settings()

    settings.validate_runtime_safety()
    app = create_app(settings)

    assert app.state.settings is settings
    assert "/api/v1/auth/login" in app.openapi()["paths"]
    assert "/api/v1/auth/refresh" in app.openapi()["paths"]
    assert "/api/v1/me" in app.openapi()["paths"]
    assert "/api/v1/spaces" in app.openapi()["paths"]
    assert "/api/v1/members" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_injected_settings_are_active_through_request_dependencies():
    custom_database_url = "sqlite+aiosqlite:///:memory:"
    settings = Settings(
        gateway={
            "environment": "test",
            "api_keys": ["context-key"],
            "legacy_api_keys_enabled": True,
            "trusted_hosts": ["testserver"],
        },
        database={"url": custom_database_url},
    )
    app = create_app(settings)

    @app.get("/settings-probe")
    async def settings_probe():
        return {"database_url": get_settings().database.url}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/settings-probe",
            headers={"Authorization": "Bearer context-key"},
        )

    assert response.status_code == 200
    assert response.json()["database_url"] == custom_database_url


def test_development_defaults_are_loopback_safe_without_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings()

    settings.validate_runtime_safety()

    assert settings.gateway.environment is RuntimeEnvironment.DEVELOPMENT
    assert settings.gateway.host == "127.0.0.1"
    assert settings.gateway.gateway_api_keys == []
    assert "*" not in settings.gateway.cors_origins


def test_secret_values_are_redacted_from_settings_repr():
    sentinel = "sentinel-secret-value"
    values = [
        LLMSettings(api_key=sentinel),
        EmbeddingSettings(api_key=sentinel),
        DatabaseSettings(url=f"postgresql+asyncpg://user:{sentinel}@db/gateway"),
        DatabaseSettings(migration_url=f"postgresql+asyncpg://migrator:{sentinel}@db/gateway"),
        GatewaySettings(api_keys=[sentinel]),
        GatewaySettings(api_key_peppers={1: sentinel}),
        GatewaySettings(mfa_encryption_keys={1: sentinel}),
    ]

    assert all(sentinel not in repr(value) for value in values)


def test_invalid_proxy_cidr_is_rejected():
    with pytest.raises(ValidationError):
        GatewaySettings(trusted_proxy_cidrs=["not-a-network"])
