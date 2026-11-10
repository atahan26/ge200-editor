"""IR conversion tests (synthetic audio only). The conversion tests need the optional audio extras."""
import os
import tempfile
import unittest

from ge200 import ir

try:
    import numpy as np
    import soundfile as sf
    HAVE_AUDIO = True
except ImportError:      # pragma: no cover
    HAVE_AUDIO = False


class Int24Tests(unittest.TestCase):
    def test_vendor_conversion_edges(self):
        v = ir.to_int24([0.0, 0.5, -0.5, 1.0, -1.0, 2.0, 1e-9, -1e-9])
        self.assertEqual(v[0], 0)
        self.assertEqual(v[1], 0x400000)                      # +0.5 * 2^23
        self.assertEqual(v[2], 0xC00000)                      # -0.5 as 24-bit two's complement
        self.assertEqual(v[3], v[5])                          # clamped to the same positive limit
        self.assertEqual(v[3], int(0.9999989867210388 * 8388608) & 0xFFFFFF)
        self.assertEqual(v[4], (-int(0.9999989867210388 * 8388608)) & 0xFFFFFF)
        self.assertEqual((v[6], v[7]), (0, 0))                # truncation toward zero

    def test_planes(self):
        hi, mid, lo = ir.planes([0x123456, 0xFEDCBA])
        self.assertEqual((hi, mid, lo), (b"\x12\xfe", b"\x34\xdc", b"\x56\xba"))

    def test_name(self):
        n = ir.clean_name("C:/x/my cab 4x12 long name.wav")
        self.assertEqual(len(n), 15)
        self.assertEqual(n, b"MY CAB 4X12 LON")
        self.assertEqual(ir.clean_name("a\u00e9b.wav"), b"AB" + b"\x00" * 13)

    def test_payload_layout(self):
        samples = [0.0] * 512
        samples[0] = 0.5
        pl = ir.write_payloads(3, samples, "test.wav")
        self.assertEqual([(p[0], p[1]) for p in pl], [(3, 1), (3, 2), (3, 3), (3, 4)])
        self.assertEqual([len(p) for p in pl], [514, 514, 514, 17])
        self.assertEqual(pl[0][2], 0x40)                      # high byte of the first sample
        self.assertEqual(pl[3][2:], b"TEST" + b"\x00" * 11)
        with self.assertRaises(ValueError):
            ir.write_payloads(10, samples, "x")
        with self.assertRaises(ValueError):
            ir.write_payloads(0, samples[:100], "x")
        self.assertEqual(ir.clear_payload(9), b"\x09")


@unittest.skipUnless(HAVE_AUDIO, "numpy/scipy/soundfile not installed")
class PrepareTests(unittest.TestCase):
    def test_resample_trim_fade(self):
        rate = 48000
        x = np.zeros(4000)
        x[300] = 1.0
        x[301:900] = 0.3 * np.exp(-np.arange(599) / 80)
        y = ir.prepare(x, rate)
        self.assertEqual(y.shape, (512,))
        # leading silence was removed: the peak sits near the start (a little later than the pre-roll because the
        # resampling filter rings before the impulse, and that ringing is kept)
        self.assertTrue(8 <= int(np.argmax(np.abs(y))) <= 40)
        self.assertAlmostEqual(float(y[-1]), 0.0, places=6)          # faded out
        self.assertLessEqual(float(np.max(np.abs(y))), 0.99 + 1e-6)

    def test_short_input_is_padded_and_level_can_be_normalised(self):
        y = ir.prepare(np.array([0.0, 0.2, -0.1]), 44100, normalize_peak=0.8)
        self.assertEqual(y.shape, (512,))
        self.assertAlmostEqual(float(np.max(np.abs(y))), 0.8, places=5)

    def test_silence_is_rejected(self):
        with self.assertRaises(ValueError):
            ir.prepare(np.zeros(1000), 44100)

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.wav")
            sf.write(p, np.stack([np.eye(1, 600, 5)[0], np.eye(1, 600, 5)[0]], axis=1), 44100, subtype="PCM_24")
            x, rate = ir.load_audio(p)
            self.assertEqual((rate, x.ndim), (44100, 1))
            self.assertEqual(int(np.argmax(x)), 5)


if __name__ == "__main__":
    unittest.main()
