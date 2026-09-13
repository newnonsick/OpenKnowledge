from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from src.gateway.application.services import oidc_service
from src.gateway.application.services.oidc_service import (
    OidcTokens,
    authenticate_callback,
    authenticate_with_tokens,
    build_login_url,
    discover,
    exchange_code,
    extract_groups,
    fetch_jwks,
    map_space_grants,
    map_system_role,
    resolve_username,
    verify_id_token,
)
from src.gateway.config import Settings, reset_runtime_settings, set_runtime_settings
from src.gateway.domain.exceptions import AuthenticationException, ValidationException
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SpaceMembershipModel


ISSUER = "https://idp.example.test"
CLIENT_ID = "gateway-client"
JWK_KID = "test-key-1"


@pytest.fixture()
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def jwks(rsa_key):
    public = rsa_key.public_key()
    numbers = public.public_numbers()
    raw_n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    raw_e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    encoded_n = base64.urlsafe_b64encode(raw_n).rstrip(b"=").decode()
    encoded_e = base64.urlsafe_b64encode(raw_e).rstrip(b"=").decode()
    return {
        JWK_KID: {
            "kty": "RSA",
            "kid": JWK_KID,
            "use": "sig",
            "alg": "RS256",
            "n": encoded_n,
            "e": encoded_e,
        }
    }


def _id_token(rsa_key, *, issuer=ISSUER, audience=CLIENT_ID, subject="oidc-sub-1", extra=None, key_id=JWK_KID, lifetime=600):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"iss": issuer, "aud": audience, "sub": subject, "iat": now, "exp": now + lifetime}
    payload.update(extra or {})
    private_pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    headers = {"kid": key_id} if key_id else None
    return jwt.encode(payload, private_pem, algorithm="RS256", headers=headers)


@pytest.fixture()
def idp_transport(rsa_key, jwks):
    token_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": list(jwks.values())})
        if request.url.path == "/token":
            token_calls.append(request)
            raw = _id_token(rsa_key, extra={"email": "sso.user@example.test", "groups": ["team-a"]})
            return httpx.Response(200, json={"id_token": raw, "token_type": "Bearer", "expires_in": 3600})
        return httpx.Response(404, json={"error": "not_found"})

    transport = httpx.MockTransport(handler)
    transport.token_calls = token_calls
    return transport


@pytest.fixture()
def gateway_settings():
    settings = Settings(
        gateway={
            "environment": "test",
            "oidc_enabled": True,
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": "test-client-secret-with-length",
            "oidc_redirect_url": "https://gateway.test/api/v1/auth/oidc/callback",
            "oidc_group_claim": "groups",
            "oidc_username_claim": "email",
            "oidc_role_mapping": {"sso-admins": "super_admin"},
            "oidc_group_space_map": {"team-a": [{"space_id": "space-a", "role": "editor"}]},
            "oidc_deprovision_disable": True,
        }
    )
    token = set_runtime_settings(settings)
    oidc_service.clear_jwks_cache()
    try:
        yield settings
    finally:
        reset_runtime_settings(token)
        oidc_service.clear_jwks_cache()


class _ScalarResult:
    def __init__(self, value):
        self._value = value


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)

    def __iter__(self):
        return iter(self._values)


