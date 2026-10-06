#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${DGX_FLEET_VENV:-$ROOT_DIR/venv_py312}"
BOOTSTRAP_PYTHON="${DGX_FLEET_BOOTSTRAP_PYTHON:-}"

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    >/dev/null 2>&1
}

if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
      BOOTSTRAP_PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$BOOTSTRAP_PYTHON" || ! -x "$BOOTSTRAP_PYTHON" ]]; then
  cat >&2 <<'MSG'
ERROR: Python 3.12 or newer is required to bootstrap dgx-spark-fleet.

Install a supported Python interpreter on the management host, or point the
bootstrap script at one explicitly, for example:

  DGX_FLEET_BOOTSTRAP_PYTHON=/opt/python3.12/bin/python3.12 ./scripts/bootstrap.sh
MSG
  exit 1
fi

if ! python_is_supported "$BOOTSTRAP_PYTHON"; then
  echo "ERROR: $BOOTSTRAP_PYTHON is older than Python 3.12." >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating repo-local Python environment: $VENV_DIR"
  if ! "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"; then
    echo "ERROR: could not create $VENV_DIR; ensure the Python venv module is installed." >&2
    exit 1
  fi
else
  echo "Reusing repo-local Python environment: $VENV_DIR"
fi

if ! python_is_supported "$VENV_DIR/bin/python"; then
  cat >&2 <<MSG
ERROR: existing environment $VENV_DIR uses an unsupported Python.
Remove it and rerun scripts/bootstrap.sh, or choose another DGX_FLEET_VENV.
MSG
  exit 1
fi

cd "$ROOT_DIR"

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e '.[dev]'

git submodule update --init --recursive

"$VENV_DIR/bin/python" - <<'PY'
import sys
import tomllib
print(f"Python environment ready: {sys.executable} ({sys.version.split()[0]})")
print("tomllib: OK")
PY

cat <<MSG

DGX Spark Fleet bootstrap complete.

Preferred operator entry point (activation not required):
  ./fleetctl --help

For direct Python-script use:
  source "$VENV_DIR/bin/activate"
  ./fleetctl.py --help

Run verification with:
  make test
MSG
