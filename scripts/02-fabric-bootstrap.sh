#!/usr/bin/env bash
set -euo pipefail
echo "WARNING: scripts/02-fabric-bootstrap.sh is deprecated; storage bootstrap is now step 02." >&2
echo "Using scripts/03-fabric-bootstrap.sh for compatibility." >&2
exec "$(dirname "$0")/03-fabric-bootstrap.sh" "$@"
