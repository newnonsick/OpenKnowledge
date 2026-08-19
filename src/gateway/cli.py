from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional, Sequence
from uuid import uuid4

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.bootstrap_service import BootstrapService
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.migrations import (
    get_schema_status_async,
    run_migrations_async,
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

    if status.compatible:
        print(f"Database schema is compatible at revision {current}.")
        return 0

    expected = ", ".join(status.head_revisions)
    print(f"Database schema is incompatible at revision {current}; expected {expected}.")
    return 1


async def execute_identity(args: argparse.Namespace) -> int:
    if not sys.stdout.isatty() and not args.allow_secret_output:
        print("Secret output requires an interactive terminal or --allow-secret-output.")
        return 2
    factory = get_session_factory()
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
    except Exception as exc:
        print(f"Database command failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
