from __future__ import annotations

import argparse
import asyncio
from typing import Optional, Sequence

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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway")
    parser.add_argument("command", choices=("migrate", "current", "check"))
    args = parser.parse_args(argv)
    try:
        return asyncio.run(execute(args.command))
    except Exception as exc:
        print(f"Database command failed ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
