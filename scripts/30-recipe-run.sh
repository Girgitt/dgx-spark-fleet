#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 4 ]] || { echo "usage: $0 CLUSTER TOPOLOGY RECIPE prepare|start|stop|status|smoke" >&2; exit 2; }
exec "$(dirname "$0")/../fleetctl.py" recipe run "$3" "$4" --cluster "$1" --topology "$2"
