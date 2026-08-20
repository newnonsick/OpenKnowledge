#!/bin/sh
set -eu
umask 077
: "${BACKUP_FILE:?}"
: "${RESTORE_DATABASE_URL:?}"
: "${RESTORE_STORAGE_DIR:?}"
: "${BACKUP_AGE_IDENTITY:?}"
restore_tmp="$(mktemp -d)"
trap 'rm -rf "$restore_tmp"' EXIT INT TERM
age --decrypt --identity "$BACKUP_AGE_IDENTITY" "$BACKUP_FILE" | tar -C "$restore_tmp" -xf -
(cd "$restore_tmp" && sha256sum -c SHA256SUMS)
pg_restore --clean --if-exists --no-owner --dbname="$RESTORE_DATABASE_URL" "$restore_tmp/database.dump"
if [ -d "$RESTORE_STORAGE_DIR" ] && [ "$(find "$RESTORE_STORAGE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "RESTORE_STORAGE_DIR must be empty" >&2
  exit 1
fi
mkdir -p "$RESTORE_STORAGE_DIR"
tar -C "$RESTORE_STORAGE_DIR" -xzf "$restore_tmp/object-storage.tar.gz"
python -m scripts.storage_manifest verify --database-url "$RESTORE_DATABASE_URL" --storage-dir "$RESTORE_STORAGE_DIR" --manifest "$restore_tmp/storage-manifest.json"
if [ -n "${RESTORE_METRICS_FILE:-}" ]; then
  metrics_dir="$(dirname "$RESTORE_METRICS_FILE")"
  mkdir -p "$metrics_dir"
  metrics_tmp="$(mktemp "$metrics_dir/.restore-metrics.XXXXXX")"
  printf 'gateway_restore_test_last_success_unixtime %s\n' "$(date -u +%s)" > "$metrics_tmp"
  chmod 600 "$metrics_tmp"
  mv "$metrics_tmp" "$RESTORE_METRICS_FILE"
fi
