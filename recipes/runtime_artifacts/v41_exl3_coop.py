#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

STOCK_SHA = "ccdc69bfa04bff4870c3e555736a990fde6448ddb329441c4e0a27d6fc41078d"
BINARY_SHA = "9a9c44f0e423e3cfe595f195925e520af8e1b5bc56814fcaefccda87a4e983ae"
RUNTIME_SHA = "20c1bbf2663f61843c6c46de803bb134243c62799e3762ca3f5afe28c533491a"
SOURCE_REF = "11f1db19a0d0580ae17c95abe3c4f05c1e897175"
IMAGE_DIGEST = "sha256:2f0cf3adc0f989c1d446be274df864eb799630175f604c3b22b71b7205971dce"
IMAGE_REF = (
    "ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks"
    f"@{IMAGE_DIGEST}"
)
# Legacy graphdriver Docker reports the image config digest as .Id.
# Containerd-backed Docker reports the target/index/manifest digest instead.
IMAGE_LEGACY_CONFIG_ID = "sha256:4cdba4e946da2d19bf5b5a20c6d3a1a4bf421fa4d6db5082f271a986168176cb"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--recipe-id", required=True)
    args = p.parse_args()

    stock = args.source / "overlay" / "exl3.py"
    if not stock.is_file():
        raise SystemExit(f"missing pinned stock overlay: {stock}")
    actual = sha256(stock)
    if actual != STOCK_SHA:
        raise SystemExit(
            "cooperative-MoE runtime pin is incompatible with the current stock EXL3 overlay: "
            f"expected {STOCK_SHA}, got {actual}"
        )

    manifest = {
        "recipe": args.recipe_id,
        "runtime_artifacts": [
            {
                "id": "v41-exl3-cooperative-moe",
                "kind": "native-library",
                "name": "cooperative_moe.so",
                "sha256": BINARY_SHA,
                "runtime_py": {
                    "path": "extensions/cooperative_moe/runtime.py",
                    "sha256": RUNTIME_SHA,
                },
                "source": {
                    "type": "git",
                    "repo": "https://github.com/todoriri/DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks.git",
                    "ref": SOURCE_REF,
                    "binary_path": "extensions/cooperative_moe/artifacts/cooperative_moe.so",
                    "runtime_path": "extensions/cooperative_moe/runtime.py",
                    "generator_path": "extensions/cooperative_moe/prepare_profile.py",
                    "stock_path": "overlay/exl3.py",
                    "gate_path": "extensions/cooperative_moe/test_cuda_integration.py",
                    "gate_support_path": "tests/test_exl3_overlay.py",
                    "provenance_path": "extensions/cooperative_moe/artifacts/cooperative_moe-build.log",
                },
                "pins": {
                    "stock_sha256": STOCK_SHA,
                    "binary_sha256": BINARY_SHA,
                    "runtime_sha256": RUNTIME_SHA,
                },
                "stage": {
                    "host_template": "{home}/.cache/vllm-dsv41-flash-exl3/coop/{sha12}",
                    "container_template": "/root/.cache/vllm/coop/{sha12}",
                    "overlay_name": "exl3-cooperative.py",
                },
                "image": {
                    "reference": IMAGE_REF,
                    "digest": IMAGE_DIGEST,
                    "legacy_config_id": IMAGE_LEGACY_CONFIG_ID,
                },
                "gate": {
                    "checks": 54,
                    "conflicting_containers": ["dsv41-exl3-head", "dsv41-exl3-worker"],
                },
                "activation": {
                    "overlay_env": "DGX_COOP_OVERLAY_HOST",
                    "env": {
                        "DGX_COOP_TEMP_ROWS_FUSED": "8",
                        "DGX_COOP_IMAGE": IMAGE_REF,
                    },
                },
            }
        ],
    }
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
