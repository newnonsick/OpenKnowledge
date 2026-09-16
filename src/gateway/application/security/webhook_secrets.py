from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from src.gateway.application.security.tokens import SecretValue


class WebhookSecretService:
    def __init__(
        self,
        encryption_keys: SecretValue | dict[int, SecretValue],
        *,
        active_key_version: int = 1,
    ) -> None:
        keys = {1: encryption_keys} if isinstance(encryption_keys, SecretValue) else encryption_keys
        if active_key_version not in keys:
            raise ValueError("Active webhook encryption key version is missing")
        self._fernets = {version: Fernet(value.reveal().encode("ascii")) for version, value in keys.items()}
        self._active_key_version = active_key_version

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    def encrypt_secret(self, secret: SecretValue, *, key_version: int | None = None) -> bytes:
        version = key_version or self._active_key_version
        return self._fernets[version].encrypt(secret.reveal().encode("ascii"))

    def decrypt_secret(self, encrypted: bytes, *, key_version: int = 1) -> SecretValue:
        try:
            return SecretValue(self._fernets[key_version].decrypt(encrypted).decode("ascii"))
        except (InvalidToken, KeyError) as exc:
            raise ValueError("Invalid encrypted webhook secret") from exc
