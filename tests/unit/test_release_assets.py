from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_github_actions_are_commit_pinned_and_cover_release_gates() -> None:
    quality = read(".github/workflows/quality.yml")
    security = read(".github/workflows/security.yml")
    restore = read(".github/workflows/restore-drill.yml")
    load = read(".github/workflows/load.yml")
    combined = "\n".join((quality, security, restore, load))
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
    assert "tests/load/k6.js" in load


def test_monitoring_and_load_assets_cover_required_objectives() -> None:
    alerts = read("deploy/prometheus/alerts.yml")
    load = read("tests/load/k6.js")
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
    assert "one-hour RPO" in operations
    assert "four-hour RTO" in operations


def test_backup_and_restore_publish_success_metrics_atomically() -> None:
    backup = read("scripts/backup.sh")
    restore = read("scripts/restore.sh")
    assert "BACKUP_METRICS_FILE" in backup
    assert "gateway_backup_last_success_unixtime" in backup
    assert "mv \"$metrics_tmp\" \"$BACKUP_METRICS_FILE\"" in backup
    assert "RESTORE_METRICS_FILE" in restore
    assert "gateway_restore_test_last_success_unixtime" in restore
    assert "mv \"$metrics_tmp\" \"$RESTORE_METRICS_FILE\"" in restore


def test_compose_services_have_explicit_resource_and_process_limits() -> None:
    compose = read("compose.yaml")
    assert compose.count("mem_limit:") == 7
    assert compose.count("cpus:") == 7
    assert compose.count("pids_limit:") == 7
