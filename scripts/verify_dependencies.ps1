param(
    [string]$LockPath
)

$ErrorActionPreference = "Stop"
$workspaceRoot = Split-Path -Parent $PSScriptRoot
if (-not $LockPath) {
    $LockPath = Join-Path $workspaceRoot "requirements/runtime.lock"
}
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$verificationRoot = Join-Path $tempRoot ("akg_dependency_verify_" + [guid]::NewGuid().ToString("N"))
$resolvedVerificationRoot = [System.IO.Path]::GetFullPath($verificationRoot)

if (-not $resolvedVerificationRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe verification directory"
}

try {
    python -m venv $resolvedVerificationRoot
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
    $python = Join-Path $resolvedVerificationRoot "Scripts/python.exe"
    & $python -m pip install --disable-pip-version-check --require-hashes -r $LockPath
    if ($LASTEXITCODE -ne 0) { throw "Locked dependency installation failed" }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed" }
    & $python -c "import alembic, argon2, asyncpg, cryptography, fastapi, httpx, multipart, pgvector, pydantic, pydantic_settings, pyotp, pypdf, sqlalchemy, structlog, uvicorn; print('runtime imports verified')"
    if ($LASTEXITCODE -ne 0) { throw "Runtime import verification failed" }
    & $python -c 'from importlib.metadata import distributions; print("\n".join(sorted("{}=={}".format(d.metadata.get("Name"), d.version) for d in distributions())))'
    if ($LASTEXITCODE -ne 0) { throw "Installed package inventory failed" }
} finally {
    if (Test-Path -LiteralPath $resolvedVerificationRoot) {
        Remove-Item -LiteralPath $resolvedVerificationRoot -Recurse -Force
    }
}
