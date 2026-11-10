"""Tiny ``.env`` loader (stdlib only) so API keys can live in a git-ignored file instead of the shell.

Looks for ``.env`` in the current directory and in the repository root. Values already present in the real
environment win. Lines look like ``KEY=value`` (optionally quoted); ``#`` starts a comment.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def load_env(paths: Optional[Iterable[Path]] = None) -> List[str]:
    """Load the first-found ``.env`` files into ``os.environ`` (never overriding). Returns the keys that were set."""
    if paths is None:
        paths = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    set_keys: List[str] = []
    for p in paths:
        try:
            values = parse(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        for k, v in values.items():
            if k not in os.environ:
                os.environ[k] = v
                set_keys.append(k)
    return set_keys
