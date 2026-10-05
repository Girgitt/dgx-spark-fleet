#!/usr/bin/env bash
set -euo pipefail
echo "WARNING: scripts/03-validate.sh is deprecated; validation is now step 04." >&2
echo "Using scripts/04-validate.sh for compatibility." >&2
exec "$(dirname "$0")/04-validate.sh" "$@"
