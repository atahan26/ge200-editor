"""EXPERIMENT (writes to the device): change ONE value of a module-state frame, re-read the device state to see
what changed (live edit buffer 0x86? stored preset 0xA5?), then ALWAYS write the original values back.

Usage: python tools/change_and_restore.py --write CMD VALUE_INDEX NEW_VALUE
Example: python tools/change_and_restore.py 0x91 0 0     # switch the DS/OD block off
"""
import sys
import time

from ge200 import protocol as p
from ge200.client import Ge200


def write(dev: Ge200, cmd: int, data: bytes) -> None:
    frame = p.build_frame(cmd, data)
    print("  send", frame.hex(" "))
    dev.hid.start_read()
    for r in p.frame_to_reports(frame):
        dev.hid.write_report(r)
    time.sleep(0.5)


def get(snap, cmd):
    return next((f.data for f in snap.frames if f.cmd == cmd), None)


def main() -> None:
    if "--write" not in sys.argv:
        raise SystemExit("this experiment writes to your GE200 (and restores it afterwards): add --write to confirm. "
                         "Take a dump first: python -m ge200 dump backup.json --raw backup.bin")
    sys.argv.remove("--write")
    cmd, idx, new = int(sys.argv[1], 0), int(sys.argv[2], 0), int(sys.argv[3], 0)
    with Ge200() as dev:
        before = dev.read_state()
        orig = get(before, cmd)
        active = before.active_index
        print("active preset:", active, "| original", hex(cmd), orig.hex(" "))
        vals = [int.from_bytes(orig[i:i + 2], "big") for i in range(0, len(orig), 2)]
        vals[idx] = new
        changed = b"".join(v.to_bytes(2, "big") for v in vals)
        try:
            print("CHANGE:")
            write(dev, cmd, changed)
            after = dev.read_state()
            print("  state frame now:", (get(after, cmd) or b"").hex(" "))
            print("  live buffer 0x86 differs from before:", get(after, 0x86) != get(before, 0x86))
            print("  stored preset A5[active] differs from before:",
                  after.presets[active].raw != before.presets[active].raw)
        finally:
            print("RESTORE:")
            write(dev, cmd, orig)
            final = dev.read_state()
            print("  state frame now:", (get(final, cmd) or b"").hex(" "))
            print("  restored OK:", get(final, cmd) == orig and get(final, 0x86) == get(before, 0x86)
                  and final.presets[active].raw == before.presets[active].raw)


if __name__ == "__main__":
    main()