class FakeSession:
    def __init__(self):
        self.members: dict[str, MemberModel] = {}
        self.memberships: list[SpaceMembershipModel] = []
        self.families: dict[str, object] = {}
        self.events: list[AuditEventModel] = []
        self.executed: list[object] = []
        self.workspaces: set[str] = {"space-a", "space-b"}

    def add(self, record):
        if isinstance(record, MemberModel):
            self.members[str(record.id)] = record
        elif isinstance(record, SpaceMembershipModel):
            self.memberships.append(record)
        elif isinstance(record, AuditEventModel):
            self.events.append(record)
        else:
            family_id = getattr(record, "id", None)
            if family_id is not None:
                self.families[str(family_id)] = record

    async def flush(self):
        return None

    async def scalar(self, query):
        try:
            compiled_query = query.compile()
            compiled = str(compiled_query)
            params = dict(compiled_query.params)
        except Exception:
            compiled = str(query)
            params = {}
        if "WHERE members.oidc_issuer" in compiled and "members.oidc_subject" in compiled:
            issuer = next(
                (str(value) for name, value in params.items() if name.startswith("oidc_issuer")),
                None,
            )
            subject = next(
                (str(value) for name, value in params.items() if name.startswith("oidc_subject")),
                None,
            )
            for member in self.members.values():
                if issuer is not None and subject is not None:
                    if member.oidc_issuer == issuer and member.oidc_subject == subject:
                        return member
                elif member.oidc_subject == subject or member.oidc_issuer == issuer:
                    return member
            return None
        if "WHERE members.username_normalized" in compiled:
            wanted = next(
                (str(value) for name, value in params.items() if name.startswith("username_normalized")),
                None,
            )
            for member in self.members.values():
                if wanted is None or member.username_normalized == wanted:
                    return member
            return None
        if "space_memberships" in compiled and "member_id" in compiled:
            for row in self.memberships:
                return row.id
            return None
        return None

    async def scalars(self, query):
        compiled = str(query)
        if "workspaces" in compiled:
            return _ScalarsResult(sorted(self.workspaces))
        if "space_memberships" in compiled:
            return _ScalarsResult(list(self.memberships))
        if "session_families" in compiled:
            return _ScalarsResult([])
        return _ScalarsResult([])

    async def get(self, model, key):
        if model is MemberModel:
            return self.members.get(str(key))
        return self.families.get(str(key))

    async def delete(self, record):
        if isinstance(record, SpaceMembershipModel):
            self.memberships = [row for row in self.memberships if row is not record]
        elif isinstance(record, MemberModel):
            self.members.pop(str(record.id), None)

    async def execute(self, statement):
        self.executed.append(statement)
        return None


@pytest.fixture()
def sqlite_factory():
    class _Factory:
        def begin(self):
            return self

        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *args):
            return False

        def __call__(self):
            return FakeSession()

    return _Factory()


@pytest.fixture()
def fake_session():
    return FakeSession()


def test_login_url_uses_pkce_s256() -> None:
    from src.gateway.application.services.oidc_service import OidcEndpoints

    endpoints = OidcEndpoints(
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/jwks",
        issuer=ISSUER,
    )
    url = build_login_url(
        endpoints,
        client_id=CLIENT_ID,
        redirect_url="https://gateway.test/api/v1/auth/oidc/callback",
        state="state-value",
        code_verifier="verifier-value",
        nonce="nonce-value",
    )
    assert url.startswith(f"{ISSUER}/authorize?")
    assert "code_challenge_method=S256" in url
    assert "state=state-value" in url


@pytest.mark.asyncio
async def test_discovery_and_code_exchange_use_stubbed_idp(idp_transport, gateway_settings) -> None:
    async with httpx.AsyncClient(transport=idp_transport, base_url="https://idp.example.test") as client:
        endpoints = await discover(ISSUER, client=client)
        assert endpoints.token_endpoint == f"{ISSUER}/token"
        jwks = await fetch_jwks(endpoints.jwks_uri, client=client)
        assert JWK_KID in jwks
        exchanged = await exchange_code(
            endpoints.token_endpoint,
            code="auth-code",
            redirect_url="https://gateway.test/api/v1/auth/oidc/callback",
            client_id=CLIENT_ID,
            client_secret="test-client-secret-with-length",
            code_verifier="verifier",
            client=client,
        )
    assert exchanged["id_token"]
    assert len(idp_transport.token_calls) == 1


@pytest.mark.asyncio
async def test_id_token_verification_rejects_bad_signature(rsa_key, jwks, gateway_settings) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _id_token(other, extra={"email": "intruder@example.test"})
    with pytest.raises(AuthenticationException):
        verify_id_token(forged, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID)
    expired = _id_token(rsa_key, extra={"email": "old@example.test"}, lifetime=-60)
    with pytest.raises(AuthenticationException):
        verify_id_token(expired, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID)
    wrong_audience = _id_token(rsa_key, audience="someone-else", extra={"email": "x@example.test"})
    with pytest.raises(AuthenticationException):
        verify_id_token(wrong_audience, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID)


