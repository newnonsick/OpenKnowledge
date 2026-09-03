from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.gateway.application.services.embedding_reembed_service import (
    REEMBED_TARGETS,
    EmbeddingReembedService,
    ReembedBatch,
    ReembedProgress,
)
from src.gateway.config import get_settings
from src.gateway.infrastructure.migrations import (
    EmbeddingDimensionStatus,
    VECTOR_INDEXES,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeReembedService:
    def __init__(self, batches: list[ReembedBatch]) -> None:
        self._batches = list(batches)
        self.progress: tuple[ReembedProgress, ...] = ()
        self.enqueued = False
        self.batch_sizes: list[int | None] = []

    async def enqueue(self) -> tuple[ReembedProgress, ...]:
        self.enqueued = True
        return self.progress

    async def status(self) -> tuple[ReembedProgress, ...]:
        return self.progress

    async def run_batch(self, batch_size: int | None = None) -> ReembedBatch:
        self.batch_sizes.append(batch_size)
        if not self._batches:
            return ReembedBatch(table="", rows_embedded=0, completed=True)
        return self._batches.pop(0)


def test_reembed_targets_cover_every_vector_column() -> None:
    assert sorted(target.name for target in REEMBED_TARGETS) == sorted(VECTOR_INDEXES)


def test_reembed_batch_queries_compile_for_postgresql() -> None:
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    for target in REEMBED_TARGETS:
        model = target.model
        statement = (
            select(model.id, model.content)
            .where(model.embedding.is_(None))
            .order_by(model.id)
            .limit(10)
        )
        if target.generation_aware:
            statement = statement.where(model.active.is_(True))

        compiled = str(statement.compile(dialect=postgresql.dialect()))

        assert f"FROM {target.name}" in compiled
        assert f"{target.name}.embedding IS NULL" in compiled


def test_deactivate_superseded_units_statement_targets_the_active_generation() -> None:
    from src.gateway.application.services.embedding_reembed_service import (
        _DEACTIVATE_SUPERSEDED_UNITS_SQL,
    )

    statement = _DEACTIVATE_SUPERSEDED_UNITS_SQL
    assert statement.count("retrieval_units") == 2
    assert "twin.embedding_generation_id = CAST(:generation_id AS uuid)" in statement


def test_set_embedding_dimension_requires_an_explicit_dimension() -> None:
    from src.gateway import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["set-embedding-dimension"])

    assert raised.value.code == 2


def test_set_embedding_dimension_refuses_mismatched_configuration(
    monkeypatch, capsys
) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    resize = AsyncMock()
    monkeypatch.setattr(cli, "set_embedding_dimension", resize)

    assert cli.main(["set-embedding-dimension", "--dimension", "768"]) == 1

    resize.assert_not_awaited()
    output = capsys.readouterr().out
    assert "EMBEDDING_DIMENSION is 1024" in output
    assert "Refusing to resize" in output


def test_set_embedding_dimension_reports_runtime_failure(monkeypatch, capsys) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setattr(
        cli,
        "set_embedding_dimension",
        AsyncMock(side_effect=RuntimeError("Database schema is at revision 020.")),
    )

    assert cli.main(["set-embedding-dimension", "--dimension", "768"]) == 1

    output = capsys.readouterr().out
    assert "Embedding dimension unchanged" in output
    assert "revision 020" in output


def test_set_embedding_dimension_reports_cleared_embeddings(monkeypatch, capsys) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setattr(
        cli,
        "set_embedding_dimension",
        AsyncMock(
            return_value=EmbeddingDimensionStatus(
                configured=768,
                current={"document_chunks": 768, "knowledge_revisions": 768},
                stored={},
                cleared=3,
            )
        ),
    )

    assert cli.main(["set-embedding-dimension", "--dimension", "768", "--allow-embedding-loss"]) == 0

    output = capsys.readouterr().out
    assert "Embedding dimension is 768" in output
    assert "3 stored embeddings were cleared" in output


def test_set_embedding_dimension_reembed_drains_until_complete(
    monkeypatch, capsys
) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setattr(
        cli,
        "set_embedding_dimension",
        AsyncMock(
            return_value=EmbeddingDimensionStatus(
                configured=768,
                current={"retrieval_units": 768},
                stored={"retrieval_units": 0},
            )
        ),
    )
    service = _FakeReembedService(
        [
            ReembedBatch(table="knowledge_revisions", rows_embedded=2, completed=False),
            ReembedBatch(table="retrieval_units", rows_embedded=1, completed=False),
        ]
    )
    monkeypatch.setattr(cli, "_reembed_service", lambda: service)
    monkeypatch.setattr(
        cli.HTTPEmbeddingClient, "close_shared_client", AsyncMock(), raising=True
    )

    assert cli.main(["set-embedding-dimension", "--dimension", "768", "--reembed"]) == 0

    output = capsys.readouterr().out
    assert service.enqueued is True
    assert "Re-embed complete: 3 rows embedded." in output


