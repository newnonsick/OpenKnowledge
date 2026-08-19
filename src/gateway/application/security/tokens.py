from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets


class SecretValue:
    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("Secret value cannot be empty")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


class OpaqueTokenCodec:
    def issue(self, entropy_bytes: int = 32) -> SecretValue:
        return SecretValue(secrets.token_urlsafe(entropy_bytes))

    def digest(self, token: SecretValue | str) -> str:
        raw = token.reveal() if isinstance(token, SecretValue) else token
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify(self, candidate: str, expected_digest: str) -> bool:
        return hmac.compare_digest(self.digest(candidate), expected_digest)


@dataclass(frozen=True, slots=True)
class ParsedAPIKey:
    public_id: str
    secret_part: str


@dataclass(frozen=True, slots=True)
class IssuedAPIKey:
    public_id: str
    secret: SecretValue
    digest: str


class APIKeyCodec:
    def __init__(self, pepper: SecretValue) -> None:
        self._pepper = pepper.reveal().encode("utf-8")

    def issue(self) -> IssuedAPIKey:
        public_id = secrets.token_hex(8)
        secret_part = secrets.token_urlsafe(32)
        raw = f"aigw_v1_{public_id}_{secret_part}"
        return IssuedAPIKey(public_id, SecretValue(raw), self._digest_parts(public_id, secret_part))

    def parse(self, raw: str) -> ParsedAPIKey:
        parts = raw.split("_", 3)
        if len(parts) != 4 or parts[0] != "aigw" or parts[1] != "v1" or not parts[2] or not parts[3]:
            raise ValueError("Invalid API key format")
        return ParsedAPIKey(parts[2], parts[3])

    def digest(self, raw: str) -> str:
        parsed = self.parse(raw)
        return self._digest_parts(parsed.public_id, parsed.secret_part)

    def verify(self, raw: str, expected_digest: str) -> bool:
        try:
            actual = self.digest(raw)
        except ValueError:
            actual = hmac.new(self._pepper, b"invalid", hashlib.sha256).hexdigest()
        return hmac.compare_digest(actual, expected_digest)

    def _digest_parts(self, public_id: str, secret_part: str) -> str:
        payload = f"v1:{public_id}:{secret_part}".encode("utf-8")
        return hmac.new(self._pepper, payload, hashlib.sha256).hexdigest()
