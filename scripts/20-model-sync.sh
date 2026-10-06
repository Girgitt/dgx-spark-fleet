#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cat >&2 <<'EOF'
WARNING: 20-model-sync.sh is the legacy explicit-path interface.
Normal operation is now recipe-driven:
  ./scripts/20-model-reconcile.sh CLUSTER [--recipe RECIPE ...] [--apply] [--download-missing]
EOF
if [[ $# -lt 4 ]]; then
  echo "legacy usage: $0 CLUSTER TOPOLOGY MODEL_PATH SOURCE_NODE" >&2
  exit 2
fi
cluster=$1 topology=$2 path=$3 source=$4
exec "$ROOT/scripts/python.sh" "$ROOT/fleetctl.py" model-sync --cluster "$cluster" --topology "$topology" --path "$path" --source-node "$source"
