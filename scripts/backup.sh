#!/bin/sh
set -eu
umask 077
: "${BACKUP_DATABASE_URL:?}"
: "${BACKUP_DIR:?}"
: "${BACKUP_AGE_RECIPIENT:?}"
: "${STORAGE_DIR:?}"
: "${BACKUP_WRITERS_QUIESCED:?Set to true only after stopping the gateway and worker}"
if [ "$BACKUP_WRITERS_QUIESCED" != "true" ]; then
  echo "Gateway and worker must be stopped before backup" >&2
  exit 1
fi
backup_role_safe="$(psql "$BACKUP_DATABASE_URL" -AtX --set=ON_ERROR_STOP=1 --command="SELECT (rolsuper OR rolbypassrls)::int FROM pg_roles WHERE rolname = current_user")"
if [ "$backup_role_safe" != "1" ]; then
  echo "BACKUP_DATABASE_URL must use a complete-data role with BYPASSRLS or superuser" >&2
  exit 1
fi
mkdir -p "$BACKUP_DIR"
backup_tmp="$(mktemp -d)"
trap 'rm -rf "$backup_tmp"' EXIT INT TERM
backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
python -m scripts.storage_manifest create --database-url "$BACKUP_DATABASE_URL" --storage-dir "$STORAGE_DIR" --manifest "$backup_tmp/storage-manifest.json"
pg_dump --format=custom --no-owner --file="$backup_tmp/database.dump" "$BACKUP_DATABASE_URL"
tar -C "$STORAGE_DIR" -czf "$backup_tmp/object-storage.tar.gz" .
pg_restore --list "$backup_tmp/database.dump" >/dev/null
printf 'backup_started_at=%s\nalembic_revision=%s\nwriters_quiesced=true\n' "$backup_stamp" "$(psql "$BACKUP_DATABASE_URL" -AtX --set=ON_ERROR_STOP=1 --command='SELECT version_num FROM alembic_version')" > "$backup_tmp/BACKUP-METADATA"
(cd "$backup_tmp" && sha256sum database.dump object-storage.tar.gz storage-manifest.json BACKUP-METADATA > SHA256SUMS)
tar -C "$backup_tmp" -cf - database.dump object-storage.tar.gz storage-manifest.json BACKUP-METADATA SHA256SUMS | age --recipient "$BACKUP_AGE_RECIPIENT" --output "$BACKUP_DIR/kinbase-$backup_stamp.tar.age"