@pytest.mark.asyncio
async def test_callback_jit_provisions_member_with_role_and_space_grants(rsa_key, jwks, gateway_settings, fake_session) -> None:
    oidc_service.clear_jwks_cache()
    raw = _id_token(
        rsa_key,
        subject="provisioned-sub-1",
        extra={"email": "Provisioned.User@Example.test", "email_verified": True, "groups": ["team-a", "sso-admins"]},
    )
    tokens, result = await authenticate_callback(
        fake_session, id_token=raw, jwks=jwks, request_id="req-1"
    )
    assert result.created is True
    assert result.system_role == "super_admin"
    assert tokens.username == "Provisioned.User@Example.test"
    assert ("space-a", "editor") in result.granted_memberships
    member = await fake_session.get(MemberModel, result.member.id)
    assert member is not None
    assert member.system_role == "super_admin"
    assert member.force_password_change is False
    assert member.oidc_subject == "provisioned-sub-1"
    assert member.oidc_issuer == ISSUER
    assert [(row.space_id, row.role) for row in fake_session.memberships] == [("space-a", "editor")]
    assert {event.action for event in fake_session.events} == {"oidc.login"}


@pytest.mark.asyncio
async def test_verified_email_required_for_new_member(rsa_key, jwks, gateway_settings, fake_session) -> None:
    raw = _id_token(
        rsa_key,
        subject="unverified-sub",
        extra={"email": "unverified@example.test", "groups": ["team-a"]},
    )
    with pytest.raises(AuthenticationException):
        await authenticate_callback(fake_session, id_token=raw, jwks=jwks, request_id="req-unverified")
    explicit_false = _id_token(
        rsa_key,
        subject="false-sub",
        extra={"email": "false@example.test", "email_verified": False, "groups": ["team-a"]},
    )
    with pytest.raises(AuthenticationException):
        await authenticate_callback(fake_session, id_token=explicit_false, jwks=jwks, request_id="req-false")
    settings = Settings(
        gateway={
            "environment": "test",
            "oidc_enabled": True,
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": "test-client-secret-with-length",
            "oidc_redirect_url": "https://gateway.test/api/v1/auth/oidc/callback",
            "oidc_group_claim": "groups",
            "oidc_username_claim": "preferred_username",
            "oidc_role_mapping": {"sso-admins": "super_admin"},
            "oidc_group_space_map": {"team-a": [{"space_id": "space-a", "role": "editor"}]},
            "oidc_deprovision_disable": True,
        }
    )
    token = set_runtime_settings(settings)
    try:
        forged_email = _id_token(
            rsa_key,
            subject="mismatch-sub",
            extra={
                "preferred_username": "sso-user",
                "email": "other@example.test",
                "email_verified": True,
                "groups": ["team-a"],
            },
        )
        with pytest.raises(AuthenticationException):
            await authenticate_callback(
                fake_session,
                id_token=forged_email,
                jwks=jwks,
                request_id="req-mismatch",
            )
    finally:
        reset_runtime_settings(token)


@pytest.mark.asyncio
async def test_cross_subject_takeover_denied(rsa_key, jwks, gateway_settings, fake_session) -> None:
    first = _id_token(
        rsa_key,
        subject="owner-sub",
        extra={"email": "owner@example.test", "email_verified": True, "groups": ["team-a"]},
    )
    _, created = await authenticate_callback(fake_session, id_token=first, jwks=jwks, request_id="req-1")
    assert created.created is True
    second = _id_token(
        rsa_key,
        subject="attacker-sub",
        extra={"email": "owner@example.test", "email_verified": True, "groups": ["team-a"]},
    )
    with pytest.raises(AuthenticationException):
        await authenticate_callback(fake_session, id_token=second, jwks=jwks, request_id="req-2")


