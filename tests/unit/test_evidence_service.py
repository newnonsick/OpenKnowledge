from uuid import uuid4

import pytest

from src.gateway.application.services.evidence_service import EvidenceReference, parse_citation_uri
from src.gateway.application.use_cases.context import UseCaseContext
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException, ValidationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole


def test_parse_knowledge_citation() -> None:
    item_id = uuid4()
    revision_id = uuid4()
    reference = parse_citation_uri(
        f"openknowledge://spaces/global/knowledge/{item_id}/revisions/{revision_id}"
    )
    assert reference == EvidenceReference("knowledge_revision", "global", item_id, revision_id, None)


def test_parse_document_chunk_citation() -> None:
    document_id = uuid4()
    revision_id = uuid4()
    chunk_id = uuid4()
    reference = parse_citation_uri(
        f"openknowledge://spaces/global/documents/{document_id}/revisions/{revision_id}/chunks/{chunk_id}"
    )
    assert reference == EvidenceReference("document_chunk", "global", document_id, revision_id, chunk_id)


def test_parse_document_revision_without_chunk() -> None:
    document_id = uuid4()
    revision_id = uuid4()
    reference = parse_citation_uri(
        f"openknowledge://spaces/global/documents/{document_id}/revisions/{revision_id}"
    )
    assert reference.chunk_id is None


def test_parse_rejects_unknown_shapes() -> None:
    with pytest.raises(ValidationException):
        parse_citation_uri("https://example.com/not-a-citation")
    with pytest.raises(ValidationException):
        parse_citation_uri("openknowledge://spaces/global/knowledge/not-a-uuid/revisions/also-bad")
    with pytest.raises(ValidationException):
        parse_citation_uri(f"openknowledge://spaces/global/documents/{uuid4()}/revisions/{uuid4()}/pages/3")
