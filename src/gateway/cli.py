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
from src.gateway.config import get_settings
from src.gateway.infrastructure.database import get_migration_session_factory
from src.gateway.infrastructure.migrations import (
    get_schema_status_async,
    run_migrations_async,
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
            "bootstrap-super-admin",
            "recover-super-admin",
        ),
    )
    parser.add_argument("--username")
    parser.add_argument("--display-name")
    parser.add_argument("--allow-secret-output", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {"bootstrap-super-admin", "recover-super-admin"}:
        if not args.username:
            parser.error("--username is required")
        if args.command == "bootstrap-super-admin" and not args.display_name:
            parser.error("--display-name is required")
    try:
        if args.command in {"bootstrap-super-admin", "recover-super-admin"}:
            return asyncio.run(execute_identity(args))
        return asyncio.run(execute(args.command))
    except (BootstrapAlreadyCompleted, BootstrapValidationError) as exc:
        print(f"Database command failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Database command failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
