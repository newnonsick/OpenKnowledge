from uuid import uuid4

import pytest

from src.gateway.application.use_cases import (
    CreateKnowledgeCommand,
    DeleteKnowledgeCommand,
    KnowledgeCommands,
    RetrievalQueries,
    SearchKnowledgeQuery,
    UpdateKnowledgeCommand,
    UseCaseContext,
)
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException, ValidationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole


def _principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read", "knowledge:write"}),
    )


class _Item:
    def __init__(self, item_id, version=1) -> None:
        self.id = item_id
        self.version = version


class _KnowledgeStub:
    def __init__(self, session) -> None:
        self.items: dict = {}

    async def create(self, principal, *, space_id, title, content, tags, request_id):
        item = _Item(uuid4())
        self.items[item.id] = {"space_id": space_id, "title": title, "content": content, "tags": tags}
        return item

    async def get(self, item_id):
        return self.items.get(item_id)

    async def update(self, principal, item_id, *, expected_version, title, content, tags, change_summary, request_id):
        self.items[item_id] = {"title": title, "content": content, "tags": tags}
        return _Item(item_id, version=expected_version + 1)

    async def delete(self, principal, item_id, *, expected_version, request_id) -> None:
        self.items.pop(item_id, None)


class _Reservation:
    def __init__(self, status, record_id, resource_ids=()) -> None:
        self.status = status
        self.record_id = record_id
        self.resource_ids = resource_ids


class _IdempotencyStub:
    def __init__(self, session) -> None:
        from src.gateway.application.services.idempotency_service import ReservationStatus

        self._status = ReservationStatus
        self.seen: list = []
        self.completed: list = []

    async def reserve(self, *, actor_id, operation, idempotency_key, payload):
        self.seen.append((operation, idempotency_key, payload))
        return _Reservation(self._status.RESERVED, uuid4())

    async def complete(self, record_id, *, response_status, resource_ids) -> None:
        self.completed.append((record_id, response_status, tuple(resource_ids)))


class _ReplayIdempotencyStub(_IdempotencyStub):
    def __init__(self, session, resource_id) -> None:
        super().__init__(session)
        self._resource_id = resource_id

    async def reserve(self, *, actor_id, operation, idempotency_key, payload):
        self.seen.append((operation, idempotency_key, payload))
        return _Reservation(self._status.REPLAY, uuid4(), (str(self._resource_id),))


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


async def _commands(stub=None, idempotency=None):
    from unittest.mock import MagicMock

    session = MagicMock()
    knowledge = stub or _KnowledgeStub(session)
    idem = idempotency or _IdempotencyStub(session)
    commands = KnowledgeCommands(
        session,
        knowledge_factory=lambda _: knowledge,
        idempotency_factory=lambda _: idem,
    )
    return commands, knowledge, idem


async def test_create_uses_single_policy_path_and_completes_idempotency() -> None:
    commands, knowledge, idem = await _commands()
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default(), request_id="r", idempotency_key="k")

    outcome = await commands.create(ctx, CreateKnowledgeCommand(space_id="s", title=" t ", content="c", tags=("a",)))

    assert outcome.replayed is False
    assert outcome.value is not None
    assert idem.seen[0][0] == "knowledge.create"
    assert idem.completed[0][1] == 201


async def test_create_replay_returns_existing_without_duplicate_write() -> None:
    existing_id = uuid4()
    stub = _KnowledgeStub(None)
    stub.items[existing_id] = {"title": "old"}
    commands, _, idem = await _commands(stub=stub, idempotency=_ReplayIdempotencyStub(None, existing_id))
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default(), request_id="r", idempotency_key="k")

    outcome = await commands.create(ctx, CreateKnowledgeCommand(space_id="s", title="n", content="c"))

    assert outcome.replayed is True
    assert idem.completed == []


async def test_mutation_tools_disabled_denies_all_writes() -> None:
    commands, _, _ = await _commands()
    policy = EffectiveRuntimePolicy.default()
    object.__setattr__(policy, "mutation_tools_enabled", False)
    ctx = UseCaseContext(principal=_principal(), policy=policy, request_id="r", idempotency_key="k")

    with pytest.raises(AuthorizationException):
        await commands.create(ctx, CreateKnowledgeCommand(space_id="s", title="t", content="c"))
    with pytest.raises(AuthorizationException):
        await commands.update(ctx, UpdateKnowledgeCommand(item_id=uuid4(), expected_version=1, title="t", content="c"))
    with pytest.raises(AuthorizationException):
        await commands.delete(ctx, DeleteKnowledgeCommand(item_id=uuid4(), expected_version=1))


async def test_writes_require_idempotency_key() -> None:
    commands, _, _ = await _commands()
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default(), request_id="r")

    with pytest.raises(ValidationException):
        await commands.create(ctx, CreateKnowledgeCommand(space_id="s", title="t", content="c"))


async def test_retrieval_search_applies_policy_and_redaction() -> None:
    from src.gateway.domain.retrieval import RetrievalResponse, RetrievalHealth, RetrievalExplanation

    seen = {}

    class _Service:
        async def search(self, principal, query, **kwargs):
            seen.update(kwargs)
            return RetrievalResponse(
                query=query,
                hits=[],
                health=RetrievalHealth(semantic_status="active", degraded_reasons=[], embedding_generation_id=None, embedding_coverage=None),
                explanation=RetrievalExplanation(effective_space_ids=[], lexical_candidates=[], vector_candidates=[], source_diversity_limit=0, abstained=True, active_space_id=None),
            )

    queries = RetrievalQueries(service_factory=_Service, policy_loader=None)
    policy = EffectiveRuntimePolicy.default()
    object.__setattr__(policy, "retrieval_explanations_enabled", False)
    ctx = UseCaseContext(principal=_principal(), policy=policy)

    payload = await queries.search(ctx, SearchKnowledgeQuery(query="q", tags=("t",)))

    assert seen["tags"] == ["t"]
    assert payload["health"]["embedding_generation_id"] is None
    assert payload["explanation"]["effective_space_ids"] == []

    disabled = EffectiveRuntimePolicy.default()
    object.__setattr__(disabled, "knowledge_tools_enabled", False)
    with pytest.raises(AuthorizationException):
        await queries.search(UseCaseContext(principal=_principal(), policy=disabled), SearchKnowledgeQuery(query="q"))


async def test_idempotency_conflict_surfaces_as_resource_conflict() -> None:
    from src.gateway.application.services.idempotency_service import ReservationStatus

    class _Busy(_IdempotencyStub):
        async def reserve(self, *, actor_id, operation, idempotency_key, payload):
            return _Reservation(ReservationStatus.IN_PROGRESS, uuid4())

    commands, _, _ = await _commands(idempotency=_Busy(None))
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default(), request_id="r", idempotency_key="k")

    with pytest.raises(ResourceConflictException):
        await commands.create(ctx, CreateKnowledgeCommand(space_id="s", title="t", content="c"))
