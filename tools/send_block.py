"""EXPERIMENT (writes to the device): send one module-state frame and print whatever comes back.

Format deduced from the vendor editor (state-push builders): CMD 0x90..0x99, payload = list of 16-bit
big-endian values ``[enable, type, params...]`` -- the same layout the device itself reports in the hello reply.

Usage:
  python tools/send_block.py --write CMD            # send back EXACTLY the values the device just reported (no-op)
  python tools/send_block.py CMD v0 v1 ...  # send explicit 16-bit values (a real change!)
"""
import sys
import time

from ge200 import protocol as p
from ge200.client import Ge200, Snapshot


def main() -> None:
    if "--write" not in sys.argv:
        raise SystemExit("this experiment writes to your GE200 (and restores it afterwards): add --write to confirm. "
                         "Take a dump first: python -m ge200 dump backup.json --raw backup.bin")
    sys.argv.remove("--write")
    cmd = int(sys.argv[1], 0)
    values = [int(x, 0) for x in sys.argv[2:]]
    with Ge200() as dev:
        snap = dev.read_state()
        current = next((f.data for f in snap.frames if f.cmd == cmd), None)
        if current is None:
            raise SystemExit(f"device did not report CMD {cmd:#x}")
        print("device state :", current.hex(" "))
        data = b"".join(v.to_bytes(2, "big") for v in values) if values else current
        frame = p.build_frame(cmd, data)
        print("sending      :", frame.hex(" "), "(no-op)" if data == current else "(CHANGE)")
        dev.hid.start_read()
        for r in p.frame_to_reports(frame):
            dev.hid.write_report(r)
        parser = p.StreamParser()
        t0 = time.time()
        while time.time() - t0 < 3:
            r = dev.hid.read_report(200)
            if r:
                for f in parser.feed_report(r):
                    print(f"reply        : cmd {f.cmd:#04x} crc_ok={f.crc_ok} data={f.data.hex(' ')}")
        print("done")


if __name__ == "__main__":
    main()
