from __future__ import annotations

import re
from pathlib import Path


def read_text(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"recipe artifact provider: missing pinned source file: {path}")
    return path.read_text(errors="replace")


def shell_value(text: str, key: str, default: str = "") -> str:
    # Read simple KEY=value or KEY="value" defaults from recipe shell/env files.
    m = re.search(rf"(?m)^[ \t]*{re.escape(key)}=(.*)$", text)
    if not m:
        return default
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if value.startswith("${"):
        # ${KEY:-default}
        dm = re.match(r"\$\{[^:}]+:-([^}]+)\}", value)
        if dm:
            value = dm.group(1)
    return value


def repo_basename(repo: str) -> str:
    return repo.rstrip("/").rsplit("/", 1)[-1]
