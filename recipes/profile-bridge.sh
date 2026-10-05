#!/usr/bin/env bash
set -euo pipefail
op=${1:?operation required}
: "${DGX_FLEET_CONFIG:?}"
: "${DGX_PROFILE:?recipe has no profile mapping for TP=${DGX_TP:-?}}"
case "$op" in
  prepare|start|stop|status|smoke)
    exec ./fleet.py --config "$DGX_FLEET_CONFIG" profile "$op" "$DGX_PROFILE"
    ;;
  *) echo "unsupported operation: $op" >&2; exit 2 ;;
esac
