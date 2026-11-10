"""Your rig profile (guitars, how you listen, taste), used to make generated tones fit *your* setup.

Stored as JSON outside the repository: ``%APPDATA%\\ge200\\rig.json`` on Windows, ``~/.config/ge200/rig.json``
elsewhere, or the path in ``GE200_RIG``. Nothing here is sent anywhere except inside the tone-generator prompt.

Shape: ``{"guitars": [{name, strings, scale, body, neck_pickup, bridge_pickup, pickup_notes, tuning, notes}, ...],
listening, styles, likes, dislikes, notes}``. The older single-guitar layout (``guitar``, ``neck_pickup``... at the top
level) is migrated automatically.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional

GUITAR_FIELDS = {
    "name": "Name (make / model)",
    "strings": "Strings (count, gauge)",
    "scale": "Scale length",
    "body": "Body / neck / fingerboard, bridge",
    "neck_pickup": "Neck pickup (model, type)",
    "bridge_pickup": "Bridge pickup (model, type)",
    "pickup_notes": "Switching / coil tap / active or passive / boost in front",
    "tuning": "Tuning you play it in",
    "notes": "Notes (what it does well or badly)",
}
GENERAL_FIELDS = {
    "listening": "How you listen (headphones, FRFR speaker, studio monitors, amp power section...)",
    "styles": "Styles / artists you play",
    "likes": "Tones you like",
    "dislikes": "Things you dislike (e.g. harsh highs, flubby low end)",
    "notes": "Anything else the generator should know",
}
# kept for backwards compatibility with older code that listed the flat fields
TEXT_FIELDS = dict(GENERAL_FIELDS)
MAX_LEN = 400
MAX_GUITARS = 8
_LEGACY = {"guitar": "name", "neck_pickup": "neck_pickup", "bridge_pickup": "bridge_pickup",
           "pickup_notes": "pickup_notes", "strings_tuning": "tuning"}


class RigError(ValueError):
    pass


def default_path() -> Path:
    env = os.environ.get("GE200_RIG")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "ge200" / "rig.json"
    return Path.home() / ".config" / "ge200" / "rig.json"


def _text(value, what: str) -> str:
    if not isinstance(value, str):
        raise RigError(f"{what} must be text")
    value = " ".join(value.split())
    if len(value) > MAX_LEN:
        raise RigError(f"{what} is too long ({MAX_LEN} characters at most)")
    return value


def _clean_guitar(g: dict) -> dict:
    if not isinstance(g, dict):
        raise RigError("each guitar must be an object")
    unknown = set(g) - set(GUITAR_FIELDS)
    if unknown:
        raise RigError(f"unknown guitar fields: {sorted(unknown)}")
    out = {k: _text(g.get(k, ""), k) for k in GUITAR_FIELDS}
    return {k: v for k, v in out.items() if v}


def _migrate(rig: dict) -> dict:
    """Old layout (one guitar at the top level) -> guitars list."""
    if "guitars" in rig or not (set(rig) & set(_LEGACY)):
        return rig
    rig = dict(rig)
    g = {new: rig.pop(old) for old, new in _LEGACY.items() if old in rig}
    rig["guitars"] = [g]
    return rig


def clean(rig: dict) -> dict:
    """Validate and normalise a rig dict (unknown keys are rejected, text is trimmed and length-limited)."""
    if not isinstance(rig, dict):
        raise RigError("rig must be an object")
    rig = _migrate(rig)
    unknown = set(rig) - set(GENERAL_FIELDS) - {"guitars"}
    if unknown:
        raise RigError(f"unknown rig fields: {sorted(unknown)}")
    out: dict = {}
    guitars = rig.get("guitars", [])
    if not isinstance(guitars, list) or len(guitars) > MAX_GUITARS:
        raise RigError(f"guitars must be a list of at most {MAX_GUITARS} entries")
    cleaned = [g for g in (_clean_guitar(g) for g in guitars) if g]
    if cleaned:
        out["guitars"] = cleaned
    for key in GENERAL_FIELDS:
        v = _text(rig.get(key, ""), key)
        if v:
            out[key] = v
    return out


def load(path: Optional[Path] = None) -> dict:
    path = path or default_path()
    try:
        return clean(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RigError):
        return {}


def save(rig: dict, path: Optional[Path] = None) -> dict:
    rig = clean(rig)
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rig, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)               # atomic
    return rig


def guitar_names(rig: dict) -> List[str]:
    return [g.get("name") or f"Guitar {i + 1}" for i, g in enumerate(rig.get("guitars", []))]


def describe_for_prompt(rig: dict) -> str:
    """The rig as a short block of text for the LLM (empty string if nothing was filled in)."""
    if not rig:
        return ""
    lines = []
    for i, g in enumerate(rig.get("guitars", [])):
        parts = [f"{GUITAR_FIELDS[k].split(' (')[0]}: {v}" for k, v in g.items() if k != "name"]
        lines.append(f"- Guitar {i + 1}: {g.get('name', '(unnamed)')}" + ("; " + "; ".join(parts) if parts else ""))
    labels = {"listening": "Listening setup", "styles": "Styles / artists", "likes": "Likes",
              "dislikes": "Dislikes", "notes": "Notes"}
    lines += [f"- {labels[k]}: {rig[k]}" for k in labels if k in rig]
    return "\n".join(lines)
