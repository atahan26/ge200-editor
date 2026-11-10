"""LLM tone generator: turns a text description into a preset edit (stage 5).

The model never talks to the device. It proposes an *edit* (name, chain order, per-module type / enabled /
params) that is validated and clamped to the ranges seen in your own presets; the web editor then loads it into
the editor where you audition it and decide whether to save.

Privacy: the prompt contains the description you type, the base preset and a sample of your own presets
(names and settings), plus the model-name tables if you passed a local ``--names`` file. All of it is sent to
the Anthropic API. Needs ``pip install anthropic`` and credentials (``ANTHROPIC_API_KEY`` or an ``ant auth login``
profile).
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from . import params as param_info
from . import rig as rig_mod
from .preset import MODULE_LAYOUT, EditError, Preset, apply_edit, param_ranges

DEFAULT_MODEL = "claude-opus-5-5"
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
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "notes": {"type": "string"},
            "chain_order": {"type": "array", "items": {"type": "integer"}},
            "modules": {"type": "object", "properties": {k: module for k in MODULE_KEYS},
                        "required": MODULE_KEYS, "additionalProperties": False},
        },
        "required": ["name", "notes", "chain_order", "modules"],
        "additionalProperties": False,
    }


SCHEMA = _schema()


def available() -> dict:
    """Is the optional SDK installed, and does an API key look configured?"""
    from .env import load_env
    load_env()
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "the 'anthropic' package is not installed (pip install anthropic)"}
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {"available": True, "reason": "" if has_key else
            "no ANTHROPIC_API_KEY in the environment (an 'ant auth login' profile also works)"}


def _compact(p: Preset) -> dict:
    return {"name": p.name, "chain_order": p.chain_order,
            "modules": {k: {"type": m.type, "enabled": m.enabled, "params": m.params} for k, m in p.modules.items()}}


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
        "usually 0-100). What each param does, with how sure we are (confidence: high = confirmed, medium = consistent "
        "with the data, low = only a guess from the editor's label order, no tag = unknown):",
        param_info.describe_for_prompt(),
        "For params with low or unknown confidence, infer from the example presets below and keep them close to the "
        "base preset's values rather than guessing wildly. Never go outside the observed ranges listed below.",
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


def call_model(client, system: str, user: str, model: str = DEFAULT_MODEL, effort: str = "high",
               fallbacks: bool = True) -> dict:
    """One Claude request with a JSON-schema constrained answer."""
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if fallbacks:    # server-side fallback if a safety classifier declines (very unlikely for this task)
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        response = client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise ToneError("the model declined this request")
    if response.stop_reason == "max_tokens":
        raise ToneError("the answer was cut off (max_tokens); try a shorter description")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ToneError("the model returned no text")
    return _parse_json(text)


def default_client():
    from .env import load_env
    load_env()                                     # picks up ANTHROPIC_API_KEY from a git-ignored .env
    try:
        import anthropic
    except ImportError as e:
        raise ToneError("the 'anthropic' package is not installed (pip install anthropic)") from e
    return anthropic.Anthropic()


def generate(description: str, base: Preset, presets: Dict[int, Preset], names=None, client=None,
             model: str = DEFAULT_MODEL, effort: str = "high", fallbacks: bool = True,
             rig: Optional[dict] = None, guitar: Optional[str] = None) -> dict:
    """Return ``{"edit": {...}, "notes": str}``; ``edit`` is valid input for ``apply_edit`` / the web editor."""
    description = (description or "").strip()
    if not description:
        raise ToneError("describe the tone you want")
    if len(description) > 1000:
        raise ToneError("the description is too long (1000 characters at most)")
    client = client or default_client()
    system = build_system_prompt(presets, names, rig=rig)
    user = build_user_prompt(description, base, guitar)
    last_error: Optional[Exception] = None
    for _ in range(2):          # one retry if the answer is unusable
        try:
            data = call_model(client, system, user, model, effort, fallbacks)
            edit = sanitize_edit(data, base, presets, names)
            return {"edit": edit, "notes": str(data.get("notes", "")).strip()[:400]}
        except (ToneError, EditError) as e:
            last_error = e
        except Exception as e:      # SDK / network errors: translate, never leak keys
            raise ToneError(_explain_api_error(e)) from e
    raise ToneError(f"no usable answer from the model: {last_error}")


def _explain_api_error(e: Exception) -> str:
    try:
        import anthropic
    except ImportError:
        return f"{type(e).__name__}"
    if isinstance(e, anthropic.AuthenticationError):
        return "authentication failed: set ANTHROPIC_API_KEY (or run 'ant auth login')"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "the API key is not allowed to use this model"
    if isinstance(e, anthropic.RateLimitError):
        return "rate limited by the API, try again in a minute"
    if isinstance(e, anthropic.APIConnectionError):
        return "could not reach the Anthropic API (network problem)"
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {getattr(e, 'message', type(e).__name__)}"
    return f"{type(e).__name__}: {e}"
