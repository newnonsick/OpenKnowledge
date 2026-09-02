from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
import pyotp

from src.gateway.application.security.tokens import SecretValue

TOTP_ISSUER = "OpenKnowledge"


class MFASecretService:
    def __init__(
        self,
        encryption_keys: SecretValue | dict[int, SecretValue],
        *,
        active_key_version: int = 1,
    ) -> None:
        keys = {1: encryption_keys} if isinstance(encryption_keys, SecretValue) else encryption_keys
        if active_key_version not in keys:
            raise ValueError("Active MFA encryption key version is missing")
        self._keys = {version: value.reveal().encode("ascii") for version, value in keys.items()}
        self._fernets = {version: Fernet(value) for version, value in self._keys.items()}
        self._active_key_version = active_key_version

    @classmethod
    def generate(cls) -> "MFASecretService":
        return cls(SecretValue(Fernet.generate_key().decode("ascii")))

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    @staticmethod
    def generate_encryption_key() -> SecretValue:
        return SecretValue(Fernet.generate_key().decode("ascii"))

    def new_totp_secret(self) -> SecretValue:
        return SecretValue(pyotp.random_base32(length=32))

    def provisioning_uri(self, secret: SecretValue, account_name: str) -> str:
        return pyotp.TOTP(secret.reveal()).provisioning_uri(name=account_name, issuer_name=TOTP_ISSUER)

    def encrypt_secret(self, secret: SecretValue, *, key_version: int | None = None) -> bytes:
        version = key_version or self._active_key_version
        return self._fernet(version).encrypt(secret.reveal().encode("ascii"))

    def decrypt_secret(self, encrypted: bytes, *, key_version: int = 1) -> SecretValue:
        try:
            return SecretValue(self._fernet(key_version).decrypt(encrypted).decode("ascii"))
        except InvalidToken as exc:
            raise ValueError("Invalid encrypted MFA secret") from exc

    def verify_totp(self, encrypted: bytes, code: str, valid_window: int = 1, *, key_version: int = 1) -> bool:
        secret = self.decrypt_secret(encrypted, key_version=key_version).reveal()
        return bool(pyotp.TOTP(secret).verify(code, valid_window=valid_window))

    def new_recovery_code(self) -> SecretValue:
        return SecretValue("-".join((secrets.token_hex(4), secrets.token_hex(4))))

    def hash_recovery_code(self, code: str, *, key_version: int | None = None) -> str:
        version = key_version or self._active_key_version
        key = hashlib.sha256(base64.urlsafe_b64decode(self._key(version))).digest()
        return hmac.new(key, code.casefold().encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_recovery_code(self, code: str, expected_digest: str, *, key_version: int = 1) -> bool:
        return hmac.compare_digest(self.hash_recovery_code(code, key_version=key_version), expected_digest)

    def _key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError as exc:
            raise ValueError("MFA encryption key version is unavailable") from exc

    def _fernet(self, version: int) -> Fernet:
        try:
            return self._fernets[version]
        except KeyError as exc:
            raise ValueError("MFA encryption key version is unavailable") from exc
