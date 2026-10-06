#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -eq 2 ]] || { echo "usage: $0 CLUSTER TOPOLOGY" >&2; exit 2; }
exec "$ROOT/scripts/python.sh" "$ROOT/fleetctl.py" topology set --cluster "$1" --name "$2"
