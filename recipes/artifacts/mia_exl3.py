#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import read_text, repo_basename, shell_value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--recipe-id", required=True)
    args = ap.parse_args()
    src = Path(args.source)
    env = read_text(src / ".env.example")
    download = read_text(src / "download.sh")
    model_repo = shell_value(env, "HF_MODEL_REPO")
    engram_repo = shell_value(env, "HF_ENGRAM_REPO")
    expected = int(shell_value(env, "EXPECTED_SHARDS", "0") or 0)
    if not model_repo or not engram_repo or expected <= 0:
        raise SystemExit("mia_exl3 artifact provider could not derive HF_MODEL_REPO/HF_ENGRAM_REPO/EXPECTED_SHARDS")
    # The pinned downloader explicitly names the Engram subset. Derive it from the
    # current checked-out script instead of duplicating that list here.
    subset = sorted(set(re.findall(r'"(model-[0-9]{5}-of-[0-9]{5}\.safetensors|model\.safetensors\.index\.json|config\.json)"', download)))
    if not subset:
        raise SystemExit("mia_exl3 artifact provider could not derive Engram include list from download.sh")
    out = {
        "recipe": args.recipe_id,
        "artifacts": [
            {
                "kind": "huggingface",
                "format": "safetensors-directory",
                "repo": model_repo,
                "revision": "",
                "mode": "full",
                "expected_shards": expected,
                "legacy_names": [repo_basename(model_repo)],
            },
            {
                "kind": "huggingface",
                "format": "safetensors-directory",
                "repo": engram_repo,
                "revision": "",
                "mode": "subset",
                "files": subset,
                "legacy_names": [repo_basename(engram_repo)],
            },
        ],
    }
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
