# ge200 (unofficial, read-only)

Small Python library and CLI that reads the state of a **Mooer GE200** over USB (Windows, stdlib only).
It is an independent project, not affiliated with or endorsed by Mooer.

**Status: reading is the default; writing is opt-in.** Reading only sends the vendor editor's own hello
(`CMD 0xE0`); the device answers with its full state (200 presets, active preset, firmware string).
Three write commands (select a preset, load the working copy, store a preset in a slot) were each tested on
a real device and need `--write` on the CLI / `allow_write=True` in the library. There is no firmware code,
on purpose. What is verified and what is only inferred is tracked in [PROTOCOL.md](PROTOCOL.md).
**Writing changes persistent preset memory: take a `dump` first.**

## Usage
```
python -m ge200 devices                    # list the HID interface (no I/O)
python -m ge200 info                       # firmware string, number of presets, active preset
python -m ge200 list
python -m ge200 show 162
python -m ge200 dump presets.json --raw dump.bin
python -m ge200 --from-raw dump.bin list   # work offline on a saved raw dump
python -m ge200 select 12 --write          # make preset 12 active
python -m ge200 write-slot 199 presets.json --write   # store the preset with index 199 from a dump into slot 199
```
## Web editor
```
python -m ge200.web --backup-dir C:\path\to\backups [--names model_names.json] [--port 8200]
python -m ge200.web --backup-dir ... --demo-raw dump.bin     # try the UI on a saved dump, no device
```
Open http://127.0.0.1:8200/. Pick a preset (this selects it on the device), edit the name, chain order and each
module (on/off, model, parameters). With "live audition" on, edits go to the device's working copy only
(about 0.35 s after you stop moving a slider); **Save** stores the preset in its slot, verifies it by reading
it back, and writes a full JSON backup first (once per session). **Revert** reloads the stored preset.
Reading the whole device takes about 16 s (the device streams 12 presets per second), so startup and
**Refresh** take that long; a save returns in about a second and is not re-read automatically (press Refresh to
verify). While a read is running, writes wait for it to finish: writing during the stream can wedge the device's
input parser (see PROTOCOL.md).
The server only listens on 127.0.0.1, checks the Host header and requires a per-run token for every change.
Parameter meanings are not decoded yet, so sliders are labelled P1..Pn (raw bytes in order).

## Tone generator (optional, uses the Claude API)
In the web editor, type a description ("dreamy clean with a long shimmery reverb") and press **Generate**.
Claude (`claude-opus-5-5`, with the server-side refusal fallback enabled) gets your description, the current
preset and a stable sample of your own presets, and answers with a preset edit as JSON. The edit is
validated and **clamped to the ranges your own presets use**, loaded into the editor, and *not* sent to the
device until you press **Audition now** (turn your volume down first: levels are unverified) or move a control;
nothing is stored until you press Save.
```
pip install anthropic          # or: pip install .[llm]
set ANTHROPIC_API_KEY=...      # or run `ant auth login`
```
Privacy: the description, the base preset, about a dozen of your presets (names and settings) and, if you
pass `--names`, the model-name tables are sent to the Anthropic API. Each request costs a few cents; the stable
part of the prompt (~3k tokens) is marked for prompt caching.

### My rig
**My rig** (header) stores your guitar, pickups, listening setup and taste in `%APPDATA%\ge200\rig.json` (outside the
repo; override with `GE200_RIG`). It is added to the tone generator's prompt so tones fit *your* setup. Parameter
meanings with confidence levels live in `ge200/params.py` (sliders show the label, an italic `?` marks guesses).

## NAM lab (second engine)
NAM (Neural Amp Modeler) captures run in a **separate environment** (`.venv-nam`: PyTorch + neural-amp-modeler), so the
main environment stays light. Start the editor with your folders and use the **NAM lab** tab to render a DI recording
through a capture and an optional cabinet IR:
```
python -m ge200.web --backup-dir ... --nam-dir D:\nam --ir-dir D:\ir --di-dir D:\di
python -m ge200.nam inspect model.nam          # metadata
python -m ge200.nam render model.nam di.wav out.wav --ir cab.wav
python -m ge200.nam bench model.nam            # speed on this machine
```
A standard-size WaveNet renders about 10x faster than real time on a laptop CPU. Live playing through Python is
**not** practical yet (block processing took 9-13 ms per 2.7 ms block); use a native NAM host for that, with the GE200
as the USB audio interface. `ge200.ir` already converts IR wav files into the device's IR-slot format (512 samples,
24 bit); uploading them to the unit is implemented as frames only and has **not** been tried on a device yet.

## Model names
Model names (amp/cab/effect names) are vendor data and are not shipped. If you own the official editor you
can build a local table with `tools/extract_names.py` and pass it with `--names`. Type numbers are
1-based table positions; that mapping is inferred, not verified on every module.

Dumps contain your own presets: keep them out of public repositories.

## Tests
```
python -m unittest discover -s tests -t .
```
Tests use synthetic data only.

## Provenance
The device, the goals, the hardware tests and the decisions are the owner's. The protocol analysis
(static analysis of the vendor editor, USB captures) and all the code in this repository were written by
Claude Code (Anthropic) under the owner's direction.
