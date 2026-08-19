from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_and_development_locks_have_distinct_contracts():
    runtime = (ROOT / "requirements" / "runtime.lock").read_text(encoding="utf-8")
    development = (ROOT / "requirements" / "dev.lock").read_text(encoding="utf-8")

    assert "--hash=sha256:" in runtime
    assert "--hash=sha256:" in development
    for package in ("pytest==", "pytest-asyncio==", "aiosqlite==", "respx=="):
        assert package not in runtime.lower()
        assert package in development.lower()


def test_readme_documents_current_production_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "python -m src.gateway.cli migrate" in readme
    assert "/healthz/live" in readme
    assert "/healthz/ready" in readme
    assert "HNSW" in readme
    assert "SQLite" in readme
    assert "TEST_DATABASE_URL" in readme
    assert "Migrations run automatically" not in readme


def test_compatibility_requirements_file_uses_development_lock():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()

    assert requirements == "-r requirements/dev.lock"


def test_dependency_verifier_fails_on_invalid_hash(tmp_path: Path):
    invalid_lock = tmp_path / "invalid.lock"
    invalid_lock.write_text(
        "idna==3.10 --hash=sha256:" + ("0" * 64) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "verify_dependencies.ps1"),
            "-LockPath",
            str(invalid_lock),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
