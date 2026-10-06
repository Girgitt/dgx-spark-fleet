#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -eq 4 ]] || { echo "usage: $0 CLUSTER TOPOLOGY RECIPE prepare|start|stop|status|smoke" >&2; exit 2; }
exec "$ROOT/scripts/python.sh" "$ROOT/fleetctl.py" recipe run "$3" "$4" --cluster "$1" --topology "$2"
