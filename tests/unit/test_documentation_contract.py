from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _documented_contract() -> str:
    documents = [ROOT / "README.md"]
    documents.extend(sorted((ROOT / "docs").glob("*.md")))
    return "\n".join(path.read_text(encoding="utf-8") for path in documents)


def test_runtime_and_development_locks_have_distinct_contracts():
    runtime = (ROOT / "requirements" / "runtime.lock").read_text(encoding="utf-8")
    development = (ROOT / "requirements" / "dev.lock").read_text(encoding="utf-8")

    assert "--hash=sha256:" in runtime
    assert "--hash=sha256:" in development
    for package in ("pytest==", "pytest-asyncio==", "aiosqlite==", "respx=="):
        assert package not in runtime.lower()
        assert package in development.lower()


def test_documentation_documents_current_production_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = _documented_contract()

    assert "python -m src.gateway.cli migrate" in documented
    assert "/healthz/live" in documented
    assert "/healthz/ready" in documented
    assert "HNSW" in documented
    assert "SQLite" in documented
    assert "TEST_DATABASE_URL" in documented
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
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell is not None
    result = subprocess.run(
        [
            shell,
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
