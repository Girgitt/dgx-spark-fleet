#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -eq 1 ]] || { echo "usage: $0 CLUSTER" >&2; exit 2; }
exec "$ROOT/scripts/python.sh" "$ROOT/fleetctl.py" bootstrap-fabric --cluster "$1"
