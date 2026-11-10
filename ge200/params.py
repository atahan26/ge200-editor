"""What the preset parameter bytes mean, per module and per model.

Provenance of the label tables below: the knob labels the GE200 itself shows for each effect model, read from the
device firmware's own UI code (a table of "label pointers -> struct fields" per model, plus the jump tables that choose
the table from the model number), cross-checked against the vendor editor and against real presets. The k-th label of
a model is the k-th parameter byte of its module block (verified for the modulation module, where the firmware
copies the four bytes after the type/enable pair into the four label fields in order).

``None`` in a list = the knob is unused for that model (its byte just keeps a default, usually 50).

Mapping confidence: DS, AMP, REVERB, FX, NS, EQ, MOD labels come straight from the firmware tables. Delay: the label
set per model is from the firmware, the byte positions were derived from the firmware's value handlers (TWO-HEAD copies
byte 0x59 -> SUB A, 0x5b -> SUB B, offset 0x160 -> TIME B; DYNAMIC uses byte 0x5a as THRES) and from real presets
(p3 = note division, p2 always 0 because the time is a 16-bit value at preset offset 350). Not proven: that p0 is LEVEL
and p1 is F.BACK (editor label order), and the CENTER / DISTANCE order of the two cabinet knobs.
"""
from __future__ import annotations

from typing import Dict, List, Optional

HIGH, MEDIUM, LOW, UNKNOWN = "high", "medium", "low", "unknown"
FW = "firmware"       # source tag used in the notes

# ---------------------------------------------------------------------------------------------- per-model labels
# module -> {model number (1-based) -> [label or None, ...]}   (labels as the device shows them)
_FX = {1: ["Q", "POSITION", "PEAK", "LEVEL"], 2: ["Q", "POSITION", "PEAK", "LEVEL"],
       3: ["RATE", "RANGE", "PEAK", "LEVEL"], 4: ["RATE", "RANGE", "PEAK", "LEVEL"], 5: ["RATE", "RANGE", "PEAK", "LEVEL"],
       6: ["ATTACK", "SENS", "PEAK", "LEVEL"],
       7: ["ATTACK", "THRES", "RATIO", "LEVEL"], 8: ["ATTACK", "THRES", "RATIO", "LEVEL"]}
_NS = {1: ["THRES", None, None], 2: ["SENS", None, None], 3: ["ATTACK", "RELEASE", "THRES"]}
_EQ = {1: ["100HZ", "250HZ", "630HZ", "1.6KHZ", "4KHZ", None],
       2: ["80HZ", "240HZ", "750HZ", "2.2KHZ", "6.6KHZ", None],
       3: ["100HZ", "200HZ", "400HZ", "800HZ", "1.6KHZ", "3.2KHZ"]}      # 4 = CUSTOM EQ: frequencies are user-set
_MOD_FAMILY = {
    (1, 2, 3): ["RATE", "LEVEL", "DEPTH", None],
    (4, 5): ["RATE", "MIX", "FEEDBACK", None],
    (6, 7, 11, 14): ["RATE", "MIX", "TONE", None],
    (8,): ["RATE", "DEPTH", "TONE", None],
    (9, 10, 20): ["PITCH", "MIX", "TONE", None],
    (12, 13): ["RATE", "MIX", "TONE", "DEPTH"],
    (15,): ["RATE", "MIX", "Q", None],
    (16, 17): ["RATE", "MIX", "RANGE", None],
    (18,): ["RISE", "LEVEL", None, None],
    (19,): ["SAMPLE", "MIX", "BIT", None],
}
_MOD = {t: labels for types, labels in _MOD_FAMILY.items() for t in types}
_DS = {t: ["VOLUME", "TONE", "GAIN"] for t in range(1, 21)}
_AMP = {t: ["GAIN", "BASS", "MID", "TREBLE", "PRES", "MST"] for t in range(1, 66)}
_REVERB = {t: ["PRE DELAY", "LEVEL", "DECAY", "TONE"] for t in range(1, 8)}
# Delay: p2 is only a placeholder (always 0): the delay time itself is the 16-bit value `wide.delay_time` (ms).
# p3 = note division (0 = off, 1 = 1/4, 2 = 1/4T, 3 = 1/4D, 4 = 1/8, 5 = 1/8T, 6 = 1/8D, 7 = 1/16, 8 = 1/16T, 9 = 1/16D),
# p4 = threshold of the DYNAMIC delay (model 8), p5 = note division of head B of the TWO-HEAD delay (model 9).
_DELAY = {t: ["LEVEL", "F.BACK", "TIME", "SUB-D", None, None] for t in range(1, 8)}
_DELAY[8] = ["LEVEL", "F.BACK", "TIME", "SUB-D", "THRES", None]
_DELAY[9] = ["LEVEL", "F.BACK", "TIME A", "SUB A", None, "SUB B"]
_CAB = {t: ["MIC", "CENTER", "DISTANCE", "POWER TUBE"] for t in range(1, 37)}

