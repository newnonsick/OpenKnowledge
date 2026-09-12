#!/bin/sh
set -eu
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
. "$repo_root/scripts/backup_timestamp.sh"

expect_epoch() {
  actual="$(parse_backup_timestamp "$1")"
  if [ "$actual" != "$2" ]; then
    echo "expected $1 to parse as $2, got $actual" >&2
    exit 1
  fi
}

expect_invalid() {
  if parse_backup_timestamp "$1" >/dev/null 2>&1; then
    echo "expected $1 to be rejected" >&2
    exit 1
  fi
}

expect_epoch "20260912T050000Z" "1789189200"
expect_epoch "2026-09-12T05:00:00Z" "1789189200"
expect_epoch "20240101T000000Z" "1704067200"
expect_invalid ""
expect_invalid "not-a-timestamp"
expect_invalid "2026-13-45T99:99:99Z"
expect_invalid "20260912T050000"