@pytest.mark.asyncio
async def test_local_account_requires_verified_link(rsa_key, jwks, gateway_settings, fake_session) -> None:
    from src.gateway.domain.identity import MemberStatus, normalize_username

    member = MemberModel(
        id=uuid4(),
        username="legacy@example.test",
        username_normalized=normalize_username("legacy@example.test"),
        display_name="legacy@example.test",
        status=MemberStatus.ACTIVE.value,
        system_role="member",
        force_password_change=False,
    )
    fake_session.add(member)
    unverified = _id_token(
        rsa_key,
        subject="legacy-sub",
        extra={"email": "legacy@example.test", "groups": ["team-a"]},
    )
    with pytest.raises(AuthenticationException):
        await authenticate_callback(fake_session, id_token=unverified, jwks=jwks, request_id="req-link-bad")
    assert member.oidc_subject is None
    verified = _id_token(
        rsa_key,
        subject="legacy-sub",
        extra={"email": "legacy@example.test", "email_verified": True, "groups": ["team-a"]},
    )
    _, linked = await authenticate_callback(fake_session, id_token=verified, jwks=jwks, request_id="req-link-good")
    assert linked.created is False
    assert member.oidc_subject == "legacy-sub"
    assert member.oidc_issuer == ISSUER


@pytest.mark.asyncio
async def test_group_removal_deprovisions_member_without_deleting(rsa_key, jwks, gateway_settings, fake_session) -> None:
    first = _id_token(rsa_key, subject="leaver-sub", extra={"email": "leaver@example.test", "email_verified": True, "groups": ["team-a"]})
    _, created_result = await authenticate_callback(
        fake_session, id_token=first, jwks=jwks, request_id="req-1"
    )
    member_id = created_result.member.id
    assert created_result.granted_memberships == (("space-a", "editor"),)
    second = _id_token(rsa_key, subject="leaver-sub", extra={"email": "leaver@example.test", "email_verified": True, "groups": []})
    _, removed_result = await authenticate_callback(
        fake_session, id_token=second, jwks=jwks, request_id="req-2"
    )
    assert removed_result.created is False
    assert removed_result.removed_memberships == (("space-a", "editor"),)
    assert removed_result.disabled is True
    member = await fake_session.get(MemberModel, member_id)
    assert member is not None
    assert member.status == "disabled"
    assert fake_session.memberships == []
    actions = [event.action for event in fake_session.events]
    assert "oidc.login" in actions
    assert "member.deprovisioned" in actions


@pytest.mark.asyncio
async def test_role_sync_downgrades_and_upgrades_on_group_change(rsa_key, jwks, gateway_settings, fake_session) -> None:
    admin_token = _id_token(rsa_key, subject="sync-sub", extra={"email": "sync@example.test", "email_verified": True, "groups": ["sso-admins"]})
    tokens = OidcTokens(
        id_token=admin_token,
        claims={"email": "sync@example.test", "email_verified": True},
        subject="sync-sub",
        username="sync@example.test",
        groups=("sso-admins",),
    )
    first = await authenticate_with_tokens(fake_session, tokens, request_id="req-1")
    assert first.system_role == "super_admin"
    downgraded = await authenticate_with_tokens(
        fake_session,
        OidcTokens(
            id_token=admin_token,
            claims={"email": "sync@example.test", "email_verified": True},
            subject="sync-sub",
            username="sync@example.test",
            groups=(),
        ),
        request_id="req-2",
    )
    assert downgraded.system_role == "member"
    member = await fake_session.get(MemberModel, first.member.id)
    assert member is not None
    assert member.system_role == "member"


@pytest.mark.asyncio
async def test_unknown_space_grant_rejected(rsa_key, jwks, gateway_settings, fake_session) -> None:
    settings = Settings(
        gateway={
            "environment": "test",
            "oidc_enabled": True,
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": "test-client-secret-with-length",
            "oidc_redirect_url": "https://gateway.test/api/v1/auth/oidc/callback",
            "oidc_group_claim": "groups",
            "oidc_username_claim": "email",
            "oidc_role_mapping": {},
            "oidc_group_space_map": {"team-a": [{"space_id": "space-ghost", "role": "editor"}]},
            "oidc_deprovision_disable": True,
        }
    )
    token = set_runtime_settings(settings)
    try:
        raw = _id_token(
            rsa_key,
            subject="ghost-sub",
            extra={"email": "ghost@example.test", "email_verified": True, "groups": ["team-a"]},
        )
        with pytest.raises(ValidationException, match="space-ghost"):
            await authenticate_callback(fake_session, id_token=raw, jwks=jwks, request_id="req-ghost")
    finally:
        reset_runtime_settings(token)


