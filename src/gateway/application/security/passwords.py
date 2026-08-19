from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


def _default_common_passwords() -> frozenset[str]:
    path = files("src.gateway.application.security").joinpath("common_passwords.txt")
    return frozenset(line.strip().casefold() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def normalize_password(password: str) -> str:
    return unicodedata.normalize("NFC", password)


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    min_length: int = 15
    max_length: int = 128
    common_passwords: frozenset[str] | set[str] = field(default_factory=_default_common_passwords)

    def validate(self, password: str, *, username: str | None = None) -> None:
        normalized = normalize_password(password)
        folded = normalized.casefold()
        if len(normalized) < self.min_length:
            raise ValueError(f"Password must contain at least {self.min_length} characters")
        if len(normalized) > self.max_length:
            raise ValueError(f"Password must contain at most {self.max_length} characters")
        if folded in self.common_passwords:
            raise ValueError("Password is commonly used")
        if username:
            username_folded = unicodedata.normalize("NFC", username.strip()).casefold()
            if len(username_folded) >= 3 and username_folded in folded:
                raise ValueError("Password must not contain the username")
        if "ai knowledge gateway" in folded or "knowledgegateway" in folded:
            raise ValueError("Password must not contain the product name")


class PasswordService:
    def __init__(
        self,
        *,
        memory_cost: int = 65536,
        time_cost: int = 3,
        parallelism: int = 4,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self._hasher = PasswordHasher(
            memory_cost=memory_cost,
            time_cost=time_cost,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._policy = policy or PasswordPolicy()

    def hash(self, password: str, *, username: str | None = None) -> str:
        normalized = normalize_password(password)
        self._policy.validate(normalized, username=username)
        return self._hasher.hash(normalized)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, normalize_password(password))
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True
