from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import time


METRICS = {
    "backup": "gateway_backup_last_success_unixtime",
    "restore": "gateway_restore_test_last_success_unixtime",
}


def publish(kind: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{METRICS[kind]} {int(time.time())}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=tuple(METRICS))
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    publish(arguments.kind, arguments.output)


if __name__ == "__main__":
    main()
