from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.config import get_settings


def configured_api_key_codec() -> APIKeyCodec:
    gateway = get_settings().gateway
    if not gateway.api_key_peppers:
        raise RuntimeError("API key peppers are unavailable")
    return APIKeyCodec(
        {
            version: SecretValue(value)
            for version, value in gateway.api_key_peppers.items()
        },
        active_pepper_version=gateway.active_api_key_pepper_version,
    )
