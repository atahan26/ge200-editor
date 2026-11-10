"""Decoding of the 512-byte preset structure (see PROTOCOL.md, "Preset layout").

Only what has been verified is decoded; everything else stays available as raw bytes so that nothing
is lost on export.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    unknown = set(edit) - {"name", "chain_order", "modules", "wide", "exp"}
    if unknown:
        raise EditError(f"unknown edit fields: {sorted(unknown)}")
    out = bytearray(raw)
    if "name" in edit:
        name = edit["name"]
        if not isinstance(name, str) or len(name) > NAME_LENGTH or not all(32 <= ord(c) <= 126 for c in name):
            raise EditError(f"name must be printable ASCII, at most {NAME_LENGTH} characters")
        current = raw[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH].split(b"\x00")[0].decode("latin-1").strip()
        if name != current:          # an unchanged name is not rewritten: the field keeps its stale bytes after the NUL
            out[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH] = name.encode("ascii").ljust(NAME_LENGTH, b"\x00")
    if "chain_order" in edit:
        order = edit["chain_order"]
        if sorted(order) != list(range(1, 10)):
            raise EditError("chain_order must be a permutation of 1..9")
        out[0:9] = bytes(order)
    for key, value in edit.get("wide", {}).items():
        if key not in WIDE_FIELDS:
            raise EditError(f"unknown wide field {key!r}")
        lo, hi = WIDE_LIMITS[key]
        if not isinstance(value, int) or not lo <= value <= hi:
            raise EditError(f"{key} must be an integer {lo}..{hi}")
        out[WIDE_FIELDS[key]:WIDE_FIELDS[key] + 2] = value.to_bytes(2, "little")
    exp = edit.get("exp")
    if exp is not None:
        if not isinstance(exp, dict) or set(exp) - set(EXP_FIELDS):
            raise EditError(f"exp must be an object with the keys {sorted(EXP_FIELDS)}")
        merged = {k: out[o] for k, o in EXP_FIELDS.items()}
        merged.update(exp)
        for n in ("exp1", "exp2"):
            if not exp_valid(merged[n + "_module"], merged[n + "_target"]):
                raise EditError(f"{n}: module must be 0..8 and target must exist in that module's list")
        for k, o in EXP_FIELDS.items():
            out[o] = merged[k]
    layout = {k: (o, n) for k, o, n in MODULE_LAYOUT}
    for key, m in edit.get("modules", {}).items():
        if key not in layout:
            raise EditError(f"unknown module {key!r}")
        off, length = layout[key]
        if "type" in m:
            if not isinstance(m["type"], int) or not 0 <= m["type"] <= 255 or (m["type"] == 0 and out[off] != 0):
                raise EditError(f"{key}: type must be 1..255")       # 0 is only accepted when it is already there
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


# 16-bit little-endian values that live outside the module blocks (found by matching the device's state frames with
# the stored preset image, then checked against real presets): name -> byte offset
WIDE_FIELDS = {
    "eq_freq_1": 344,      # Hz   (only used by the custom EQ, model 4; default 100)
    "eq_freq_2": 346,      # Hz   (default 600)
    "eq_freq_3": 348,      # Hz   (default 1250)
    "delay_time": 350,     # ms   (117..1066 in real presets)
    "delay_time_b": 352,   # ms   (second head of the TWO-HEAD delay, model 9)
}
# hard limits accepted by apply_edit (unused fields are 0 in real presets, so 0 must stay valid)
WIDE_LIMITS = {"eq_freq_1": (0, 20000), "eq_freq_2": (0, 20000), "eq_freq_3": (0, 20000),
               "delay_time": (0, 2000), "delay_time_b": (0, 2000)}
WIDE_MIN_SENSIBLE = 20      # a value the *editor or the generator* changes is kept at least this large


# Expression pedal assignment [V]. Found by lining up the device's 0xA3 state frame (whose bytes the vendor editor builds as
# [flag, EXP1 module-1, EXP1 target, EXP2 module-1, EXP2 target, ...]) with stored presets, then checked on 200 real
# presets: the target is a knob the module's current model really has in 114 of 115 non-default assignments, and no value
# is out of range. ``module`` is the index into MODULE_LAYOUT (0 = FX ... 8 = REVERB); ``target`` indexes the module's list
# below, which is the list the editor offers (names read from its EXP picture tables; in the editor every name appears
# twice, grey and orange, and only the first half is ever stored). Not decoded: MERGE (one pedal on several parameters,
# each with a heel and a toe value) and the pedal calibration.
EXP_FIELDS = {"exp1_module": 356, "exp1_target": 357, "exp2_module": 358, "exp2_target": 359}
EXP_TARGETS = {
    "fx": ["POSITION", "Q", "RATE", "ATTACK", "RANGE", "SENS", "THRE", "PEAK", "RATIO", "LEVEL"],
    "ds": ["VOLUME", "TONE", "GAIN"],
    "amp": ["GAIN", "BASS", "MID", "TREBLE", "PRES", "MST"],
    "cab": ["CENTER", "DISTANCE"],
    "ns": ["THRES", "SENS", "ATTACK", "RELEASE"],
    "eq": ["100HZ", "80HZ", "250HZ", "240HZ", "200HZ", "630HZ", "750HZ", "400HZ", "1.6KHZ", "2.2KHZ", "800HZ",
           "4KHZ", "6.6KHZ", "3.2KHZ"],
    "mod": ["RATE", "PITCH", "RISE", "SAMPLE", "MIX", "DEPTH", "LEVEL", "FEEDBACK", "TONE", "Q", "RANGE", "BIT"],
    "delay": ["LEVEL", "F.BACK", "TIME", "TIME A", "THRE", "TIME B"],
    "reverb": ["PRE DELAY", "LEVEL", "DECAY", "TONE"],
}
EXP_MODULE_KEYS = [k for k, _, _ in MODULE_LAYOUT]


def exp_valid(module: int, target: int) -> bool:
    return (isinstance(module, int) and isinstance(target, int) and not isinstance(module, bool)
            and not isinstance(target, bool) and 0 <= module < len(EXP_MODULE_KEYS)
            and 0 <= target < len(EXP_TARGETS[EXP_MODULE_KEYS[module]]))


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
    wide: Dict[str, int] = field(default_factory=dict)   # 16-bit values, see WIDE_FIELDS
    exp: Dict[str, int] = field(default_factory=dict)    # expression pedal assignment, see EXP_FIELDS

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
        wide = {k: int.from_bytes(raw[o:o + 2], "little") for k, o in WIDE_FIELDS.items()}
        exp = {k: raw[o] for k, o in EXP_FIELDS.items()}
        return cls(index=index, name=name, chain_order=list(raw[0:9]), modules=modules, raw=raw, wide=wide, exp=exp)

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
            "wide": dict(self.wide),
            "exp": dict(self.exp),
            "raw": self.raw.hex(),
        }
