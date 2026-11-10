# AGENTS.md: guide for AI coding agents

This file is for any coding agent (Claude Code, Codex, Cursor, Copilot, ...) working in this repository.
Read `README.md` for the user-facing description and `docs/USAGE.md` for the features. Everything below is what you
need to change the code safely.

## What this is
An unofficial Python toolkit for the **Mooer GE200** multi-effects pedal: a USB HID protocol client (reverse-engineered,
see `PROTOCOL.md`), a CLI, a local web editor drawn as a pedalboard, an LLM tone generator (any provider), IR import,
expression-pedal assignment and MERGE, and an offline NAM render lab. Python 3.9+. The core needs only the standard
library; numpy/scipy/soundfile (IRs, NAM) and `anthropic` (optional) are extras.

## Run
```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[audio,llm]"
.venv\Scripts\python.exe -m unittest discover -s tests -t .      # synthetic data only, no device needed
.venv\Scripts\python.exe -m ge200.web --backup-dir backups --demo-raw dump.bin   # the editor on a saved dump, no device
```
Without a unit, use `--demo-raw` (a saved `dump --raw` file) or `--from-raw` on the CLI.

## Map
- `ge200/protocol.py`: framing, CRC, report chunking, stream parser. `ge200/hid_win.py`, `hid_linux.py`, `hid.py`: HID transports.
- `ge200/client.py`: `Ge200` (read state, select, load working copy, store a slot, upload IRs), `Snapshot`.
- `ge200/preset.py`: the 512-byte preset layout: modules, wide values, expression pedal (`EXP_*`) and MERGE (`MERGE_*`), `apply_edit`.
- `ge200/params.py`: knob labels per model (derived from the firmware), confidence tables.
- `ge200/web/server.py` + `static/index.html`: the editor (localhost only, per-run token). `irlab.py`, `namlab.py`: IR import, NAM lab.
- `ge200/llm.py`, `tone.py`: provider layer and the tone generator. `rig.py`, `env.py`: rig profile, `.env` loader.
- `ge200/ir.py`, `ge200/nam/`: IR format conversion, NAM render tools (run in a separate environment).
- `tools/`: one-off experiments, **several write to the device** (read each file's docstring first).
- `PROTOCOL.md`: what is verified and what is only inferred. Keep it honest when you learn something.

## Common tasks
- **New preset field:** find its bytes (compare state frames or dumps, see `PROTOCOL.md`), add it to `preset.py`
  (`from_bytes`, `to_dict`, validation in `apply_edit`), add a no-op round-trip check on real-looking data in `tests/`, document it.
- **New LLM provider:** one small class in `ge200/llm.py` (`complete`, `list_models`), registered in `providers()`; test it
  against the fake HTTP server in `tests/test_llm.py`.
- **UI change:** `ge200/web/static/index.html` is one file (plain HTML, CSS, JavaScript, no build step).

## Safety rules (follow these)
1. **Never write to a connected unit without the user's explicit confirmation.** Reading (hello) is fine. Writes are:
   select, live load, store slot, IR upload/clear. IR uploads cannot be undone (the unit cannot return the old IR).
2. **Never flash firmware or run the vendor updater**, and never add code that does. The repo deliberately has none.
3. Only one program can talk to the unit at a time: stop the web editor before running a script that opens the device.
4. **Never commit** device dumps (`dumps/`, `*.bin`, `*.json` dumps), USB captures, vendor files (`*.mr`, the vendor editor,
   model-name tables), `.env`, `llm.json`, `rig.json`, API keys or personal paths. They are in `.gitignore`; keep it that way.
5. The web server must stay bound to `127.0.0.1` and keep the per-run token and Host check. Keys and server addresses
   for AI providers are never sent to the page and the page can never choose a URL.
6. Do not add telemetry or network calls beyond the AI provider the user picked.
7. Do not present guesses as facts: mark them as inferred in `PROTOCOL.md` and the UI.

## Style
Small, readable Python with type hints and `from __future__ import annotations`; standard library first. Tests use
synthetic data (`unittest`). Match the surrounding code.
