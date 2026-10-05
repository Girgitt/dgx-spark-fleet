#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 1 ]] || { echo "usage: $0 CLUSTER" >&2; exit 2; }
exec "$(dirname "$0")/../fleetctl.py" bootstrap-storage --cluster "$1"
