"""Decoding of the 512-byte preset structure (see PROTOCOL.md, "Preset layout").

Only what has been verified is decoded; everything else stays available as raw bytes so that nothing
is lost on export.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

PRESET_SIZE = 512
NAME_OFFSET, NAME_LENGTH = 12, 16

# (module key, preset offset, block length in bytes incl. the two leading type/enable bytes)
MODULE_LAYOUT = [
    ("fx", 28, 6),
    ("ds", 36, 5),
    ("amp", 44, 8),
    ("cab", 52, 6),
    ("ns", 60, 5),
    ("eq", 68, 8),
    ("mod", 76, 6),
    ("delay", 84, 8),
    ("reverb", 92, 6),
]


class EditError(ValueError):
    """Raised when a requested preset edit is invalid."""


def apply_edit(raw: bytes, edit: dict) -> bytes:
    """Return a new 512-byte image with ``edit`` applied. Only fields we understand can be changed, everything
    else is copied untouched. ``edit`` keys (all optional): name, chain_order, modules{key:{type,enabled,params}}.
    """
    if len(raw) != PRESET_SIZE:
        raise EditError("preset image must be 512 bytes")
    unknown = set(edit) - {"name", "chain_order", "modules"}
    if unknown:
        raise EditError(f"unknown edit fields: {sorted(unknown)}")
    out = bytearray(raw)
    if "name" in edit:
        name = edit["name"]
        if not isinstance(name, str) or len(name) > NAME_LENGTH or not all(32 <= ord(c) <= 126 for c in name):
            raise EditError(f"name must be printable ASCII, at most {NAME_LENGTH} characters")
        out[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH] = name.encode("ascii").ljust(NAME_LENGTH, b"\x00")
    if "chain_order" in edit:
        order = edit["chain_order"]
        if sorted(order) != list(range(1, 10)):
            raise EditError("chain_order must be a permutation of 1..9")
        out[0:9] = bytes(order)
    layout = {k: (o, n) for k, o, n in MODULE_LAYOUT}
    for key, m in edit.get("modules", {}).items():
        if key not in layout:
            raise EditError(f"unknown module {key!r}")
        off, length = layout[key]
        if "type" in m:
            if not isinstance(m["type"], int) or not 1 <= m["type"] <= 255:
                raise EditError(f"{key}: type must be 1..255")
            out[off] = m["type"]
        if "enabled" in m:
            out[off + 1] = 1 if m["enabled"] else 0
        if "params" in m:
            params = m["params"]
            if len(params) != length - 2 or not all(isinstance(v, int) and 0 <= v <= 255 for v in params):
                raise EditError(f"{key}: params must be {length - 2} integers 0..255")
            out[off + 2:off + length] = bytes(params)
    return bytes(out)


def param_ranges(presets) -> Dict[str, dict]:
    """Observed (min, max) of the type byte and of each parameter byte over a set of presets (UI slider hints)."""
    result: Dict[str, dict] = {}
    for key, off, length in MODULE_LAYOUT:
        col = lambda i: [p.raw[off + i] for p in presets]  # noqa: E731
        result[key] = {
            "type": [min(col(0)), max(col(0))],
            "params": [[min(col(i)), max(col(i))] for i in range(2, length)],
        }
    return result


@dataclass
class Module:
    type: int          # 1-based model number (0 never seen)
    enabled: bool
    params: List[int]  # remaining bytes of the block, meaning depends on module/model (see PROTOCOL.md)


@dataclass
class Preset:
    index: int
    name: str
    chain_order: List[int]   # permutation of 1..9
    modules: Dict[str, Module]
    raw: bytes               # the full 512 bytes

    @classmethod
    def from_bytes(cls, index: int, raw: bytes) -> "Preset":
        raw = bytes(raw)
        if len(raw) != PRESET_SIZE:
            raise ValueError(f"preset must be {PRESET_SIZE} bytes, got {len(raw)}")
        name = raw[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH].split(b"\x00")[0].decode("latin-1").strip()
        modules = {
            key: Module(type=raw[off], enabled=bool(raw[off + 1]), params=list(raw[off + 2:off + length]))
            for key, off, length in MODULE_LAYOUT
        }
        return cls(index=index, name=name, chain_order=list(raw[0:9]), modules=modules, raw=raw)

    def to_dict(self, names: Optional[Dict[str, List[str]]] = None) -> dict:
        mods = {}
        for key, m in self.modules.items():
            d = {"type": m.type, "enabled": m.enabled, "params": m.params}
            table = (names or {}).get(key)
            if table and 1 <= m.type <= len(table):
                d["model"] = table[m.type - 1]
            mods[key] = d
        return {
            "index": self.index,
            "name": self.name,
            "chain_order": self.chain_order,
            "modules": mods,
            "raw": self.raw.hex(),
        }
