#!/usr/bin/env bash
set -euo pipefail
INSTALL=0
[[ "${1:-}" == "--install" ]] && INSTALL=1
required=(git rsync curl python3 docker nvidia-smi ibdev2netdev ibv_devinfo rdma)
if (( INSTALL )); then
  sudo apt-get update
  sudo apt-get install -y git rsync curl jq ca-certificates python3 rdma-core ibverbs-utils nfs-common nfs-kernel-server
fi
missing=0
for c in "${required[@]}"; do
  if command -v "$c" >/dev/null 2>&1; then printf 'OK   %s -> %s\n' "$c" "$(command -v "$c")";
  else printf 'MISS %s\n' "$c"; missing=1; fi
done
printf '\nGPU:\n'; nvidia-smi -L || true
printf '\nRDMA/netdev mapping:\n'; ibdev2netdev || true
printf '\nRDMA links:\n'; rdma link || true
exit "$missing"
