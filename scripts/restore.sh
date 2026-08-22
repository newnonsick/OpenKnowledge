#!/bin/sh
set -eu
umask 077
: "${BACKUP_FILE:?}"
: "${RESTORE_DATABASE_URL:?}"
: "${RESTORE_STORAGE_DIR:?}"
: "${BACKUP_AGE_IDENTITY:?}"
: "${RESTORE_CONFIRM_ISOLATED:?Set to true only for an isolated restore target}"
: "${RESTORE_EXPECTED_DATABASE:?}"
if [ "$RESTORE_CONFIRM_ISOLATED" != "true" ]; then
  echo "Restore target isolation must be confirmed" >&2
  exit 1
fi
case "$RESTORE_STORAGE_DIR" in
  /|"")
    echo "RESTORE_STORAGE_DIR must be a dedicated directory" >&2
    exit 1
    ;;
esac
if [ ! -d "$RESTORE_STORAGE_DIR" ] || [ ! -w "$RESTORE_STORAGE_DIR" ]; then
  echo "RESTORE_STORAGE_DIR must be an existing writable directory" >&2
  exit 1
fi
if [ "$(find "$RESTORE_STORAGE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "RESTORE_STORAGE_DIR must be empty" >&2
  exit 1
fi
restore_database="$(psql "$RESTORE_DATABASE_URL" -AtX --set=ON_ERROR_STOP=1 --command='SELECT current_database()')"
if [ "$restore_database" != "$RESTORE_EXPECTED_DATABASE" ]; then
  echo "RESTORE_EXPECTED_DATABASE does not match the restore target" >&2
  exit 1
fi
target_relations="$(psql "$RESTORE_DATABASE_URL" -AtX --set=ON_ERROR_STOP=1 --command="SELECT count(*) FROM pg_class relation JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace WHERE namespace.nspname = 'public' AND relation.relkind IN ('r','p','v','m','S','f')")"
if [ "$target_relations" != "0" ]; then
  echo "RESTORE_DATABASE_URL must reference a newly created empty database" >&2
  exit 1
fi
restore_tmp="$(mktemp -d)"
trap 'rm -rf "$restore_tmp"' EXIT INT TERM
age --decrypt --identity "$BACKUP_AGE_IDENTITY" "$BACKUP_FILE" | tar -C "$restore_tmp" -xf -
(cd "$restore_tmp" && sha256sum -c SHA256SUMS)
if [ -n "${RESTORE_EXPECTED_BACKUP_SHA256:-}" ]; then
  actual_backup_sha256="$(sha256sum "$BACKUP_FILE" | cut -d ' ' -f 1)"
  if [ "$actual_backup_sha256" != "$RESTORE_EXPECTED_BACKUP_SHA256" ]; then
    echo "Backup artifact identity does not match" >&2
    exit 1
  fi
fi
if [ -n "${RESTORE_MAX_BACKUP_AGE_SECONDS:-}" ]; then
  backup_started_at="$(sed -n 's/^backup_started_at=//p' "$restore_tmp/BACKUP-METADATA")"
  case "$backup_started_at" in
    ????[0-1][0-9][0-3][0-9]T[0-2][0-9][0-5][0-9][0-5][0-9]Z) ;;
    *) echo "Backup metadata timestamp is invalid" >&2; exit 1 ;;
  esac
  backup_epoch="$(date -u -d "$backup_started_at" +%s)"
  current_epoch="$(date -u +%s)"
  backup_age="$((current_epoch - backup_epoch))"
  if [ "$backup_age" -lt 0 ] || [ "$backup_age" -gt "$RESTORE_MAX_BACKUP_AGE_SECONDS" ]; then
    echo "Backup is outside the permitted recovery age" >&2
    exit 1
  fi
fi
pg_restore --clean --if-exists --no-owner --dbname="$RESTORE_DATABASE_URL" "$restore_tmp/database.dump"
tar -C "$RESTORE_STORAGE_DIR" -xzf "$restore_tmp/object-storage.tar.gz"
python -m scripts.storage_manifest verify --database-url "$RESTORE_DATABASE_URL" --storage-dir "$RESTORE_STORAGE_DIR" --manifest "$restore_tmp/storage-manifest.json"
