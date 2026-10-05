#!/usr/bin/env bash
set -euo pipefail
[[ $# -ge 1 ]] || { echo "usage: $0 CLUSTER [TOPOLOGY]" >&2; exit 2; }
args=(validate --cluster "$1" --stage full)
[[ $# -ge 2 ]] && args+=(--topology "$2")
exec "$(dirname "$0")/../fleetctl.py" "${args[@]}"
