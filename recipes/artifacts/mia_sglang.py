#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import read_text, repo_basename, shell_value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--recipe-id", required=True)
    args = ap.parse_args()
    src = Path(args.source)
    start = read_text(src / "start.sh")
    repo = shell_value(start, "HF_REPO", "deepseek-ai/DeepSeek-V4.1-Flash")
    revision = shell_value(start, "HF_REVISION", "")
    expected = int(shell_value(start, "EXPECTED_SHARDS", "0") or 0)
    if not repo:
        raise SystemExit("mia_sglang artifact provider could not derive HF_REPO")
    print(json.dumps({
        "recipe": args.recipe_id,
        "artifacts": [{
            "kind": "huggingface",
            "format": "safetensors-directory",
            "repo": repo,
            "revision": revision,
            "mode": "full",
            "expected_shards": expected,
            "legacy_names": [repo_basename(repo), "DeepSeek-V4.1-Flash"],
        }],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