MODEL_LABELS: Dict[str, Dict[int, List[Optional[str]]]] = {
    "fx": _FX, "ds": _DS, "amp": _AMP, "ns": _NS, "eq": _EQ, "mod": _MOD, "reverb": _REVERB,
    "delay": _DELAY, "cab": _CAB,
}

NOTE_DIVISIONS = ["OFF", "1/4", "1/4T", "1/4D", "1/8", "1/8T", "1/8D", "1/16", "1/16T", "1/16D"]

# 16-bit values stored outside the module blocks (see preset.WIDE_FIELDS)
WIDE_INFO = {
    "delay_time": {"label": "DELAY TIME", "unit": "ms", "applies": "delay (all models; TIME A of TWO-HEAD)",
                   "range": [20, 2000]},
    "delay_time_b": {"label": "DELAY TIME B", "unit": "ms", "applies": "delay model 9 (TWO-HEAD) only",
                     "range": [20, 2000]},
    "eq_freq_1": {"label": "EQ FREQ 1", "unit": "Hz", "applies": "EQ model 4 (CUSTOM EQ) only", "range": [20, 20000]},
    "eq_freq_2": {"label": "EQ FREQ 2", "unit": "Hz", "applies": "EQ model 4 (CUSTOM EQ) only", "range": [20, 20000]},
    "eq_freq_3": {"label": "EQ FREQ 3", "unit": "Hz", "applies": "EQ model 4 (CUSTOM EQ) only", "range": [20, 20000]},
}

# ---------------------------------------------------------------------------------------------- generic notes
def _p(label: str, confidence: str, note: str = "") -> dict:
    return {"label": label, "confidence": confidence, "note": note}


# fallback / extra knowledge per module position (used when a model has no firmware table or for notes)
PARAMS: Dict[str, List[dict]] = {
    "fx": [_p("P1", MEDIUM), _p("P2", MEDIUM), _p("P3", MEDIUM), _p("LEVEL", MEDIUM, "output level (last knob)")],
    "ds": [_p("VOLUME", HIGH, "output level, usually 60-90"), _p("TONE", HIGH, "50 = neutral"),
           _p("GAIN", HIGH, "drive amount: ~10-30 for boosts, 50-80 for distortions")],
    "amp": [_p("GAIN", HIGH), _p("BASS", HIGH), _p("MID", HIGH), _p("TREBLE", HIGH), _p("PRES", HIGH),
            _p("MST", HIGH, "master volume; typically 70-95, loud: lower it for quiet auditioning")],
"cab": [_p("MIC", HIGH, "microphone model number, 0..9"),
            _p("CENTER", MEDIUM, "mic position, 0 = centre of the cone; order CENTER, DISTANCE is the editor's, not proven"),
            _p("DISTANCE", MEDIUM, "mic distance"),
            _p("POWER TUBE", HIGH, "0 = off, 1..4 = tube type")],
    "ns": [_p("P1", MEDIUM), _p("P2", MEDIUM), _p("P3", MEDIUM)],
    "eq": [_p(f"BAND {i}", MEDIUM, "gain, 0..24, about 10-12 is flat") for i in range(1, 7)],
    "mod": [_p("P1", MEDIUM), _p("P2", MEDIUM), _p("P3", MEDIUM), _p("P4", MEDIUM)],
    "delay": [_p("LEVEL", MEDIUM), _p("F.BACK", MEDIUM), _p("TIME", MEDIUM, "placeholder: the time is wide.delay_time"),
              _p("SUB-D", HIGH, "note division 0..9: OFF,1/4,1/4T,1/4D,1/8,1/8T,1/8D,1/16,1/16T,1/16D"),
              _p("THRES", MEDIUM, "DYNAMIC delay only"), _p("SUB B", MEDIUM, "TWO-HEAD delay only")],
    "reverb": [_p("PRE DELAY", HIGH), _p("LEVEL", HIGH), _p("DECAY", HIGH), _p("TONE", HIGH)],
}


