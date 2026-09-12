from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import UUID

from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class UseCaseContext:
    principal: Principal
    policy: EffectiveRuntimePolicy | None = None
    request_id: str = ""
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class UseCaseOutcome(Generic[T]):
    value: T | None
    replayed: bool


def require_knowledge_tools(policy: EffectiveRuntimePolicy | None) -> None:
    if policy is not None and not policy.knowledge_tools_enabled:
        raise AuthorizationException()


def require_mutation_tools(policy: EffectiveRuntimePolicy | None) -> None:
    if policy is not None and not policy.mutation_tools_enabled:
        raise AuthorizationException()


def actor_id(principal: Principal) -> UUID:
    try:
        return UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc
