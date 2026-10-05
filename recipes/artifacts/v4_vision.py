#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import read_text, repo_basename


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--recipe-id", required=True)
    args = ap.parse_args()
    src = Path(args.source)
    # CURRENT.md says the launchers are the source of truth for the model dir,
    # while README/vision-exp README identify the HF repository and pinned model revision.
    text = read_text(src / "README.md") + "\n" + read_text(src / "vision-exp" / "README.md")
    lm = read_text(src / "launchers" / "ds4-vision-tp2.sh")
    repo_match = re.search(r"\b(deepseek-ai/DeepSeek-V4-Flash-Vision-Exp)\b", text)
    if not repo_match:
        raise SystemExit("v4_vision artifact provider could not derive model repository from pinned source")
    repo = repo_match.group(1)
    rev_match = re.search(r"checkpoint pinned at commit [`']?([0-9a-f]{40})", text, flags=re.I)
    revision = rev_match.group(1) if rev_match else ""
    dir_match = re.search(r'MODEL_DIR="\$\{MODEL_DIR:-([^}"]+)\}"', lm)
    legacy = [repo_basename(repo)]
    if dir_match and dir_match.group(1) not in legacy:
        legacy.append(dir_match.group(1))
    print(json.dumps({
        "recipe": args.recipe_id,
        "artifacts": [{
            "kind": "huggingface",
            "format": "safetensors-directory",
            "repo": repo,
            "revision": revision,
            "mode": "full",
            "expected_shards": 0,
            "legacy_names": legacy,
        }],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
