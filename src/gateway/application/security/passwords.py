from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


def _default_common_passwords() -> frozenset[str]:
    path = files("src.gateway.application.security").joinpath("common_passwords.txt")
    return frozenset(line.strip().casefold() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    min_length: int = 15
    max_length: int = 256
    common_passwords: frozenset[str] | set[str] = field(default_factory=_default_common_passwords)

    def validate(self, password: str) -> None:
        if len(password) < self.min_length:
            raise ValueError(f"Password must contain at least {self.min_length} characters")
        if len(password) > self.max_length:
            raise ValueError(f"Password must contain at most {self.max_length} characters")
        if password.casefold() in self.common_passwords:
            raise ValueError("Password is commonly used")


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

    def hash(self, password: str) -> str:
        self._policy.validate(password)
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True
