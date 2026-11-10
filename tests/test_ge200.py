"""Offline tests using synthetic data only (no device, no vendor data)."""
import unittest

from ge200 import protocol as p
from ge200.client import Snapshot, dump_raw, load_raw
from ge200.preset import MODULE_LAYOUT, PRESET_SIZE, Preset


def synthetic_preset(name: str = "TEST", fx_type: int = 3) -> bytes:
    raw = bytearray(PRESET_SIZE)
    raw[0:9] = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    raw[12:12 + len(name)] = name.encode()
    off = dict((k, o) for k, o, _ in MODULE_LAYOUT)["fx"]
    raw[off], raw[off + 1], raw[off + 2:off + 6] = fx_type, 1, bytes([10, 20, 30, 40])
    return bytes(raw)


class ProtocolTests(unittest.TestCase):
    def test_hello_packet_matches_wire_capture(self):
        self.assertEqual(p.build_frame(p.CMD_HELLO).hex(" "), "aa 55 01 00 e0 35 e1")

    def test_hello_report_layout(self):
        (report,) = p.frame_to_reports(p.build_frame(p.CMD_HELLO))
        self.assertEqual(len(report), 64)
        self.assertEqual(report[:8].hex(" "), "07 aa 55 01 00 e0 35 e1")

    def test_roundtrip_multi_report_frame(self):
        data = bytes(range(256)) * 2 + b"x" * 1  # 513 bytes like a preset frame
        frame = p.build_frame(0xA5, data)
        reports = p.frame_to_reports(frame)
        self.assertGreater(len(reports), 1)
        frames = p.frames_from_reports(reports)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].crc_ok)
        self.assertEqual((frames[0].cmd, frames[0].data), (0xA5, data))

    def test_bad_crc_is_flagged(self):
        frame = bytearray(p.build_frame(0x90, b"\x01\x02"))
        frame[-1] ^= 0xFF
        (f,) = p.frames_from_reports(p.frame_to_reports(bytes(frame)))
        self.assertFalse(f.crc_ok)

    def test_resync_after_garbage_and_back_to_back_frames(self):
        a, b = p.build_frame(0x91, b"\x01"), p.build_frame(0x92, b"\x02\x03")
        stream = b"\x00\x13\xaa" + a + b
        reports = [(bytes([len(stream[i:i + 63])]) + stream[i:i + 63]).ljust(64, b"\0")
                   for i in range(0, len(stream), 63)]
        frames = p.frames_from_reports(reports)
        self.assertEqual([f.cmd for f in frames], [0x91, 0x92])
        self.assertTrue(all(f.crc_ok for f in frames))


class PresetTests(unittest.TestCase):
    def test_parse(self):
        pr = Preset.from_bytes(7, synthetic_preset("HELLO", fx_type=5))
        self.assertEqual((pr.index, pr.name), (7, "HELLO"))
        self.assertEqual(pr.chain_order, list(range(1, 10)))
        self.assertEqual(pr.modules["fx"].type, 5)
        self.assertTrue(pr.modules["fx"].enabled)
        self.assertEqual(pr.modules["fx"].params, [10, 20, 30, 40])
        self.assertEqual(pr.to_dict({"fx": ["a", "b", "c", "d", "e"]})["modules"]["fx"]["model"], "e")

    def test_wrong_size(self):
        with self.assertRaises(ValueError):
            Preset.from_bytes(0, b"\0" * 10)


class FakeHid:
    def __init__(self):
        self.sent = []

    def start_read(self):
        pass

    def write_report(self, report):
        self.sent.append(report)

    def close(self):
        pass


class WriteApiTests(unittest.TestCase):
    def test_writes_are_blocked_by_default(self):
        from ge200.client import Ge200, WriteNotAllowed
        dev = Ge200(hid=FakeHid())
        with self.assertRaises(WriteNotAllowed):
            dev.select_preset(1)
        self.assertEqual(dev.hid.sent, [])

    def test_write_slot_frame(self):
        from ge200.client import Ge200
        hid = FakeHid()
        Ge200(hid=hid, allow_write=True).write_slot(199, synthetic_preset("X"))
        frames = p.frames_from_reports(hid.sent)
        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].cmd, frames[0].data[0], len(frames[0].data), frames[0].crc_ok),
                         (0xE5, 199, 513, True))

    def test_select_frame_matches_wire(self):
        from ge200.client import Ge200
        hid = FakeHid()
        Ge200(hid=hid, allow_write=True).select_preset(163)
        self.assertEqual(hid.sent[0][:9].hex(" "), "08 aa 55 02 00 a6 a3 20 c6")   # seen on the wire
        with self.assertRaises(ValueError):
            Ge200(hid=hid, allow_write=True).write_slot(200, synthetic_preset())


class SnapshotTests(unittest.TestCase):
    def build_reports(self):
        reports = []
        for i in (0, 1):
            reports += p.frame_to_reports(p.build_frame(p.CMD_PRESET, bytes([i]) + synthetic_preset(f"P{i}")))
        reports += p.frame_to_reports(p.build_frame(p.CMD_ACTIVE_PRESET, b"\x01"))
        reports += p.frame_to_reports(p.build_frame(p.CMD_VERSION, b"2.0.4MOOER"))
        return reports

    def test_snapshot(self):
        snap = Snapshot.from_reports(self.build_reports())
        self.assertEqual(sorted(snap.presets), [0, 1])
        self.assertEqual(snap.presets[1].name, "P1")
        self.assertEqual(snap.active_index, 1)
        self.assertTrue(snap.version.startswith("2.0.4"))
        self.assertEqual(snap.bad_crc, 0)

    def test_raw_file_both_record_sizes(self):
        reports = self.build_reports()
        self.assertEqual(load_raw(dump_raw(reports)), reports)
        with65 = b"".join(b"\x00" + r for r in reports)
        self.assertEqual(load_raw(with65), reports)


if __name__ == "__main__":
    unittest.main()
