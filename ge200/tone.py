"""LLM tone generator: turns a text description into a preset edit (stage 5).

The model never talks to the device. It proposes an *edit* (name, chain order, per-module type / enabled /
params) that is validated and clamped to the ranges seen in your own presets; the web editor then loads it into
the editor where you audition it and decide whether to save.

Any LLM back end works (Claude, OpenAI, Gemini, OpenRouter, or a local OpenAI-compatible server): see
``ge200.llm`` for the providers and the environment variables.

Privacy: the prompt contains the description you type, the base preset and a sample of your own presets
(names and settings), plus the model-name tables if you passed a local ``--names`` file and your rig text. All of
it is sent to the provider you choose; pick ``local`` to keep it on your machine.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from . import llm
from . import params as param_info
from . import rig as rig_mod
from .preset import (MODULE_LAYOUT, WIDE_FIELDS, WIDE_LIMITS, WIDE_MIN_SENSIBLE, EditError, Preset, apply_edit,
                     param_ranges)

DEFAULT_MODEL = llm.ANTHROPIC_DEFAULT_MODEL
MODULE_LABELS = {
    "fx": "FX (wah / compressor family)", "ds": "drive / overdrive / distortion", "amp": "amp model",
    "cab": "cabinet / IR", "ns": "noise suppressor", "eq": "equalizer", "mod": "modulation",
    "delay": "delay", "reverb": "reverb",
}
MODULE_KEYS = [k for k, _, _ in MODULE_LAYOUT]
PARAM_COUNT = {k: n - 2 for k, _, n in MODULE_LAYOUT}


class ToneError(RuntimeError):
    """The generator could not produce a usable edit (message is safe to show to the user)."""


def _schema() -> dict:
    module = {
        "type": "object",
        "properties": {
            "type": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "params": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["type", "enabled", "params"],
        "additionalProperties": False,
    }
    wide = {"type": "object", "properties": {k: {"type": "integer"} for k in WIDE_FIELDS},
            "required": list(WIDE_FIELDS), "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "notes": {"type": "string"},
            "chain_order": {"type": "array", "items": {"type": "integer"}},
            "modules": {"type": "object", "properties": {k: module for k in MODULE_KEYS},
                        "required": MODULE_KEYS, "additionalProperties": False},
            "wide": wide,
        },
        "required": ["name", "notes", "chain_order", "modules", "wide"],
        "additionalProperties": False,
    }


SCHEMA = _schema()


def available() -> dict:
    """Which providers are configured (see ``llm.overview``)."""
    return llm.overview()


def _compact(p: Preset) -> dict:
    return {"name": p.name, "chain_order": p.chain_order,
            "modules": {k: {"type": m.type, "enabled": m.enabled, "params": m.params} for k, m in p.modules.items()},
            "wide": p.wide}


def pick_examples(presets: Dict[int, Preset], count: int = 14) -> List[Preset]:
    """A stable, evenly spread sample of presets that have a real name (stable => the prompt can be cached)."""
    named = [presets[i] for i in sorted(presets) if presets[i].name and presets[i].name.upper() != "EMPTY"]
    if len(named) <= count:
        return named
    step = len(named) / count
    return [named[int(i * step)] for i in range(count)]


def build_system_prompt(presets: Dict[int, Preset], names: Optional[Dict[str, List[str]]] = None,
                        example_count: int = 14, rig: Optional[dict] = None) -> str:
    ranges = param_ranges(list(presets.values()))
    lines = [
        "You configure a Mooer GE200 guitar multi-effects unit by designing presets.",
        "",
        "A preset has nine modules in this fixed order: " + ", ".join(f"{k} ({MODULE_LABELS[k]})" for k in MODULE_KEYS) + ".",
        "Every module has: `type` (1-based model number), `enabled` (true/false) and `params` (a list of integers, "
        "usually 0-100). What each param does comes from the device's own knob labels, per model (the k-th label is "
        "params[k]):",
        param_info.describe_for_prompt(names),
        "Where a param is not mapped (delay, two cabinet knobs) keep it close to the base preset's value. "
        "Never go outside the observed ranges listed below.",
        "`chain_order` is a permutation of 1..9 giving the signal order; module ids are assumed to follow the order "
        "above (1=fx, 2=ds, 3=amp, 4=cab, 5=ns, 6=eq, 7=mod, 8=delay, 9=reverb). Change it only when it helps the sound.",
        "`name` is at most 16 printable ASCII characters (upper case is the convention).",
        "`notes` is one or two short sentences explaining the main choices, written for the guitarist.",
        "",
        "The description may name an artist, album or song. You cannot hear audio: use only what you actually know "
        "about that recording's guitar sound (amp family, gain level, pickup position, effects, era). If you do not "
        "really know it, say so in `notes` and fall back on the genre and era instead of inventing gear details. "
        "State the key assumptions you made in `notes`.",
        "",]
    if rig:
        lines += ["The guitarist's own rig (fit the tone to it: pickups change how much gain and treble are needed, "
                  "and the listening setup changes the cab and EQ choices):", rig_mod.describe_for_prompt(rig), ""]
    lines += [
        "Be conservative with output levels and gain staging: the result is auditioned on real hardware through "
        "real speakers, so avoid settings that would be much louder than the example presets. Disable modules "
        "the sound does not need.",
        "",
        "Observed ranges in this user's presets (type range; then min-max per param):",
    ]
    for k in MODULE_KEYS:
        r = ranges[k]
        lines.append(f"- {k}: type {r['type'][0]}-{r['type'][1]}; params " + ", ".join(f"{a}-{b}" for a, b in r["params"]))
    if names:
        lines += ["", "Model tables (number = `type`):"]
        for k in MODULE_KEYS:
            if names.get(k):
                lines.append(f"{k}: " + "; ".join(f"{i + 1}={n}" for i, n in enumerate(names[k])))
    lines += ["", "Example presets from this user's unit (compact JSON, one per line):"]
    for p in pick_examples(presets, example_count):
        lines.append(json.dumps(_compact(p), separators=(",", ":")))
    return "\n".join(lines)


def build_user_prompt(description: str, base: Preset, guitar: Optional[str] = None) -> str:
    which = (f"\n\nThe guitarist will play this preset with: {guitar} (see the rig above; its pickups, scale, string count "
             "and tuning matter: e.g. a 7-string in a low tuning needs a tighter low end and a lower low-cut than a "
             "6-string, and hot ceramic humbuckers need less drive than single coils)") if guitar else ""
    return ("Base preset (start from it and change what the description needs):\n"
            + json.dumps(_compact(base), separators=(",", ":"))
            + "\n\nTone description from the guitarist:\n" + description.strip() + which
            + "\n\nReturn the complete preset edit.")


def sanitize_edit(edit: dict, base: Preset, presets: Dict[int, Preset],
                  names: Optional[Dict[str, List[str]]] = None) -> dict:
    """Make a model answer safe: right shapes, ASCII name, values clamped to what the user's own presets use."""
    ranges = param_ranges(list(presets.values()))
    out: dict = {}
    name = "".join(c for c in str(edit.get("name", base.name)) if 32 <= ord(c) <= 126).strip()[:16]
    out["name"] = name or base.name
    order = edit.get("chain_order")
    out["chain_order"] = order if isinstance(order, list) and sorted(order) == list(range(1, 10)) else list(base.chain_order)
    mods = edit.get("modules") if isinstance(edit.get("modules"), dict) else {}
    out["modules"] = {}
    for key in MODULE_KEYS:
        b = base.modules[key]
        m = mods.get(key) if isinstance(mods.get(key), dict) else {}
        rg = ranges[key]
        table = (names or {}).get(key)
        max_type = len(table) if table else max(rg["type"][1], 1)
        t = m.get("type") if isinstance(m.get("type"), int) else b.type
        params = m.get("params") if isinstance(m.get("params"), list) else []
        fixed = []
        for i in range(PARAM_COUNT[key]):
            v = params[i] if i < len(params) and isinstance(params[i], int) else b.params[i]
            fixed.append(max(0, min(v, rg["params"][i][1])))
        out["modules"][key] = {
            "type": max(1, min(t, max_type)),
            "enabled": bool(m.get("enabled", b.enabled)),
            "params": fixed,
        }
    wide_in = edit.get("wide") if isinstance(edit.get("wide"), dict) else {}
    out["wide"] = {}
    for k in WIDE_FIELDS:
        lo, hi = max(WIDE_LIMITS[k][0], WIDE_MIN_SENSIBLE), WIDE_LIMITS[k][1]
        v = wide_in.get(k)
        v = v if isinstance(v, int) and not isinstance(v, bool) else base.wide[k]
        out["wide"][k] = v if v == base.wide[k] else max(lo, min(hi, v))     # untouched values stay exactly as they were
    apply_edit(base.raw, out)           # raises EditError if something is still wrong
    return out


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToneError(f"the model did not return valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ToneError("the model answer is not a JSON object")
    return data


def resolve_provider(provider=None, client=None, fallbacks: bool = True) -> "llm.Provider":
    if provider is not None and not isinstance(provider, str):
        return provider
    if client is not None:                      # an Anthropic-style client (tests inject a fake one)
        return llm.AnthropicProvider(client=client, fallbacks=fallbacks)
    try:
        return llm.get_provider(provider)
    except llm.LlmError as e:
        raise ToneError(str(e)) from e


def generate(description: str, base: Preset, presets: Dict[int, Preset], names=None, client=None,
             model: Optional[str] = None, effort: str = "high", fallbacks: bool = True,
             rig: Optional[dict] = None, guitar: Optional[str] = None, provider=None) -> dict:
    """Return ``{"edit": {...}, "notes": str}``; ``edit`` is valid input for ``apply_edit`` / the web editor.

    ``provider`` is a provider id ("anthropic", "openai", "gemini", "openrouter", "local"), a ``llm.Provider``
    object, or None for the configured default. ``client`` (an Anthropic SDK client) is kept for tests/back-compat.
    """
    description = (description or "").strip()
    if not description:
        raise ToneError("describe the tone you want")
    if len(description) > 1000:
        raise ToneError("the description is too long (1000 characters at most)")
    prov = resolve_provider(provider, client, fallbacks)
    model = (model or prov.default_model() or "").strip()
    if not model:
        raise ToneError(f"name a model for the {prov.label} provider (or set GE200_LLM_MODEL)")
    system = build_system_prompt(presets, names, rig=rig)
    user = build_user_prompt(description, base, guitar)
    last_error: Optional[Exception] = None
    for _ in range(2):          # one retry if the answer is unusable
        try:
            done = prov.complete(system, user, SCHEMA, model, effort)
            if done.refused:
                raise ToneError("the model declined this request")
            if done.truncated:
                raise ToneError("the answer was cut off (max tokens); try a shorter description or a larger limit")
            if not done.text:
                raise ToneError("the model returned no text")
            data = _parse_json(done.text)
            edit = sanitize_edit(data, base, presets, names)
            return {"edit": edit, "notes": str(data.get("notes", "")).strip()[:900]}
        except (ToneError, EditError) as e:
            last_error = e
        except llm.LlmError as e:
            raise ToneError(str(e)) from e
    raise ToneError(f"no usable answer from the model: {last_error}")
