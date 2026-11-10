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
