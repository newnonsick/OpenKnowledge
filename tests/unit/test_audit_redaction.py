from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.services.audit_service import sanitize_audit_details


def test_audit_details_recursively_remove_secret_bearing_fields() -> None:
    value = sanitize_audit_details(
        {
            "safe": "visible",
            "temporary_password": "never-persist",
            "nested": {
                "refresh_token": "never-persist",
                "items": [{"totp_secret": "never-persist"}, {"count": 2}],
            },
            "wrapped": SecretValue("never-persist"),
        }
    )
    assert value == {
        "safe": "visible",
        "nested": {"items": [{}, {"count": 2}]},
    }
