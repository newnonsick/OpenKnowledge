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
    secret_part: SecretValue
    pepper_version: int


@dataclass(frozen=True, slots=True)
class IssuedAPIKey:
    public_id: str
    secret: SecretValue
    digest: str
    pepper_version: int


class APIKeyCodec:
    def __init__(
        self,
        pepper: SecretValue | dict[int, SecretValue],
        *,
        active_pepper_version: int = 1,
    ) -> None:
        peppers = {1: pepper} if isinstance(pepper, SecretValue) else pepper
        if active_pepper_version not in peppers:
            raise ValueError("Active API key pepper version is missing")
        self._peppers = {
            version: value.reveal().encode("utf-8")
            for version, value in peppers.items()
        }
        self._active_pepper_version = active_pepper_version

    def issue(self) -> IssuedAPIKey:
        public_id = secrets.token_hex(8)
        secret_part = secrets.token_urlsafe(32)
        version = self._active_pepper_version
        raw = f"aigw_v{version}_{public_id}_{secret_part}"
        return IssuedAPIKey(
            public_id,
            SecretValue(raw),
            self._digest_parts(public_id, secret_part, version),
            version,
        )

    def parse(self, raw: str) -> ParsedAPIKey:
        parts = raw.split("_", 3)
        if (
            len(parts) != 4
            or parts[0] != "aigw"
            or not parts[1].startswith("v")
            or not parts[1][1:].isdigit()
            or not parts[2]
            or not parts[3]
        ):
            raise ValueError("Invalid API key format")
        return ParsedAPIKey(parts[2], SecretValue(parts[3]), int(parts[1][1:]))

    def digest(self, raw: str) -> str:
        parsed = self.parse(raw)
        return self._digest_parts(
            parsed.public_id,
            parsed.secret_part.reveal(),
            parsed.pepper_version,
        )

    def verify(
        self,
        raw: str,
        expected_digest: str,
        *,
        pepper_version: int | None = None,
    ) -> bool:
        try:
            parsed = self.parse(raw)
            version = pepper_version or parsed.pepper_version
            if parsed.pepper_version != version:
                return False
            actual = self._digest_parts(parsed.public_id, parsed.secret_part.reveal(), version)
        except ValueError:
            pepper = self._peppers[self._active_pepper_version]
            actual = hmac.new(pepper, b"invalid", hashlib.sha256).hexdigest()
        return hmac.compare_digest(actual, expected_digest)

    def _digest_parts(self, public_id: str, secret_part: str, pepper_version: int) -> str:
        try:
            pepper = self._peppers[pepper_version]
        except KeyError as exc:
            raise ValueError("API key pepper version is unavailable") from exc
        payload = f"v{pepper_version}:{public_id}:{secret_part}".encode("utf-8")
        return hmac.new(pepper, payload, hashlib.sha256).hexdigest()
