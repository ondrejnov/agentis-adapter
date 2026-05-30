#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_DIR="${MAIN_DIR:-$ROOT_DIR}"
WORKDIR="${WORKDIR:-$ROOT_DIR}"

BACKEND_DIR="$WORKDIR"
VENV_PATH="$BACKEND_DIR/.venv"

export HOME="${HOME:-/root}"
export PATH="$VENV_PATH/bin:/root/.local/bin:$PATH"

copy_if_exists() {
  local source_path="$1"
  local destination_path="$2"

  if [[ -f "$source_path" ]]; then
    mkdir -p "$(dirname "$destination_path")"
    cp "$source_path" "$destination_path"
  fi
}

(
  cd "$BACKEND_DIR"
  python3.13 -m venv "$VENV_PATH"
  VIRTUAL_ENV="$VENV_PATH" poetry lock --no-update --no-interaction --no-ansi
  VIRTUAL_ENV="$VENV_PATH" poetry install --no-interaction --no-ansi
)

