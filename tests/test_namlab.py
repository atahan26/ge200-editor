"""NAM file inspection and the web NAM lab (fake renderer, synthetic files only; no PyTorch needed)."""
import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

from ge200.client import Snapshot
from ge200.nam.info import NamFileError, inspect_nam, scan_library
from ge200.web.namlab import BUILTIN_DI, NamLab
from ge200.web.server import App, MemoryBackend, make_server
from tests.test_tone import make_presets


def write_nam(path, name="Capture", **meta):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cfg = {"version": "0.5.4", "architecture": "WaveNet", "config": {}, "weights": [0.1] * 10,
           "metadata": dict({"name": name, "gear_make": "Acme", "gear_model": "Amp 1", "tone_type": "crunch"}, **meta)}
    Path(path).write_text(json.dumps(cfg), encoding="utf-8")


def write_wav(path, n=100):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"".join(struct.pack("<h", (i * 37) % 2000) for i in range(n)))


class InfoTests(unittest.TestCase):
    def test_inspect_and_library(self):
        with tempfile.TemporaryDirectory() as d:
            write_nam(Path(d) / "a.nam", "Alpha", loudness=-12.5)
            write_nam(Path(d) / "sub" / "b.nam", "beta", sample_rate=None)
            (Path(d) / "junk.nam").write_text("{not json", encoding="utf-8")
            (Path(d) / "other.json").write_text("{}", encoding="utf-8")
            info = inspect_nam(str(Path(d) / "a.nam"))
            self.assertEqual((info["name"], info["architecture"], info["weights"], info["sample_rate"]),
                             ("Alpha", "WaveNet", 10, 48000))
            self.assertEqual((info["gear_make"], info["loudness"]), ("Acme", -12.5))
            lib = scan_library(d)
            self.assertEqual([m["name"] for m in lib], ["Alpha", "beta"])          # junk skipped, sorted, recursive
            with self.assertRaises(NamFileError):
                inspect_nam(str(Path(d) / "junk.nam"))
            with self.assertRaises(NamFileError):
                inspect_nam(str(Path(d) / "other.json"))


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, python_exe, args, timeout=600):
        self.calls.append(args)
        out = args[args.index("render") + 3]
        write_wav(out, 50)
        return {"seconds": 1.0, "inference_s": 0.01, "total_s": 0.02, "realtime_factor": 0.01}


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        write_nam(d / "nam" / "one.nam", "One")
        write_wav(d / "ir" / "cab.wav")
        write_wav(d / "di" / "riff.wav")
        self.exe = str(d / "python.exe")
        Path(self.exe).write_text("", encoding="utf-8")
        self.runner = FakeRunner()
        self.lab = NamLab(str(d / "nam"), str(d / "ir"), str(d / "di"), self.exe, runner=self.runner)

    def tearDown(self):
        self.tmp.cleanup()

    def test_listing(self):
        lst = self.lab.listing()
        self.assertTrue(lst["available"])
        self.assertEqual([m["id"] for m in lst["models"]], ["one.nam"])
        self.assertNotIn("path", lst["models"][0])                       # absolute paths are not exposed
        self.assertEqual((lst["irs"], lst["dis"]), (["cab.wav"], [BUILTIN_DI, "riff.wav"]))

    def test_render_uses_only_listed_files(self):
        r = self.lab.render("one.nam", "cab.wav", "riff.wav", 3)
        self.assertEqual(len(r["id"]), 16)
        args = self.runner.calls[0]
        self.assertTrue(args[1].endswith("one.nam") and "--ir" in args and "--gain-db" in args)
        self.assertTrue(self.lab.audio(r["id"]).startswith(b"RIFF"))
        for bad in (("../one.nam", "", "riff.wav", 0), ("one.nam", "../../x.wav", "riff.wav", 0),
                    ("one.nam", "", "..\\riff.wav", 0), ("one.nam", "", "riff.wav", 999), ("nope.nam", "", "riff.wav", 0)):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.lab.render(*bad)
        self.assertEqual(len(self.runner.calls), 1)                      # nothing invalid reached the runner

    def test_builtin_di_is_generated(self):
        r = self.lab.render("one.nam", "", BUILTIN_DI, 0)
        self.assertTrue(os.path.exists(self.runner.calls[0][2]))
        self.assertNotIn("--ir", self.runner.calls[0])
        with self.assertRaises(KeyError):
            self.lab.audio("0123456789abcdef")

    def test_unavailable_without_the_nam_environment(self):
        lab = NamLab(str(Path(self.tmp.name) / "nam"), None, None, str(Path(self.tmp.name) / "missing.exe"))
        self.assertFalse(lab.listing()["available"])
        with self.assertRaises(ValueError):
            lab.render("one.nam", "", BUILTIN_DI, 0)


class LabWebTests(unittest.TestCase):
    def setUp(self):
        self.lt = LabTests("test_listing")
        self.lt.setUp()
        snap = Snapshot(version="t", active_index=1)
        snap.presets = make_presets(4)
        self.app = App(MemoryBackend(snap), {}, None, None, self.lt.lab)
        self.srv = make_server(self.app, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.lt.tearDown()

    def call(self, path, body=None, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-GE200-Token"] = self.app.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (raw if r.headers.get_content_type() == "audio/wav" else json.loads(raw))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_endpoints(self):
        code, lst = self.call("/api/nam")
        self.assertEqual((code, lst["models"][0]["id"]), (200, "one.nam"))
        code, r = self.call("/api/nam/render", {"model": "one.nam", "ir": "", "di": BUILTIN_DI, "gain_db": 0})
        self.assertEqual(code, 200)
        code, audio = self.call("/api/nam/audio/" + r["id"])
        self.assertEqual((code, audio[:4]), (200, b"RIFF"))
        self.assertEqual(self.call("/api/nam/audio/ffffffffffffffff")[0], 404)
        self.assertEqual(self.call("/api/nam/render", {"model": "../x", "ir": "", "di": BUILTIN_DI})[0], 400)
        self.assertEqual(self.call("/api/nam/render", {"model": "one.nam", "di": BUILTIN_DI}, token=False)[0], 403)


if __name__ == "__main__":
    unittest.main()
