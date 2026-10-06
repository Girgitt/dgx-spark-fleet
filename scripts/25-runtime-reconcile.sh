#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -ge 2 ]] || { echo "usage: $0 CLUSTER TOPOLOGY [--recipe RECIPE ...] [--fetch] [--apply] [--gate]" >&2; exit 2; }
cluster=$1
topology=$2
shift 2
exec "$ROOT/scripts/python.sh" "$ROOT/fleetctl.py" runtime-reconcile --cluster "$cluster" --topology "$topology" "$@"
