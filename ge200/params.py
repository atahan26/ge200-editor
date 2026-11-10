"""What the preset parameter bytes mean (as far as we know), with an honest confidence level for each.

Sources: the label order the vendor editor uses for each module's knobs (static analysis) and statistics over a real
set of presets (e.g. in the drive block the third value is ~10-28 for boosts and 50-80 for distortions, i.e. GAIN).

confidence: ``high`` = label order from the editor AND confirmed by the data; ``medium`` = label order from the editor
and consistent with the data; ``low`` = only the order of the editor's label list or a guess; ``unknown`` = nothing.
"""
from __future__ import annotations

from typing import Dict, List

HIGH, MEDIUM, LOW, UNKNOWN = "high", "medium", "low", "unknown"


def _p(label: str, confidence: str, note: str = "") -> dict:
    return {"label": label, "confidence": confidence, "note": note}


PARAMS: Dict[str, List[dict]] = {
    "fx": [_p("P1", UNKNOWN), _p("P2", UNKNOWN), _p("P3", UNKNOWN), _p("P4", UNKNOWN)],
    "ds": [
        _p("LEVEL", HIGH, "output volume (the editor calls it VOLUME); usually 60-90"),
        _p("TONE", HIGH, "tone control, 50 = neutral"),
        _p("GAIN", HIGH, "drive amount: ~10-30 for boosts, 50-80 for distortions"),
    ],
    "amp": [
        _p("GAIN", MEDIUM, "preamp gain"),
        _p("BASS", MEDIUM), _p("MID", MEDIUM), _p("TREBLE", MEDIUM),
        _p("PRESENCE", MEDIUM),
        _p("MASTER", MEDIUM, "master volume; typically 70-95, i.e. loud: lower it for quiet auditioning"),
    ],
    "cab": [
        _p("MIC", HIGH, "microphone model number, 0..9"),
        _p("P2", LOW, "the editor lists CENTER and DISTANCE knobs for the cabinet; which of the two bytes is which is unknown"),
        _p("P3", LOW, "see above"),
        _p("POWER TUBE", HIGH, "0 = off, 1..4 = tube type"),
    ],
    "ns": [
        _p("THRESHOLD", LOW, "label order from the editor (THRES, SENS, ATTACK)"),
        _p("SENSITIVITY", LOW), _p("ATTACK", LOW),
    ],
    "eq": [_p(f"BAND {i} GAIN", MEDIUM, "0..24, about 10-12 is flat; the band frequencies depend on the EQ type (unknown)")
           for i in range(1, 7)],
    "mod": [_p("P1", UNKNOWN), _p("P2", UNKNOWN), _p("P3", UNKNOWN), _p("P4", UNKNOWN)],
    "delay": [_p("P1", UNKNOWN), _p("P2", UNKNOWN), _p("P3", UNKNOWN), _p("P4", UNKNOWN), _p("P5", UNKNOWN),
              _p("P6", UNKNOWN, "the editor lists LEVEL, F.BACK, TIME ... knobs; the delay time itself is a separate 16-bit value")],
    "reverb": [
        _p("PRE DELAY", LOW, "label order from the editor (PRE DELAY, LEVEL, DECAY, TONE)"),
        _p("LEVEL", LOW), _p("DECAY", LOW), _p("TONE", LOW),
    ],
}


def label(module: str, index: int) -> str:
    items = PARAMS.get(module, [])
    return items[index]["label"] if index < len(items) else f"P{index + 1}"


def describe_for_prompt() -> str:
    """One line per module for the LLM prompt, with the confidence spelled out."""
    lines = []
    for key, items in PARAMS.items():
        parts = []
        for i, it in enumerate(items):
            tag = "" if it["confidence"] == UNKNOWN else f" [{it['confidence']}]"
            hint = f" ({it['note']})" if it["note"] and it["confidence"] in (HIGH, MEDIUM) else ""
            parts.append(f"p{i}={it['label']}{tag}{hint}")
        lines.append(f"- {key}: " + ", ".join(parts))
    return "\n".join(lines)
