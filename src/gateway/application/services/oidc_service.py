from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthenticationException, StorageException, ValidationException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole, normalize_username
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SessionCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass
class OidcEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str


@dataclass
class OidcTokens:
    id_token: str
    claims: dict[str, Any]
    subject: str
    username: str
    groups: tuple[str, ...]


@dataclass
class OidcProvisioningResult:
    member: MemberModel
    created: bool
    system_role: str
    granted_memberships: tuple[tuple[str, str], ...]
    removed_memberships: tuple[tuple[str, str], ...]
    disabled: bool


@dataclass
class _JwksCache:
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: float = 0.0


_JWKS_CACHE: dict[str, _JwksCache] = {}
_JWKS_TTL_SECONDS = 600.0


def _gateway() -> Any:
    return get_settings().gateway


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False)


def _is_localhost_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1"}


def _require_https_url(value: str, *, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationException(f"OIDC {field} is not configured.")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValidationException(f"OIDC {field} is not a valid URL.") from exc
    if parsed.scheme == "https" and parsed.hostname:
        return text
    if _is_localhost_url(text):
        return text
    raise ValidationException(f"OIDC {field} must use HTTPS.")


async def _fetch_jwks_document(jwks_uri: str, *, client: httpx.AsyncClient | None = None) -> dict[str, dict[str, Any]]:
    own = client is None
    active = client or _http_client()
    try:
        response = await active.get(jwks_uri)
    except httpx.HTTPError as exc:
        raise StorageException("OIDC JWKS fetch failed.") from exc
    finally:
        if own:
            await active.aclose()
    if response.status_code != 200:
        raise StorageException("OIDC JWKS fetch failed.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise StorageException("OIDC JWKS returned invalid JSON.") from exc
    keys: dict[str, dict[str, Any]] = {}
    entries = payload.get("keys")
    if not isinstance(entries, list):
        raise StorageException("OIDC JWKS returned invalid JSON.")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("kid") and entry.get("kty") == "RSA":
            keys[str(entry["kid"])] = entry
    if not keys:
        raise StorageException("OIDC JWKS contains no usable RSA keys.")
    return keys


async def discover(issuer: str, *, client: httpx.AsyncClient | None = None) -> OidcEndpoints:
    normalized = (issuer or "").rstrip("/")
    normalized = _require_https_url(normalized, field="issuer")
    own = client is None
    active = client or _http_client()
    try:
        response = await active.get(f"{normalized}/.well-known/openid-configuration")
    except httpx.HTTPError as exc:
        raise StorageException("OIDC discovery failed.") from exc
    finally:
        if own:
            await active.aclose()
    if response.status_code != 200:
        raise StorageException("OIDC discovery failed.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise StorageException("OIDC discovery returned invalid JSON.") from exc
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not payload.get(required):
            raise StorageException("OIDC discovery document is incomplete.")
    return OidcEndpoints(
        authorization_endpoint=_require_https_url(str(payload["authorization_endpoint"]), field="authorization endpoint"),
        token_endpoint=_require_https_url(str(payload["token_endpoint"]), field="token endpoint"),
        jwks_uri=_require_https_url(str(payload["jwks_uri"]), field="JWKS URI"),
        issuer=str(payload["issuer"]),
    )


async def fetch_jwks(jwks_uri: str, *, client: httpx.AsyncClient | None = None) -> dict[str, dict[str, Any]]:
    _require_https_url(jwks_uri, field="JWKS URI")
    if client is None:
        cached = _JWKS_CACHE.get(jwks_uri)
        now = time.monotonic()
        if cached is not None and now - cached.fetched_at < _JWKS_TTL_SECONDS:
            return dict(cached.keys)
        keys = await _fetch_jwks_document(jwks_uri)
        _JWKS_CACHE[jwks_uri] = _JwksCache(keys=keys, fetched_at=now)
        return dict(keys)
    return await _fetch_jwks_document(jwks_uri, client=client)


async def refresh_jwks(jwks_uri: str, *, client: httpx.AsyncClient | None = None) -> dict[str, dict[str, Any]]:
    _require_https_url(jwks_uri, field="JWKS URI")
    keys = await _fetch_jwks_document(jwks_uri, client=client)
    _JWKS_CACHE[jwks_uri] = _JwksCache(keys=keys, fetched_at=time.monotonic())
    return dict(keys)


def clear_jwks_cache() -> None:
    _JWKS_CACHE.clear()


def verification_key(jwks: dict[str, dict[str, Any]], kid: str | None):
    if not kid or kid not in jwks:
        raise AuthenticationException("OIDC token signature is not trusted.")
    try:
        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwks[kid]))
    except Exception as exc:
        raise AuthenticationException("OIDC token signature is not trusted.") from exc


def verify_id_token(
    raw_token: str,
    *,
    jwks: dict[str, dict[str, Any]],
    issuer: str,
    audience: str,
    expected_nonce: str | None = None,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.PyJWTError as exc:
        raise AuthenticationException("OIDC token is invalid.") from exc
    if header.get("alg") != "RS256":
        raise AuthenticationException("OIDC token signature is not trusted.")
    key = verification_key(jwks, header.get("kid"))
    try:
        claims = jwt.decode(
            raw_token,
            key=key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationException("OIDC token has expired.") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthenticationException("OIDC token issuer is not trusted.") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthenticationException("OIDC token audience is invalid.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationException("OIDC token signature is not trusted.") from exc
    token_nonce = claims.get("nonce")
    if expected_nonce is not None and token_nonce != expected_nonce:
        raise AuthenticationException("OIDC token nonce mismatch.")
    return claims


def extract_groups(claims: dict[str, Any], group_claim: str) -> tuple[str, ...]:
    raw = claims.get(group_claim, [])
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    groups: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in groups:
            groups.append(text)
    return tuple(groups)


def resolve_username(claims: dict[str, Any], username_claim: str) -> str:
    for candidate in (claims.get(username_claim), claims.get("email"), claims.get("preferred_username"), claims.get("sub")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:255]
    raise AuthenticationException("OIDC token carries no usable username.")


def map_system_role(groups: tuple[str, ...], role_mapping: dict[str, str]) -> str:
    for group in groups:
        mapped = (role_mapping or {}).get(group)
        if mapped == SystemRole.SUPER_ADMIN.value:
            return SystemRole.SUPER_ADMIN.value
    return SystemRole.MEMBER.value


def map_space_grants(groups: tuple[str, ...], group_space_map: dict[str, list[dict[str, str]]]) -> tuple[tuple[str, str], ...]:
    grants: list[tuple[str, str]] = []
    for group in groups:
        for grant in (group_space_map or {}).get(group, []):
            pair = (str(grant["space_id"]), str(grant["role"]))
            if pair not in grants:
                grants.append(pair)
    return tuple(grants)


def build_login_url(
    endpoints: OidcEndpoints,
    *,
    client_id: str,
    redirect_url: str,
    state: str,
    code_verifier: str,
    nonce: str,
) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_url,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{endpoints.authorization_endpoint}?{query}"


def new_state() -> str:
    return base64.urlsafe_b64encode(os.urandom(24)).decode()


def new_code_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")


async def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_url: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    own = client is None
    active = client or _http_client()
    try:
        try:
            response = await active.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_url,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise StorageException("OIDC code exchange failed.") from exc
    finally:
        if own:
            await active.aclose()
    if response.status_code != 200:
        raise AuthenticationException("OIDC code exchange failed.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthenticationException("OIDC code exchange failed.") from exc
    if not payload.get("id_token"):
        raise AuthenticationException("OIDC code exchange failed.")
    return dict(payload)


async def authenticate_with_tokens(
    session: AsyncSession,
    tokens: OidcTokens,
    *,
    request_id: str,
    now: datetime | None = None,
) -> OidcProvisioningResult:
    gateway = _gateway()
    current_time = now or datetime.now(timezone.utc)
    issuer = (gateway.oidc_issuer or "").rstrip("/")
    subject = str(tokens.subject or "")
    if not subject:
        raise AuthenticationException("OIDC token carries no subject.")
    member = await session.scalar(
        select(MemberModel)
        .where(MemberModel.oidc_issuer == issuer, MemberModel.oidc_subject == subject)
        .with_for_update()
    )
    normalized = normalize_username(tokens.username)
    if member is not None:
        created = False
    else:
        token_email = str(tokens.claims.get("email") or "").casefold()
        if not token_email or token_email != normalized or tokens.claims.get("email_verified") is not True:
            raise AuthenticationException("OIDC account email is not verified.")
        candidate = await session.scalar(
            select(MemberModel).where(MemberModel.username_normalized == normalized).with_for_update()
        )
        if candidate is None:
            member = MemberModel(
                id=uuid4(),
                username=tokens.username,
                username_normalized=normalized,
                display_name=tokens.username,
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
                oidc_subject=subject,
                oidc_issuer=issuer,
            )
            session.add(member)
            await session.flush()
            created = True
        else:
            member = candidate
            if member.oidc_subject is not None:
                if member.oidc_subject != subject or (member.oidc_issuer or "") != issuer:
                    raise AuthenticationException("OIDC account exists with different credentials.")
            else:
                member.oidc_subject = subject
                member.oidc_issuer = issuer
            created = False
    if member.status == MemberStatus.DISABLED.value:
        raise AuthenticationException("OIDC account is disabled.")
    system_role = map_system_role(tokens.groups, dict(gateway.oidc_role_mapping or {}))
    if member.system_role != system_role:
        member.system_role = system_role
    member.updated_at = current_time
    desired = map_space_grants(tokens.groups, dict(gateway.oidc_group_space_map or {}))
    if desired:
        known_spaces = set(
            await session.scalars(
                select(Workspace.id).where(
                    Workspace.id.in_([pair[0] for pair in desired])
                )
            )
        )
        for space_id, _ in desired:
            if space_id not in known_spaces:
                raise ValidationException(f"Unknown OIDC-mapped workspace '{space_id}'.")
    existing_rows = list(
        await session.scalars(
            select(SpaceMembershipModel).where(SpaceMembershipModel.member_id == member.id).with_for_update()
        )
    )
    desired_set = set(desired)
    granted: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    for row in existing_rows:
        pair = (row.space_id, row.role)
        if pair not in desired_set:
            await session.delete(row)
            removed.append(pair)
    remaining = {(row.space_id, row.role) for row in existing_rows if (row.space_id, row.role) in desired_set}
    for pair in desired:
        if pair not in remaining:
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id=pair[0],
                    member_id=member.id,
                    role=pair[1],
                )
            )
            granted.append(pair)
    await session.flush()
    disabled = False
    if removed and not desired and gateway.oidc_deprovision_disable:
        leftover = await session.scalar(
            select(SpaceMembershipModel.id).where(SpaceMembershipModel.member_id == member.id)
        )
        if leftover is None and member.status == MemberStatus.ACTIVE.value:
            member.status = MemberStatus.DISABLED.value
            member.disabled_at = member.disabled_at or current_time
            member.updated_at = current_time
            family_ids = list(
                await session.scalars(
                    select(SessionFamilyModel.id).where(
                        SessionFamilyModel.member_id == member.id,
                        SessionFamilyModel.revoked_at.is_(None),
                    )
                )
            )
            for family_id in family_ids:
                family = await session.get(SessionFamilyModel, family_id)
                if family is not None:
                    family.revoked_at = current_time
                    family.revoke_reason = "oidc_deprovisioned"
            if family_ids:
                await session.execute(
                    delete(SessionCredentialModel).where(SessionCredentialModel.family_id.in_(family_ids))
                )
            disabled = True
    audit = AuditService(AuditRepository(session))
    audit.record(
        actor_member_id=member.id,
        actor_kind=PrincipalKind.SESSION.value,
        request_id=request_id,
        action="oidc.login",
        resource_type="member",
        resource_id=str(member.id),
        details={"username": normalized, "created": created, "system_role": system_role},
    )
    if disabled:
        audit.record(
            actor_member_id=member.id,
            actor_kind=PrincipalKind.SESSION.value,
            request_id=request_id,
            action="member.deprovisioned",
            resource_type="member",
            resource_id=str(member.id),
            details={"username": normalized, "removed_memberships": [list(pair) for pair in removed]},
        )
    await session.flush()
    return OidcProvisioningResult(
        member=member,
        created=created,
        system_role=system_role,
        granted_memberships=tuple(granted),
        removed_memberships=tuple(removed),
        disabled=disabled,
    )


async def authenticate_callback(
    session: AsyncSession,
    *,
    id_token: str,
    jwks: dict[str, dict[str, Any]],
    request_id: str,
    now: datetime | None = None,
    expected_nonce: str | None = None,
    jwks_uri: str | None = None,
    jwks_client: httpx.AsyncClient | None = None,
) -> tuple[OidcTokens, OidcProvisioningResult]:
    gateway = _gateway()
    issuer = (gateway.oidc_issuer or "").rstrip("/")
    if jwks_uri is not None:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise AuthenticationException("OIDC token is invalid.") from exc
        if not header.get("kid") or header.get("kid") not in jwks:
            jwks = await refresh_jwks(jwks_uri, client=jwks_client)
    claims = verify_id_token(
        id_token, jwks=jwks, issuer=issuer, audience=gateway.oidc_client_id, expected_nonce=expected_nonce
    )
    username = resolve_username(claims, gateway.oidc_username_claim or "email")
    groups = extract_groups(claims, gateway.oidc_group_claim or "groups")
    tokens = OidcTokens(
        id_token=id_token,
        claims=claims,
        subject=str(claims.get("sub", "")),
        username=username,
        groups=groups,
    )
    result = await authenticate_with_tokens(session, tokens, request_id=request_id, now=now)
    return tokens, result


def build_principal(member: MemberModel) -> Principal:
    return Principal(
        subject_id=str(member.id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole(member.system_role),
        scopes=frozenset({"*"}),
        active=True,
        restricted=False,
    )
