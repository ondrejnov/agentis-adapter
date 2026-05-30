#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_ROOT="${SNAPSHOT_ROOT:-/tmp/agentis-source-snapshots}"
MAX_AGE_MINUTES="${MAX_AGE_MINUTES:-60}"

if [[ ! -d "$SNAPSHOT_ROOT" ]]; then
  exit 0
fi

case "$SNAPSHOT_ROOT" in
  /tmp/agentis-source-snapshots|/tmp/agentis-source-snapshots/*) ;;
  *)
    printf 'Refusing to clean unexpected SNAPSHOT_ROOT: %s\n' "$SNAPSHOT_ROOT" >&2
    exit 1
    ;;
esac

if [[ "${1:-}" == "--dry-run" ]]; then
  find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d -mmin +"$MAX_AGE_MINUTES" -print
else
  find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d -mmin +"$MAX_AGE_MINUTES" -exec rm -rf -- {} +
fi