def model_labels(module: str, model: int) -> Optional[List[Optional[str]]]:
    """The device's own knob labels for this model (``None`` entries = unused knob), or None if unknown."""
    return MODEL_LABELS.get(module, {}).get(model)


def label(module: str, index: int, model: Optional[int] = None) -> str:
    """Best label for parameter ``index`` of ``module`` (model-specific when the model is known)."""
    if model is not None:
        labels = model_labels(module, model)
        if labels is not None and index < len(labels):
            return labels[index] or "(unused)"
    items = PARAMS.get(module, [])
    return items[index]["label"] if index < len(items) else f"P{index + 1}"


def confidence(module: str, index: int, model: Optional[int] = None) -> str:
    if model is not None and model_labels(module, model) is not None:
        return HIGH
    items = PARAMS.get(module, [])
    return items[index]["confidence"] if index < len(items) else UNKNOWN


def describe_for_prompt(names: Optional[Dict[str, List[str]]] = None) -> str:
    """Parameter meanings for the LLM prompt. Model-specific tables come from the device firmware."""
    lines = ["(labels are the device's own knob names; '-' = unused knob, keep it at its base value)"]
    for key in ("fx", "ds", "amp", "cab", "ns", "eq", "mod", "delay", "reverb"):
        table = MODEL_LABELS[key]
        groups: Dict[tuple, List[int]] = {}
        for t, labs in table.items():
            groups.setdefault(tuple(labs), []).append(t)
        if len(groups) == 1:
            labs = next(iter(groups))
            lines.append(f"- {key}: all models: " + ", ".join(f"p{i}={l or '-'}" for i, l in enumerate(labs)))
            continue
        lines.append(f"- {key}:")
        for labs, types in sorted(groups.items(), key=lambda kv: kv[1][0]):
            tn = ", ".join(f"{t}" + (f"={names[key][t - 1]}" if names and names.get(key) and t - 1 < len(names[key]) else "")
                           for t in types)
            lines.append(f"    models {tn}: " + ", ".join(f"p{i}={l or '-'}" for i, l in enumerate(labs)))
    lines.append("- cab: MIC = microphone model 0-9, POWER TUBE 0 = off / 1-4 = tube type; CENTER/DISTANCE order is from the editor")
    lines.append("- delay: p2 is only a placeholder (always 0): the delay time is `wide.delay_time` in ms (typical 120-1000; "
                 "`wide.delay_time_b` only for model 9 TWO-HEAD). SUB-D is the note division: 0=off, 1=1/4, 2=1/4T, 3=1/4D, "
                 "4=1/8, 5=1/8T, 6=1/8D, 7=1/16, 8=1/16T, 9=1/16D (keep 1 unless the tone calls for a rhythmic delay)")
    lines.append("- wide.eq_freq_1..3 (Hz) matter only for EQ model 4 (CUSTOM EQ); otherwise keep them at the base values")
    return "\n".join(lines)