@pytest.mark.asyncio
async def test_nonce_mismatch_rejected(rsa_key, jwks, gateway_settings) -> None:
    raw = _id_token(
        rsa_key,
        subject="nonce-sub",
        extra={"email": "nonce@example.test", "email_verified": True, "nonce": "wrong-nonce"},
    )
    with pytest.raises(AuthenticationException):
        verify_id_token(raw, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID, expected_nonce="expected-nonce")
    good = _id_token(
        rsa_key,
        subject="nonce-sub",
        extra={"email": "nonce@example.test", "email_verified": True, "nonce": "expected-nonce"},
    )
    claims = verify_id_token(good, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID, expected_nonce="expected-nonce")
    assert claims["nonce"] == "expected-nonce"


@pytest.mark.asyncio
async def test_issuer_pinning_rejects_foreign_issuer(rsa_key, jwks, gateway_settings) -> None:
    foreign = _id_token(
        rsa_key,
        issuer="https://evil.example.test",
        subject="nonce-sub",
        extra={"email": "nonce@example.test", "email_verified": True},
    )
    with pytest.raises(AuthenticationException):
        verify_id_token(foreign, jwks=jwks, issuer=ISSUER, audience=CLIENT_ID)
    with pytest.raises(ValidationException):
        await discover("http://idp.example.test")
    with pytest.raises(ValidationException):
        await discover("http://idp.example.test:8080")


@pytest.mark.asyncio
async def test_discovery_rejects_http_endpoints(idp_transport, gateway_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "http://idp.example.test/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        return httpx.Response(404, json={"error": "not_found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ISSUER) as client:
        with pytest.raises(ValidationException):
            await discover(ISSUER, client=client)


@pytest.mark.asyncio
async def test_jwks_unknown_kid_refetches_once(rsa_key, gateway_settings) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = other.public_key().public_numbers()
    raw_n = public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")
    raw_e = public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")
    rotated = {
        "rotated-kid": {
            "kty": "RSA",
            "kid": "rotated-kid",
            "use": "sig",
            "alg": "RS256",
            "n": base64.urlsafe_b64encode(raw_n).rstrip(b"=").decode(),
            "e": base64.urlsafe_b64encode(raw_e).rstrip(b"=").decode(),
        }
    }
    raw = _id_token(rsa_key, key_id="rotated-kid", extra={"email": "rot@example.test"})
    with pytest.raises(AuthenticationException):
        verify_id_token(raw, jwks={}, issuer=ISSUER, audience=CLIENT_ID)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": list(rotated.values())})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ISSUER) as client:
        refreshed = await oidc_service.refresh_jwks(f"{ISSUER}/jwks", client=client)
        assert "rotated-kid" in refreshed
        assert len(calls) == 1


def test_malformed_oidc_mapping_json_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(gateway={"environment": "test", "oidc_role_mapping": "{not-json"})
    with pytest.raises(ValidationError):
        Settings(gateway={"environment": "test", "oidc_group_space_map": "{not-json"})


def test_group_extraction_and_username_resolution() -> None:
    assert extract_groups({"groups": ["a", "a", " b "]}, "groups") == ("a", "b")
    assert extract_groups({"teams": "solo"}, "teams") == ("solo",)
    assert extract_groups({}, "groups") == ()
    assert resolve_username({"email": "user@example.test"}, "email") == "user@example.test"
    assert resolve_username({"sub": "sub-1"}, "email") == "sub-1"
    with pytest.raises(AuthenticationException):
        resolve_username({}, "email")


