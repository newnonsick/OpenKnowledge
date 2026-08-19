import json

import pytest

from src.gateway.application.security.passwords import PasswordPolicy, PasswordService, normalize_password
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


def test_password_policy_enforces_spec_maximum_and_identity_terms() -> None:
    policy = PasswordPolicy(common_passwords=set())
    with pytest.raises(ValueError, match="at most 128"):
        policy.validate("x" * 129)
    with pytest.raises(ValueError, match="username"):
        policy.validate("alice-is-my-password", username="Alice")
    with pytest.raises(ValueError, match="product"):
        policy.validate("ai knowledge gateway forever")


def test_passwords_are_normalized_with_nfc_before_hash_and_verify() -> None:
    composed = "pässword with enough length"
    decomposed = "pa\u0308ssword with enough length"
    assert normalize_password(decomposed) == composed
    service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    encoded = service.hash(decomposed)
    assert service.verify(encoded, composed) is True


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


def test_totp_keyring_preserves_old_factors_during_rotation() -> None:
    first_key = MFASecretService.generate_encryption_key()
    second_key = MFASecretService.generate_encryption_key()
    first = MFASecretService({1: first_key}, active_key_version=1)
    secret = first.new_totp_secret()
    encrypted = first.encrypt_secret(secret)
    recovery = first.new_recovery_code()
    recovery_digest = first.hash_recovery_code(recovery.reveal(), key_version=1)

    rotated = MFASecretService(
        {1: first_key, 2: second_key},
        active_key_version=2,
    )
    assert rotated.active_key_version == 2
    assert rotated.decrypt_secret(encrypted, key_version=1).reveal() == secret.reveal()
    assert rotated.verify_recovery_code(
        recovery.reveal(),
        recovery_digest,
        key_version=1,
    ) is True


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
