#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 CLUSTER TOPOLOGY" >&2; exit 2; }
exec "$(dirname "$0")/../fleetctl.py" topology set --cluster "$1" --name "$2"