def test_set_embedding_dimension_reembed_honours_batch_limit(
    monkeypatch, capsys
) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setattr(
        cli,
        "set_embedding_dimension",
        AsyncMock(
            return_value=EmbeddingDimensionStatus(
                configured=768, current={"retrieval_units": 768}, stored={"retrieval_units": 0}
            )
        ),
    )
    service = _FakeReembedService(
        [ReembedBatch(table="knowledge_revisions", rows_embedded=4, completed=False)]
        * 3
    )
    monkeypatch.setattr(cli, "_reembed_service", lambda: service)
    monkeypatch.setattr(
        cli.HTTPEmbeddingClient, "close_shared_client", AsyncMock(), raising=True
    )

    assert (
        cli.main(
            ["set-embedding-dimension", "--dimension", "768", "--reembed", "--max-batches", "2"]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Batch limit reached" in output
    assert "Re-embed complete" not in output


def test_set_embedding_dimension_reembed_preserves_progress_on_failure(
    monkeypatch, capsys
) -> None:
    from src.gateway import cli

    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setattr(
        cli,
        "set_embedding_dimension",
        AsyncMock(
            return_value=EmbeddingDimensionStatus(
                configured=768, current={"retrieval_units": 768}, stored={"retrieval_units": 0}
            )
        ),
    )

    class _FailingService(_FakeReembedService):
        async def run_batch(self, batch_size: int | None = None) -> ReembedBatch:
            self.batch_sizes.append(batch_size)
            raise RuntimeError("Embedding provider connection failed.")

    monkeypatch.setattr(cli, "_reembed_service", lambda: _FailingService([]))
    monkeypatch.setattr(
        cli.HTTPEmbeddingClient, "close_shared_client", AsyncMock(), raising=True
    )

    assert cli.main(["set-embedding-dimension", "--dimension", "768", "--reembed"]) == 1

    output = capsys.readouterr().out
    assert "Progress is preserved" in output


def test_reembed_status_reports_progress(monkeypatch, capsys) -> None:
    from src.gateway import cli

    service = _FakeReembedService([])
    service.progress = (
        ReembedProgress(
            table="knowledge_revisions",
            phase="snapshot",
            rows_migrated=120,
            completed=False,
        ),
        ReembedProgress(
            table="retrieval_units",
            phase="complete",
            rows_migrated=40,
            completed=True,
            last_error_code="EmbeddingException",
        ),
    )
    monkeypatch.setattr(cli, "_reembed_service", lambda: service)

    assert cli.main(["reembed-status"]) == 0

    output = capsys.readouterr().out
    assert "knowledge_revisions: snapshot, 120 rows embedded" in output
    assert "retrieval_units: complete" in output


def test_reembed_status_reports_an_empty_queue(monkeypatch, capsys) -> None:
    from src.gateway import cli

    monkeypatch.setattr(cli, "_reembed_service", lambda: _FakeReembedService([]))

    assert cli.main(["reembed-status"]) == 0
    assert "No embedding re-embed has been queued." in capsys.readouterr().out


def test_embedding_reembed_service_requires_positive_batch_size() -> None:
    with pytest.raises(ValueError):
        EmbeddingReembedService(
            session_factory=AsyncMock(),
            embedding_client=AsyncMock(),
            generation_service=AsyncMock(),
            batch_size=0,
        )


def test_parse_vector_dimension_supports_vector_and_halfvec() -> None:
    from src.gateway.infrastructure.migrations import _parse_vector_dimension

    assert _parse_vector_dimension("vector(1024)") == 1024
    assert _parse_vector_dimension("halfvec(2048)") == 2048
    assert _parse_vector_dimension("halfvec(4000)") == 4000
    assert _parse_vector_dimension("invalid") is None
    assert _parse_vector_dimension(None) is None


@pytest.mark.asyncio
async def test_set_embedding_dimension_rejects_out_of_bounds() -> None:
    from src.gateway.infrastructure.migrations import set_embedding_dimension

    with pytest.raises(RuntimeError) as exc_zero:
        await set_embedding_dimension(0)
    assert "between 1 and 4000" in str(exc_zero.value)

    with pytest.raises(RuntimeError) as exc_too_large:
        await set_embedding_dimension(4001)
    assert "between 1 and 4000" in str(exc_too_large.value)