def test_role_and_grant_mapping() -> None:
    assert map_system_role(("team-a", "sso-admins"), {"sso-admins": "super_admin"}) == "super_admin"
    assert map_system_role(("team-a",), {"sso-admins": "super_admin"}) == "member"
    assert map_space_grants(("team-a",), {"team-a": [{"space_id": "s", "role": "reader"}]}) == (("s", "reader"),)


class _OidcApp:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings


@pytest.fixture()
def oidc_app(gateway_settings, idp_transport):
    from src.gateway.infrastructure.database import get_db_session
    from src.gateway.main import create_app

    app = create_app(gateway_settings)

    async def _fake_session():
        yield FakeSession()

    app.dependency_overrides[get_db_session] = _fake_session
    return _OidcApp(app, gateway_settings)


def _signed_state_cookie(settings, payload):
    import base64 as _base64
    import hashlib as _hashlib
    import hmac as _hmac
    import json as _json
    import time as _time

    enriched = dict(payload)
    enriched.setdefault("expires_at", int(_time.time()) + 600)
    text = _json.dumps(enriched, separators=(",", ":"))
    signature = _hmac.new(
        settings.gateway.oidc_client_secret.encode(), text.encode(), _hashlib.sha256
    ).hexdigest()
    envelope = _json.dumps({"payload": text, "signature": signature}, separators=(",", ":"))
    return _base64.urlsafe_b64encode(envelope.encode()).decode()


@pytest.mark.asyncio
async def test_oidc_callback_rejects_state_mismatch(oidc_app) -> None:
    transport = httpx.ASGITransport(app=oidc_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        cookie = _signed_state_cookie(
            oidc_app.settings,
            {"state": "expected-state", "nonce": "nonce", "code_verifier": "verifier"},
        )
        response = await client.get(
            "/api/v1/auth/oidc/callback",
            params={"code": "auth-code", "state": "wrong-state"},
            cookies={"openknowledge-oidc-state": cookie},
        )
    assert response.status_code in {400, 422}


@pytest.mark.asyncio
async def test_oidc_callback_rejects_expired_state(oidc_app) -> None:
    transport = httpx.ASGITransport(app=oidc_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        cookie = _signed_state_cookie(
            oidc_app.settings,
            {"state": "expected-state", "nonce": "nonce", "code_verifier": "verifier", "expires_at": 1},
        )
        response = await client.get(
            "/api/v1/auth/oidc/callback",
            params={"code": "auth-code", "state": "expected-state"},
            cookies={"openknowledge-oidc-state": cookie},
        )
    assert response.status_code in {400, 422}


@pytest.mark.asyncio
async def test_oidc_callback_rejects_invalid_signature(oidc_app, rsa_key, jwks, monkeypatch) -> None:
    import src.gateway.presentation.routers.management_auth as auth_router

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _id_token(other, extra={"email": "intruder@example.test", "groups": ["team-a"]})
    endpoints = oidc_service.OidcEndpoints(
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/jwks",
        issuer=ISSUER,
    )

    async def fake_discover(issuer):
        assert issuer == ISSUER
        return endpoints

    async def fake_exchange(token_endpoint, **kwargs):
        assert token_endpoint == f"{ISSUER}/token"
        return {"id_token": forged, "token_type": "Bearer"}

    async def fake_jwks(uri):
        assert uri == f"{ISSUER}/jwks"
        return jwks

    monkeypatch.setattr(auth_router, "discover", fake_discover)
    monkeypatch.setattr(auth_router, "exchange_code", fake_exchange)
    monkeypatch.setattr(auth_router, "fetch_jwks", fake_jwks)
    transport = httpx.ASGITransport(app=oidc_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        cookie = _signed_state_cookie(
            oidc_app.settings,
            {"state": "expected-state", "nonce": "nonce", "code_verifier": "verifier"},
        )
        response = await client.get(
            "/api/v1/auth/oidc/callback",
            params={"code": "auth-code", "state": "expected-state"},
            cookies={"openknowledge-oidc-state": cookie},
        )
    assert response.status_code == 401
