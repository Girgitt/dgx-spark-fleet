#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cluster=${1:?usage: $0 CLUSTER [--recipe RECIPE ...] [--apply] [--download-missing] [--seed-node N]}
shift
exec python3 "$ROOT/fleetctl.py" model-reconcile --cluster "$cluster" "$@"
