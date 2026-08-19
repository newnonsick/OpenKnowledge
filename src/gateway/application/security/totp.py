from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
import pyotp

from src.gateway.application.security.tokens import SecretValue


class MFASecretService:
    def __init__(self, encryption_key: SecretValue) -> None:
        self._key = encryption_key.reveal().encode("ascii")
        self._fernet = Fernet(self._key)

    @classmethod
    def generate(cls) -> "MFASecretService":
        return cls(SecretValue(Fernet.generate_key().decode("ascii")))

    @staticmethod
    def generate_encryption_key() -> SecretValue:
        return SecretValue(Fernet.generate_key().decode("ascii"))

    def new_totp_secret(self) -> SecretValue:
        return SecretValue(pyotp.random_base32(length=32))

    def encrypt_secret(self, secret: SecretValue) -> bytes:
        return self._fernet.encrypt(secret.reveal().encode("ascii"))

    def decrypt_secret(self, encrypted: bytes) -> SecretValue:
        try:
            return SecretValue(self._fernet.decrypt(encrypted).decode("ascii"))
        except InvalidToken as exc:
            raise ValueError("Invalid encrypted MFA secret") from exc

    def verify_totp(self, encrypted: bytes, code: str, valid_window: int = 1) -> bool:
        secret = self.decrypt_secret(encrypted).reveal()
        return bool(pyotp.TOTP(secret).verify(code, valid_window=valid_window))

    def new_recovery_code(self) -> SecretValue:
        return SecretValue("-".join((secrets.token_hex(4), secrets.token_hex(4))))

    def hash_recovery_code(self, code: str) -> str:
        key = hashlib.sha256(base64.urlsafe_b64decode(self._key)).digest()
        return hmac.new(key, code.casefold().encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_recovery_code(self, code: str, expected_digest: str) -> bool:
        return hmac.compare_digest(self.hash_recovery_code(code), expected_digest)
