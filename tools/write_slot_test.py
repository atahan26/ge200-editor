"""EXPERIMENT (writes to the device): test CMD 0xE5 = [slot index][512-byte preset] as "write preset into a slot"
(deduced from the vendor editor's preset-import path; same layout as the device's own 0xA5 dump frame).
Verifies by re-reading, and ALWAYS writes the slot's original content back afterwards.

Usage:
  python tools/write_slot_test.py SLOT noop     # write the slot's own content back (no change expected)
  python tools/write_slot_test.py SLOT param    # change one DS parameter byte, verify it was stored, then restore
"""
import sys
import time

from ge200 import protocol as p
from ge200.client import Ge200


def send(dev: Ge200, cmd: int, data: bytes, wait: float = 2.0) -> None:
    frame = p.build_frame(cmd, data)
    print(f"  send cmd {cmd:#04x}, {len(data)} data bytes, frame {len(frame)} bytes")
    dev.hid.start_read()
    for r in p.frame_to_reports(frame):
        dev.hid.write_report(r)
    time.sleep(wait)


def main() -> None:
    slot, mode = int(sys.argv[1], 0), sys.argv[2]
    with Ge200() as dev:
        before = dev.read_state()
        orig = before.presets[slot].raw
        print("active:", before.active_index, "| slot", slot, "name:", before.presets[slot].name)
        new = bytearray(orig)
        if mode == "param":
            new[38] = (orig[38] + 7) % 101      # DS/OD first parameter
        try:
            print("WRITE", mode)
            send(dev, 0xE5, bytes([slot]) + bytes(new))
            after = dev.read_state()
            print("  stored slot == sent:", after.presets[slot].raw == bytes(new),
                  "| == original:", after.presets[slot].raw == orig,
                  "| byte38 stored/sent:", after.presets[slot].raw[38], new[38])
            print("  other slots unchanged:",
                  all(after.presets[i].raw == before.presets[i].raw for i in before.presets if i != slot))
            print("  active preset still:", after.active_index)
        finally:
            print("RESTORE slot", slot)
            send(dev, 0xE5, bytes([slot]) + orig)
            final = dev.read_state()
            print("  all 200 presets identical to start:",
                  all(final.presets[i].raw == before.presets[i].raw for i in before.presets),
                  "| active:", final.active_index)


if __name__ == "__main__":
    main()
