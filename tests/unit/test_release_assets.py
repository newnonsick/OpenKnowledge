from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_github_actions_are_commit_pinned_and_cover_release_gates() -> None:
    quality = read(".github/workflows/quality.yml")
    security = read(".github/workflows/security.yml")
    restore = read(".github/workflows/restore-drill.yml")
    production_restore = read(".github/workflows/production-restore-rehearsal.yml")
    live_provider = read(".github/workflows/live-provider-quality.yml")
    load = read(".github/workflows/load.yml")
    combined = "\n".join(
        (quality, security, restore, production_restore, live_provider, load)
    )
    uses = re.findall(r"uses:\s*([^\s]+)", combined)
    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)
    assert "pytest" in quality
    assert "npm test" in quality
    assert "npm run build" in quality
    assert "gh-action-pip-audit" in security
    assert "npm audit" in security
    assert "trivy-action" in security
    assert "sbom-action" in security
    assert "scripts/backup.sh" in restore
    assert "scripts/restore.sh" in restore
    assert "schedule:" in production_restore
    assert "self-hosted, recovery" in production_restore
    assert "scripts.recovery_verify" in production_restore
    assert "ALTER FUNCTION gateway_run_retention" in read("deploy/grant-runtime.sql")
    assert "RUN_LIVE_PROVIDER_TESTS" in live_provider
    assert "LIVE_PROVIDER_EVIDENCE_FILE" in live_provider
    assert "pytest -q" in quality
    assert "tests/load/k6.js" in load
    assert "actions/upload-artifact" in load
    assert "load-summary.json" in load
    assert "load-metadata.json" in load


def test_monitoring_and_load_assets_cover_required_objectives() -> None:
    alerts = read("deploy/prometheus/alerts.yml")
    load = read("tests/load/k6.js")
    caddy = read("deploy/Caddyfile")
    operations = read("docs/operations.md")
    for signal in (
        "gateway_http_requests_total",
        "gateway_ingestion_queue_depth",
        "gateway_backup_last_success_unixtime",
        "gateway_restore_test_last_success_unixtime",
        "probe_ssl_earliest_cert_expiry",
    ):
        assert signal in alerts
    for scenario in ("management", "retrieval", "streaming", "ingestion"):
        assert scenario in load
    assert "p(95)<500" in load
    assert "p(95)<1000" in load
    assert 'checks: ["rate>0.99"]' in load
    assert "handleSummary" in load
    assert "handle /metrics" not in caddy
    assert "one-hour RPO" in operations
    assert "four-hour RTO" in operations


def test_edge_routes_management_api_directly_to_gateway() -> None:
    caddy = read("deploy/Caddyfile")
    api_block = caddy.split("handle /api/*")[1].split("handle {")[0]
    assert "reverse_proxy gateway:8000" in api_block
    assert "reverse_proxy web:3000" not in api_block


def test_backup_and_restore_publish_success_metrics_atomically() -> None:
    backup = read("scripts/backup.sh")
    recovery_metric = read("scripts/recovery_metric.py")
    workflow = read(".github/workflows/restore-drill.yml")
    assert "BACKUP_METRICS_FILE" in backup
    assert "gateway_backup_last_success_unixtime" in backup
    assert "mv \"$metrics_tmp\" \"$BACKUP_METRICS_FILE\"" in backup
    assert "gateway_restore_test_last_success_unixtime" in recovery_metric
    assert "os.replace" in recovery_metric
    assert workflow.index("scripts.restore_drill verify") < workflow.index("scripts.recovery_metric restore")


def test_restore_fails_closed_before_mutating_a_target() -> None:
    restore = read("scripts/restore.sh")
    confirmation = restore.index("RESTORE_CONFIRM_ISOLATED")
    storage_check = restore.index("RESTORE_STORAGE_DIR must be empty")
    database_restore = restore.index("pg_restore --clean")
    assert confirmation < database_restore
    assert storage_check < database_restore
    assert restore.index("newly created empty database") < database_restore
    assert restore.index("RESTORE_EXPECTED_BACKUP_SHA256") < database_restore
    assert restore.index("RESTORE_MAX_BACKUP_AGE_SECONDS") < database_restore


def test_production_restore_publishes_success_only_after_runtime_readiness() -> None:
    workflow = read(".github/workflows/production-restore-rehearsal.yml")
    metric = workflow.index("scripts.recovery_metric restore")
    assert workflow.index("RECOVERY_EXPECTED_BACKUP_SHA256") < metric
    assert workflow.index("deploy/grant-runtime.sql") < metric
    assert workflow.index("scripts.recovery_roles_verify") < metric
    assert workflow.index("scripts.restore_drill verify") < metric
    assert workflow.index("healthz/ready") < metric


def test_restore_drill_verifies_representative_data_and_retrieval() -> None:
    workflow = read(".github/workflows/restore-drill.yml")
    drill = read("scripts/restore_drill.py")
    assert "scripts.restore_drill seed" in workflow
    assert "scripts.restore_drill verify" in workflow
    assert "scripts.recovery_verify" in workflow
    for gate in (
        "canonical_knowledge",
        "immutable_storage",
        "lexical_retrieval",
        "vector_retrieval",
        "ann_retrieval",
        "hnsw_plan",
    ):
        assert gate in drill


def test_compose_services_have_explicit_resource_and_process_limits() -> None:
    compose = read("compose.yaml")
    assert compose.count("mem_limit:") == 7
    assert compose.count("cpus:") == 7
    assert compose.count("pids_limit:") == 7


def test_compose_gateway_has_no_worker_or_admin_secrets() -> None:
    compose = read("compose.yaml")
    gateway = compose.split("  gateway:")[1].split("\n  worker:")[0]
    assert "WORKER_DATABASE_URL" not in gateway
    assert "POSTGRES_PASSWORD" not in gateway
    assert "GATEWAY_WORKER_DB_PASSWORD" not in gateway
    assert "DATABASE_URL: postgresql+asyncpg://gateway_runtime:" in gateway


def test_compose_worker_has_no_gateway_runtime_secret() -> None:
    compose = read("compose.yaml")
    worker = compose.split("\n  worker:")[1].split("\n  web:")[0]
    assert "WORKER_DATABASE_URL: postgresql+asyncpg://gateway_worker:" in worker
    assert "gateway_runtime:" not in worker
    assert "POSTGRES_PASSWORD" not in worker


def test_compose_healthcheck_sends_allowed_host() -> None:
    compose = read("compose.yaml")
    assert "headers={'Host':" in compose or 'headers={"Host":' in compose


def test_compose_worker_has_controlled_egress_without_exposing_postgres() -> None:
    compose = read("compose.yaml")
    worker = compose.split("\n  worker:")[1].split("\n  web:")[0]
    assert "host.docker.internal" in worker
    assert "networks: [data]" in worker
    assert "ports:" not in worker


def test_dockerfile_initializes_storage_ownership_for_application_user() -> None:
    dockerfile = read("Dockerfile")
    assert "mkdir -p /data/storage" in dockerfile
    assert "chown gateway:gateway /data/storage" in dockerfile
    assert dockerfile.index("chown gateway:gateway /data/storage") < dockerfile.index("USER 10001:10001")
