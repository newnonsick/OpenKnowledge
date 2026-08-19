import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def render_identity_sql(username: str, pepper: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost/gateway",
            "BOOTSTRAP_USERNAME": username,
            "API_KEY_PEPPER": pepper,
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_identity_schema_is_frozen_and_contains_no_environment_secrets() -> None:
    first = render_identity_sql("first-admin", "first-secret-pepper")
    second = render_identity_sql("second-admin", "second-secret-pepper")
    assert first == second
    assert "first-admin" not in first
    assert "first-secret-pepper" not in first
    for table in (
        "members",
        "password_credentials",
        "mfa_factors",
        "mfa_recovery_codes",
        "session_families",
        "session_credentials",
        "personal_api_keys",
        "api_key_scopes",
        "space_memberships",
        "audit_events",
        "idempotency_records",
        "compatibility_principals",
    ):
        assert f"CREATE TABLE {table}" in first


def test_identity_schema_never_declares_plaintext_secret_columns() -> None:
    sql = render_identity_sql("admin", "pepper")
    forbidden = (" plaintext_password ", " raw_token ", " api_key_secret ", " totp_secret ")
    assert all(value not in sql.lower() for value in forbidden)
    assert "password_hash" in sql.lower()
    assert "token_digest" in sql.lower()
    assert "secret_ciphertext" in sql.lower()
    assert "encryption_key_version" in sql.lower()
    assert "status in ('pending','active','disabled')" in sql.lower()
