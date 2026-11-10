"""Read the metadata of ``.nam`` capture files (plain JSON) without any heavy dependency."""
from __future__ import annotations

import json
import os
from typing import Dict, List

DEFAULT_SAMPLE_RATE = 48000


class NamFileError(ValueError):
    pass


def inspect_nam(path: str) -> Dict:
    """Summarise one ``.nam`` file. Raises :class:`NamFileError` if it is not a NAM capture."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise NamFileError(f"cannot read {os.path.basename(path)}: {e}") from e
    if not isinstance(cfg, dict) or "architecture" not in cfg or "weights" not in cfg:
        raise NamFileError(f"{os.path.basename(path)} is not a .nam file")
    meta = cfg.get("metadata") if isinstance(cfg.get("metadata"), dict) else {}
    weights = cfg.get("weights")
    return {
        "file": os.path.basename(path),
        "path": path,
        "name": meta.get("name") or os.path.splitext(os.path.basename(path))[0],
        "architecture": cfg.get("architecture"),
        "version": cfg.get("version"),
        "sample_rate": int(cfg.get("sample_rate") or DEFAULT_SAMPLE_RATE),
        "weights": len(weights) if isinstance(weights, list) else None,
        "size_bytes": os.path.getsize(path),
        "modeled_by": meta.get("modeled_by"),
        "gear_make": meta.get("gear_make"),
        "gear_model": meta.get("gear_model"),
        "gear_type": meta.get("gear_type"),
        "tone_type": meta.get("tone_type"),
        "input_level_dbu": meta.get("input_level_dbu"),
        "output_level_dbu": meta.get("output_level_dbu"),
        "loudness": meta.get("loudness"),
    }


def scan_library(directory: str) -> List[Dict]:
    """All readable ``.nam`` files under ``directory`` (non-recursive sub-folders included), sorted by name."""
    out: List[Dict] = []
    for root, _dirs, files in os.walk(directory):
        for fn in files:
            if fn.lower().endswith(".nam"):
                try:
                    out.append(inspect_nam(os.path.join(root, fn)))
                except NamFileError:
                    continue
    return sorted(out, key=lambda d: d["name"].lower())
