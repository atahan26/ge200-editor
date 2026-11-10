# Roadmap

What is planned or open. Nothing on this page is built unless it says so.

## Portable NAM rig for the stage (a plan, work in progress)

**Status: only a plan.** Nothing is built or tested, and there is no code for it in this repository. (An offline "NAM lab" tab
existed earlier and was removed because it was not used.)

**Goal.** Play [Neural Amp Modeler](https://www.neuralampmodeler.com/) captures (for example from TONE3000) on stage without a
laptop, next to the GE200, and switch between the GE200's own sounds and the NAM sounds.

**The idea.** A Raspberry Pi 5 (4 GB is the author's guess, not measured) running a headless NAM host such as
[PiPedal](https://rerdavies.github.io/pipedal/) (phone as the controller), with a Focusrite Scarlett Solo (3rd gen) as the audio
interface. The GE200 stays as it is. Rough cost from a quick price check in early 2026: about 120 to 220 EUR for the Pi, power
supply, cooler and SD card (the Scarlett is already owned). Prices move a lot.

**Why not on the GE200 itself.** Its amp slots take Mooer's own capture format, not NAM. The firmware analysis suggests a small
processor and little memory (the newer hardware revision looks like an STM32H7-class part; which revision a given unit has was
not determined), so a NAM model would need custom firmware. That is not planned.

**Why not in Python on a PC.** Measured earlier: block processing in Python was several times slower than the real-time budget.
A native host works on a PC (the TONE3000 / NAM plugin in a DAW), but that is not part of this project.

**Open questions** (from web research, none verified in practice yet):
- Does PiPedal's cabinet IR plugin load your own WAV files, including 500 ms ones? (Not confirmed.)
- Real round-trip latency of a standard NAM model on a Pi 5 at a small buffer. (No reliable published numbers were found.)
- Does a Scarlett Solo (3rd gen) work as expected on a Pi 5? (Class compliant; Gen 3 may need its mass-storage mode switched off.)
- How to switch between a GE200 preset and a Pi scene without a PC: a phone page, a USB MIDI footswitch, or the unit itself.
  The Linux USB transport in this repository (`ge200/hid_linux.py`) has only been tested with fake I/O.

## Expression pedal

- Done and checked on 200 presets: the EXP / EXP2 assignment and MERGE. Not yet tried by moving the real pedal.
- Open: MERGE on the external pedal (EXP2) (where it is stored is not known), the pedal calibration, and the 0/1 flag at byte 11.

## Other open items

- Try the Claude-free AI providers (OpenAI, Gemini, OpenRouter, a local model) against the real services; today they are only
  tested against a local fake server.
- A real Linux / Raspberry Pi test of the HID transport.
