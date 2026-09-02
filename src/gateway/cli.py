from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional, Sequence
from uuid import uuid4

from sqlalchemy import select

from src.gateway.application.services.embedding_generation_service import (
    EmbeddingGenerationService,
)
from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.bootstrap_service import (
    BootstrapAlreadyCompleted,
    BootstrapService,
    BootstrapValidationError,
)
from src.gateway.application.services.embedding_reembed_service import (
    EmbeddingReembedService,
)
from src.gateway.config import get_settings
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.database import get_migration_session_factory
from src.gateway.infrastructure.migrations import (
    get_schema_status_async,
    run_migrations_async,
    set_embedding_dimension,
)
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
)


async def execute(command_name: str) -> int:
    if command_name == "migrate":
        await run_migrations_async()
        print("Database migration completed.")
        return 0

    status = await get_schema_status_async()
    current = status.current_revision or "unversioned"
    if command_name == "current":
        print(f"Current database revision: {current}")
        return 0

    if not status.compatible:
        expected = ", ".join(status.head_revisions)
        print(f"Database schema is incompatible at revision {current}; expected {expected}.")
        return 1

    if command_name == "ensure-embedding-generation":
        settings = get_settings()
        factory = get_migration_session_factory()
        async with factory() as session:
            existing = await session.scalar(
                select(EmbeddingGenerationModel).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                    EmbeddingGenerationModel.model_id == settings.embedding.model_id,
                    EmbeddingGenerationModel.dimensions == settings.embedding.dimension,
                )
            )
        if existing is None:
            service = EmbeddingGenerationService(factory)
            generation = await service.ensure_active(
                model_id=settings.embedding.model_id,
                dimensions=settings.embedding.dimension,
            )
            print(f"Embedding generation ready: {generation.id}")
        else:
            print(f"Embedding generation already active: {existing.id}")
        return 0

    print(f"Database schema is compatible at revision {current}.")
    return 0


async def execute_embedding_dimension(args: argparse.Namespace) -> int:
    configured = get_settings().embedding.dimension
    if configured != args.dimension:
        print(
            f"Refusing to resize: --dimension is {args.dimension} but EMBEDDING_DIMENSION is "
            f"{configured}. Set EMBEDDING_DIMENSION to the dimension you intend, then pass the "
            "same value to --dimension."
        )
        return 1

    try:
        status = await set_embedding_dimension(
            args.dimension,
            allow_embedding_loss=args.allow_embedding_loss or args.reembed,
        )
    except RuntimeError as exc:
        print(f"Embedding dimension unchanged: {exc}")
        return 1

    tables = ", ".join(sorted(status.current))
    print(f"Embedding dimension is {status.configured} on {tables}.")
    if args.reembed:
        return await _drain_embedding_reembed(args.max_batches)
    if status.cleared:
        print(
            f"{status.cleared} stored embeddings were cleared. Re-run with --reembed to "
            "regenerate them from stored content, or re-run ingestion to rebuild them."
        )
    return 0


def _reembed_service() -> EmbeddingReembedService:
    factory = get_migration_session_factory()
    return EmbeddingReembedService(
        factory,
        HTTPEmbeddingClient(),
        EmbeddingGenerationService(factory),
    )


async def _drain_embedding_reembed(max_batches: Optional[int]) -> int:
    service = _reembed_service()
    try:
        await service.enqueue()
        batches = 0
        total = 0
        while True:
            batch = await service.run_batch()
            if batch.completed:
                break
            total += batch.rows_embedded
            batches += 1
            print(f"Re-embedded {batch.rows_embedded} rows in {batch.table} (total {total}).")
            if max_batches is not None and batches >= max_batches:
                print("Batch limit reached; re-run this command to continue.")
                return 0
    except Exception as exc:
        print(f"Re-embed stopped early ({type(exc).__name__}): {exc}")
        print("Progress is preserved; re-run this command to resume.")
        return 1
    finally:
        await HTTPEmbeddingClient.close_shared_client()
    print(f"Re-embed complete: {total} rows embedded.")
    return 0


async def execute_reembed_status() -> int:
    progress = await _reembed_service().status()
    if not progress:
        print("No embedding re-embed has been queued.")
        return 0
    for item in progress:
        if item.completed:
            state = "complete"
        else:
            state = f"{item.phase}, {item.rows_migrated} rows embedded"
        if item.last_error_code:
            state = f"{state}, last error {item.last_error_code}"
        print(f"{item.table}: {state}")
    return 0


async def execute_identity(args: argparse.Namespace) -> int:
    if not sys.stdout.isatty() and not args.allow_secret_output:
        print("Secret output requires an interactive terminal or --allow-secret-output.")
        return 2
    factory = get_migration_session_factory()
    async with factory.begin() as session:
        service = BootstrapService(session, PasswordService())
        request_id = f"cli-{uuid4()}"
        if args.command == "bootstrap-super-admin":
            issued = await service.create_first_super_admin(
                username=args.username,
                display_name=args.display_name,
                request_id=request_id,
            )
        else:
            issued = await service.recover_super_admin(
                username=args.username,
                request_id=request_id,
            )
    print(f"Member ID: {issued.member_id}")
    print(f"Temporary password: {issued.temporary_password.reveal()}")
    print(f"Expires at: {issued.expires_at.isoformat()}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway")
    parser.add_argument(
        "command",
        choices=(
            "migrate",
            "current",
            "check",
            "ensure-embedding-generation",
            "set-embedding-dimension",
            "reembed-status",
            "bootstrap-super-admin",
            "recover-super-admin",
        ),
    )
    parser.add_argument("--username")
    parser.add_argument("--display-name")
    parser.add_argument("--allow-secret-output", action="store_true")
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--max-batches", type=int)
    loss_policy = parser.add_mutually_exclusive_group()
    loss_policy.add_argument("--allow-embedding-loss", action="store_true")
    loss_policy.add_argument("--reembed", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {"bootstrap-super-admin", "recover-super-admin"}:
        if not args.username:
            parser.error("--username is required")
        if args.command == "bootstrap-super-admin" and not args.display_name:
            parser.error("--display-name is required")
    if args.command == "set-embedding-dimension" and args.dimension is None:
        parser.error("--dimension is required")
    try:
        if args.command in {"bootstrap-super-admin", "recover-super-admin"}:
            return asyncio.run(execute_identity(args))
        if args.command == "set-embedding-dimension":
            return asyncio.run(execute_embedding_dimension(args))
        if args.command == "reembed-status":
            return asyncio.run(execute_reembed_status())
        return asyncio.run(execute(args.command))
    except (BootstrapAlreadyCompleted, BootstrapValidationError) as exc:
        print(f"Database command failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Database command failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
