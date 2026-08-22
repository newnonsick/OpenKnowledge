from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy.ext.asyncio import create_async_engine

from src.gateway.infrastructure.database import (
    normalize_database_url,
    validate_runtime_database_connection,
    validate_worker_database_connection,
)


async def verify_roles(runtime_database_url: str, worker_database_url: str) -> dict[str, bool]:
    runtime_engine = create_async_engine(normalize_database_url(runtime_database_url))
    worker_engine = create_async_engine(normalize_database_url(worker_database_url))
    try:
        async with runtime_engine.connect() as connection:
            await validate_runtime_database_connection(connection)
        async with worker_engine.connect() as connection:
            await validate_worker_database_connection(connection)
        return {"runtime_role": True, "worker_role": True}
    finally:
        await runtime_engine.dispose()
        await worker_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-database-url", required=True)
    parser.add_argument("--worker-database-url", required=True)
    arguments = parser.parse_args()
    result = asyncio.run(
        verify_roles(arguments.runtime_database_url, arguments.worker_database_url)
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
