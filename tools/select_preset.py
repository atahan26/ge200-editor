"""EXPERIMENT (writes to the device): select a preset (CMD 0xA6 + 1 byte index), verify by re-reading the
device state, then ALWAYS select the original preset again.

Usage: python tools/select_preset.py TARGET_INDEX
"""
import sys
import time

from ge200 import protocol as p
from ge200.client import Ge200


def select(dev: Ge200, idx: int) -> None:
    frame = p.build_frame(0xA6, bytes([idx]))
    print("  send", frame.hex(" "))
    dev.hid.start_read()
    for r in p.frame_to_reports(frame):
        dev.hid.write_report(r)
    time.sleep(0.7)


def live(snap):
    f = next((f.data for f in snap.frames if f.cmd == 0x86), None)
    return f


def main() -> None:
    target = int(sys.argv[1], 0)
    with Ge200() as dev:
        before = dev.read_state()
        orig = before.active_index
        print("active preset before:", orig, before.presets[orig].name)
        try:
            print("SELECT", target)
            select(dev, target)
            after = dev.read_state()
            print("  active now:", after.active_index, "| live buffer == preset[target]:",
                  live(after) == after.presets[target].raw,
                  "| name:", after.presets[target].name)
            print("  all stored presets unchanged:",
                  all(after.presets[i].raw == before.presets[i].raw for i in before.presets))
        finally:
            print("RESTORE", orig)
            select(dev, orig)
            final = dev.read_state()
            print("  active now:", final.active_index, "| restored OK:",
                  final.active_index == orig and live(final) == live(before))


if __name__ == "__main__":
    main()
