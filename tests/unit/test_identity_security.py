import json

import pytest

from src.gateway.application.security.passwords import PasswordPolicy, PasswordService
from src.gateway.application.security.tokens import APIKeyCodec, OpaqueTokenCodec, SecretValue
from src.gateway.application.security.totp import MFASecretService
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole, normalize_username


def test_username_normalization_uses_nfc_and_casefold() -> None:
    assert normalize_username("  A\u0308LICE  ") == "älice"


def test_password_policy_rejects_short_and_common_passwords() -> None:
    policy = PasswordPolicy(min_length=15, common_passwords={"correct horse battery staple"})
    with pytest.raises(ValueError, match="at least 15"):
        policy.validate("short password")
    with pytest.raises(ValueError, match="commonly used"):
        policy.validate("Correct Horse Battery Staple")


def test_argon2id_hashes_verify_and_detect_rehash() -> None:
    service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    encoded = service.hash("a sufficiently long password")
    assert encoded.startswith("$argon2id$")
    assert service.verify(encoded, "a sufficiently long password") is True
    assert service.verify(encoded, "not the password") is False
    stronger = PasswordService(memory_cost=16384, time_cost=2, parallelism=1)
    assert stronger.needs_rehash(encoded) is True


def test_opaque_and_api_key_tokens_store_only_digests() -> None:
    token_codec = OpaqueTokenCodec()
    token = token_codec.issue()
    digest = token_codec.digest(token)
    assert token.reveal() not in digest
    assert token_codec.verify(token.reveal(), digest) is True

    key_codec = APIKeyCodec(pepper=SecretValue("deployment-pepper"))
    issued = key_codec.issue()
    parsed = key_codec.parse(issued.secret.reveal())
    assert parsed.public_id == issued.public_id
    assert issued.secret.reveal().startswith("aigw_v1_")
    assert key_codec.verify(issued.secret.reveal(), issued.digest) is True
    assert key_codec.verify(issued.secret.reveal() + "x", issued.digest) is False


def test_totp_secrets_encrypt_and_recovery_codes_are_one_way() -> None:
    service = MFASecretService.generate()
    secret = service.new_totp_secret()
    encrypted = service.encrypt_secret(secret)
    assert secret.reveal().encode() not in encrypted
    assert service.decrypt_secret(encrypted).reveal() == secret.reveal()
    recovery = service.new_recovery_code()
    digest = service.hash_recovery_code(recovery.reveal())
    assert recovery.reveal() not in digest
    assert service.verify_recovery_code(recovery.reveal(), digest) is True


def test_secret_values_and_principals_are_redacted_and_non_serializable() -> None:
    secret = SecretValue("never-print-this")
    assert "never-print-this" not in repr(secret)
    with pytest.raises(TypeError):
        json.dumps(secret)

    principal = Principal(
        subject_id="member-1",
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )
    assert principal.subject_id == "member-1"
    with pytest.raises(Exception):
        principal.subject_id = "member-2"
