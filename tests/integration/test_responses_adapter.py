from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.canonical import CanonicalChatResponse, CanonicalTextBlock, CanonicalUsage
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.main import create_app
from tests.integration.postgres_test_database import isolated_postgres_database


PEPPER = "test-responses-pepper-with-adequate-length"


def _member_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


def _settings() -> Settings:
    return Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: PEPPER},
            "active_api_key_pepper_version": 1,
            "trusted_hosts": ["testserver", "gateway.test"],
        }
    )


class StubOrchestrator:
    def __init__(self) -> None:
        self.seen = []

    async def orchestrate_chat(self, request, workspace_id="global"):
        self.seen.append(request)
        return CanonicalChatResponse(
            id="chatcmpl-test",
            model=request.model,
            content=[CanonicalTextBlock(text="Convention answer.")],
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )

    async def orchestrate_chat_stream(self, request, workspace_id="global"):
        raise AssertionError("streaming must not be used")


@pytest.fixture()
async def responses_provisioned():
    from src.gateway.presentation.routers import responses as responses_module

    stub = StubOrchestrator()
    now = datetime.now(timezone.utc)
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with isolated_postgres_database() as (_, factory):
        member_id = uuid4()
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="responses-owner",
                    username_normalized="responses-owner",
                    display_name="Responses Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="resp-space", name="Resp", created_by_member_id=member_id))
            session.add(
                EmbeddingGenerationModel(
                    id=uuid4(),
                    purpose="retrieval",
                    model_id="responses-test-generation",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(id=uuid4(), space_id="resp-space", member_id=member_id, role="owner")
            )
        async with factory.begin() as session:
            website_session = await SessionService(session).issue(
                _member_principal(member_id), now=now, step_up_at=now
            )
            created = await APIKeyService(session, codec).create(
                member_id,
                family_id=website_session.family_id,
                name="Responses key",
                scopes={"chat:write", "knowledge:read"},
                request_id="responses-key-create",
                now=now,
            )
            raw_key = created.secret.reveal()
            narrow = await APIKeyService(session, codec).create(
                member_id,
                family_id=website_session.family_id,
                name="Narrow key",
                scopes={"knowledge:read"},
                request_id="responses-narrow-create",
                now=now,
            )
            narrow_key = narrow.secret.reveal()
        app = create_app(_settings())
        app.dependency_overrides[responses_module.get_chat_orchestrator] = lambda: stub
        set_session_factory(factory)
        try:
            yield app, stub, raw_key, narrow_key
        finally:
            app.dependency_overrides.pop(responses_module.get_chat_orchestrator, None)
            set_session_factory(None)


async def test_responses_happy_path_string_and_messages(responses_provisioned, monkeypatch) -> None:
    from src.gateway.presentation.routers import responses as responses_module

    recorded = []

    async def fake_record(principal, *, space_id, tokens):
        recorded.append({"space_id": space_id, "tokens": tokens})

    monkeypatch.setattr(responses_module, "record_token_usage", fake_record)
    app, stub, raw_key, _ = responses_provisioned
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "default", "input": "What conventions?"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "response"
        assert body["id"].startswith("resp_")
        assert body["status"] == "completed"
        assert body["output"] == [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Convention answer."}]}
        ]
        assert body["usage"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}

        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "default",
                "input": [
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": [{"type": "input_text", "text": "Summarize."}]},
                ],
                "max_output_tokens": 64,
                "metadata": {"trace": "abc"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["output"][0]["content"][0]["text"] == "Convention answer."
        assert stub.seen[-1].max_tokens == 64
        assert stub.seen[-1].system_prompt == "Be brief."
        assert recorded == [
            {"space_id": "global", "tokens": 10},
            {"space_id": "global", "tokens": 10},
        ]


async def test_responses_rejects_unknown_fields_stream_and_images(responses_provisioned) -> None:
    app, stub, raw_key, _ = responses_provisioned
    calls_before = len(stub.seen)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        extra = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "default", "input": "Hi", "max_tokens": 16},
        )
        assert extra.status_code == 422

        streamed = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "default", "input": "Hi", "stream": True},
        )
        assert streamed.status_code == 400
        assert streamed.json()["error"]["code"] == "unsupported_stream"

        image = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "default",
                "input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://x/y.png"}]}],
            },
        )
        assert image.status_code == 400
        assert image.json()["error"]["code"] == "unsupported_input"

        empty = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "default", "input": "   "},
        )
        assert empty.status_code == 400
        assert len(stub.seen) == calls_before


async def test_responses_auth_and_model_errors(responses_provisioned) -> None:
    app, _, raw_key, narrow_key = responses_provisioned
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        denied = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {narrow_key}"},
            json={"model": "default", "input": "Hi"},
        )
        assert denied.status_code in (401, 404)

        anonymous = await client.post("/v1/responses", json={"model": "default", "input": "Hi"})
        assert anonymous.status_code == 401

        unknown_model = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "no-such-model-xyz", "input": "Hi"},
        )
        assert unknown_model.status_code == 404
