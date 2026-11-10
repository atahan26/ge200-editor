"""EXPERIMENT (writes to the device): test CMD 0x83 (512-byte preset payload, no slot index) as "save into the
ACTIVE slot". Selects SLOT, sends 0x83, verifies by re-reading, and always restores slot + active preset.

Usage:
  python tools/save_preset_test.py --write SLOT noop      # save the slot's own current content back (no change expected)
  python tools/save_preset_test.py SLOT rename    # change the name to 'GE200LIB TEST', verify, then restore
"""
import sys
import time

from ge200 import protocol as p
from ge200.client import Ge200
from ge200.preset import NAME_LENGTH, NAME_OFFSET


def send(dev: Ge200, cmd: int, data: bytes, wait: float = 1.0) -> None:
    frame = p.build_frame(cmd, data)
    print(f"  send cmd {cmd:#04x}, {len(data)} data bytes, frame {len(frame)} bytes")
    dev.hid.start_read()
    for r in p.frame_to_reports(frame):
        dev.hid.write_report(r)
    time.sleep(wait)


def main() -> None:
    if "--write" not in sys.argv:
        raise SystemExit("this experiment writes to your GE200 (and restores it afterwards): add --write to confirm. "
                         "Take a dump first: python -m ge200 dump backup.json --raw backup.bin")
    sys.argv.remove("--write")
    slot, mode = int(sys.argv[1], 0), sys.argv[2]
    with Ge200() as dev:
        before = dev.read_state()
        orig_active = before.active_index
        orig_raw = before.presets[slot].raw
        print("active before:", orig_active, "| slot", slot, "name:", before.presets[slot].name)
        try:
            print("SELECT", slot)
            send(dev, 0xA6, bytes([slot]), 0.7)
            new = bytearray(orig_raw)
            if mode == "rename":
                new[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH] = b"GE200LIB TEST".ljust(NAME_LENGTH, b"\x00")
            elif mode == "param":  # DS/OD first parameter (preset offset 38) -> a different value
                new[38] = (orig_raw[38] + 7) % 101
            print("SAVE", mode)
            send(dev, 0x83, bytes(new), 2.0)
            after = dev.read_state()
            live86 = next((f.data for f in after.frames if f.cmd == 0x86), b"")
            print("  slot name now:", after.presets[slot].name, "| active:", after.active_index)
            print("  live buffer (0x86) == sent:", live86 == bytes(new), "| byte38 live/stored/sent:",
                  live86[38], after.presets[slot].raw[38], new[38])
            print("  slot content == sent:", after.presets[slot].raw == bytes(new),
                  "| == original:", after.presets[slot].raw == orig_raw)
            print("  other slots unchanged:",
                  all(after.presets[i].raw == before.presets[i].raw for i in before.presets if i != slot))
        finally:
            print("RESTORE slot", slot)
            send(dev, 0xA6, bytes([slot]), 0.7)
            send(dev, 0x83, orig_raw, 2.0)
            print("RESTORE active", orig_active)
            send(dev, 0xA6, bytes([orig_active]), 0.7)
            final = dev.read_state()
            print("  all 200 presets identical to start:",
                  all(final.presets[i].raw == before.presets[i].raw for i in before.presets),
                  "| active:", final.active_index)


if __name__ == "__main__":
    main()
