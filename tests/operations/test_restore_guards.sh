#!/bin/sh
set -eu
: "${BACKUP_DATABASE_URL:?}"
guard_tmp="$(mktemp -d)"
trap 'rm -rf "$guard_tmp"' EXIT INT TERM
touch "$guard_tmp/not-a-directory"
if BACKUP_FILE="$guard_tmp/missing.age" RESTORE_DATABASE_URL="$BACKUP_DATABASE_URL" RESTORE_STORAGE_DIR="$guard_tmp/not-a-directory" BACKUP_AGE_IDENTITY="$guard_tmp/missing-key" RESTORE_CONFIRM_ISOLATED=true RESTORE_EXPECTED_DATABASE=gateway_source sh scripts/restore.sh; then
  exit 1
fi
mkdir "$guard_tmp/empty-storage"
if BACKUP_FILE="$guard_tmp/missing.age" RESTORE_DATABASE_URL="$BACKUP_DATABASE_URL" RESTORE_STORAGE_DIR="$guard_tmp/empty-storage" BACKUP_AGE_IDENTITY="$guard_tmp/missing-key" RESTORE_CONFIRM_ISOLATED=true RESTORE_EXPECTED_DATABASE=gateway_source sh scripts/restore.sh; then
  exit 1
fi
