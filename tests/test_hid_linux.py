"""Linux hidraw transport tests with fake I/O (they run on any OS; real hardware is not involved)."""
import os
import tempfile
import unittest
from pathlib import Path

from ge200 import protocol as p
from ge200.client import Ge200, Snapshot
from ge200.hid_linux import HidDevice, find_device_paths


class FakeIO:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.written = []
        self.closed = False

    def readable(self, timeout_s):
        return bool(self.incoming)

    def read(self, n):
        return self.incoming.pop(0)

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True


class FindTests(unittest.TestCase):
    def test_finds_only_matching_hidraw_nodes(self):
        with tempfile.TemporaryDirectory() as d:
            for name, hid_id in (("hidraw0", "0003:0000046D:0000C52B"), ("hidraw1", "0003:00000483:00005703"),
                                 ("hidraw2", "0003:00000483:00005740")):
                u = Path(d) / name / "device"
                u.mkdir(parents=True)
                (u / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=x\n", encoding="utf-8")
            (Path(d) / "hidraw3").mkdir()                                   # no uevent file: skipped
            self.assertEqual(find_device_paths(sysfs=d, dev="/dev"), ["/dev/hidraw1"])
            self.assertEqual(find_device_paths(sysfs=os.path.join(d, "missing")), [])


class TransportTests(unittest.TestCase):
    def test_write_and_read(self):
        report = bytes(range(64))
        io = FakeIO([report])
        dev = HidDevice(io=io)
        dev.write_report(bytes(64))
        self.assertEqual(io.written, [bytes(64)])
        self.assertEqual(dev.read_report(10), report)
        self.assertIsNone(dev.read_report(10))                               # nothing queued -> timeout
        with self.assertRaises(ValueError):
            dev.write_report(b"short")
        dev.close()
        self.assertTrue(io.closed)

    def test_client_reads_a_state_dump_through_the_linux_transport(self):
        """The Ge200 client works unchanged on top of this transport (fake device answers the hello)."""
        from tests.test_ge200 import synthetic_preset
        frames = b"".join(p.build_frame(p.CMD_PRESET, bytes([i]) + synthetic_preset(f"P{i}")) for i in range(200))
        frames += p.build_frame(p.CMD_ACTIVE_PRESET, b"\x03") + p.build_frame(p.CMD_VERSION, b"2.0.4TEST")
        reports = [(bytes([len(frames[i:i + 63])]) + frames[i:i + 63]).ljust(64, b"\0") for i in range(0, len(frames), 63)]

        class Responder(FakeIO):
            def write(self, data):
                self.written.append(data)
                if data[1:6] == bytes([0xAA, 0x55, 0x01, 0x00, 0xE0]) or data[0] == 7:   # the hello frame
                    self.incoming = list(reports)
                return len(data)

        io = Responder()
        snap = Ge200(hid=HidDevice(io=io)).read_state(quiet_s=0.05, timeout_s=5)
        self.assertEqual((len(snap.presets), snap.active_index, snap.version), (200, 3, "2.0.4TEST"))
        self.assertEqual(io.written[0][:8].hex(" "), "07 aa 55 01 00 e0 35 e1")


if __name__ == "__main__":
    unittest.main()
