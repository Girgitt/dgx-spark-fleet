#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${DGX_FLEET_VENV:-$ROOT_DIR/venv_py312}"
PYTHON_BIN="${DGX_FLEET_PYTHON:-$VENV_DIR/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  cat >&2 <<MSG
ERROR: dgx-spark-fleet Python environment is missing:
  $PYTHON_BIN

Run:
  $ROOT_DIR/scripts/bootstrap.sh
MSG
  exit 1
fi

exec "$PYTHON_BIN" "$@"
