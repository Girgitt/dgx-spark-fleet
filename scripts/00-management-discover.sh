#!/usr/bin/env bash
set -euo pipefail
# Read-only helper. Optionally pass one or more MAC suffixes, e.g. 12:34:56.
# It deliberately does not hard-code a vendor OUI: USB/WireGuard/bridged management
# paths are valid too. Use discovered IPv4 addresses in fleetctl.py init.
mapfile -t suffixes < <(printf '%s\n' "$@" | tr '[:upper:]' '[:lower:]')
{
  ip -4 neigh show 2>/dev/null || true
  arp -an 2>/dev/null || true
} | awk 'NF' | while IFS= read -r line; do
  if ((${#suffixes[@]}==0)); then
    printf '%s\n' "$line"
    continue
  fi
  low=${line,,}
  for s in "${suffixes[@]}"; do
    [[ "$low" == *"$s"* ]] && { printf '%s\n' "$line"; break; }
  done
done
