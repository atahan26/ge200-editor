"""IR import tools of the web editor: preview, upload guard, folder safety (memory backend, synthetic audio)."""
import base64
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

try:
    import numpy as np
    import soundfile as sf
    HAVE_AUDIO = True
except ImportError:      # pragma: no cover
    HAVE_AUDIO = False

from ge200.client import Snapshot
from ge200.web.irlab import IrError, IrLab
from ge200.web.server import App, MemoryBackend, make_server
from tests.test_tone import make_presets


def wav_bytes(seconds=0.2, rate=48000, stereo=False):
    t = np.arange(int(seconds * rate)) / rate
    x = (np.exp(-t * 60) * np.sin(2 * np.pi * 1500 * t)).astype("float32")
    data = np.stack([x, x * 0.5], axis=1) if stereo else x
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV", subtype="PCM_24")
    return buf.getvalue()


@unittest.skipUnless(HAVE_AUDIO, "numpy/soundfile not installed")
class IrLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "sub"))
        with open(os.path.join(self.tmp.name, "sub", "My Cab 57.wav"), "wb") as f:
            f.write(wav_bytes())
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as f:
            f.write("not audio")
        snap = Snapshot(version="t", active_index=1)
        snap.presets = make_presets(6)
        self.backend = MemoryBackend(snap)
        self.app = App(self.backend, {}, None, None, None, IrLab(self.tmp.name))
        self.srv = make_server(self.app, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def post(self, path, body, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-GE200-Token"] = self.app.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_listing_shows_only_audio_files_of_the_folder(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/ir") as r:
            info = json.loads(r.read())
        self.assertEqual(info["files"], ["sub/My Cab 57.wav"])
        self.assertTrue(info["available"])

    def test_preview_of_a_browser_upload_and_of_a_folder_file(self):
        data = base64.b64encode(wav_bytes(rate=44100, stereo=True)).decode()
        code, r = self.post("/api/ir/preview", {"data": data, "filename": "Fancy Cab (57) v2.wav"})
        self.assertEqual(code, 200)
        self.assertEqual(len(r["wave"]), 512)
        self.assertEqual(r["name"], "FANCY CAB 57 V2")
        self.assertLessEqual(r["peak_out"], 0.99)
        code, r2 = self.post("/api/ir/preview", {"file": "sub/My Cab 57.wav", "normalize": True})
        self.assertEqual(code, 200)
        self.assertEqual(r2["rate"], 48000)
        self.assertAlmostEqual(r2["peak_out"], 0.9, places=2)       # normalised
        self.assertTrue(r2["cut"])                                    # 200 ms is longer than the 11.6 ms the unit keeps

    def test_bad_inputs_are_refused(self):
        self.assertEqual(self.post("/api/ir/preview", {"file": "../../etc/passwd"})[0], 400)
        self.assertEqual(self.post("/api/ir/preview", {"file": "notes.txt"})[0], 400)       # not in the audio listing
        self.assertEqual(self.post("/api/ir/preview", {"data": "###"})[0], 400)
        junk = base64.b64encode(b"this is not audio").decode()
        self.assertEqual(self.post("/api/ir/preview", {"data": junk})[0], 400)
        self.assertEqual(self.post("/api/ir/preview", {})[0], 400)
        self.assertEqual(self.post("/api/ir/preview", {"file": "sub/My Cab 57.wav"}, token=False)[0], 403)

    def test_upload_needs_the_name_the_user_saw_and_updates_the_names(self):
        body = {"slot": 9, "name": "My cab ir", "file": "sub/My Cab 57.wav"}
        code, r = self.post("/api/ir/upload", dict(body, replace="SOMETHING ELSE"))
        self.assertEqual(code, 400)                                    # stale page: slot holds a different IR
        self.assertEqual(self.backend.log, [])
        code, r = self.post("/api/ir/upload", dict(body, replace=""))
        self.assertEqual(code, 200)
        self.assertEqual(r["name"], "MY CAB IR")
        self.assertEqual(r["slots"][9], "MY CAB IR")
        self.assertEqual(self.backend.log, ["ir 9 MY CAB IR"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state") as resp:
            self.assertEqual(json.loads(resp.read())["ir_names"][9], "MY CAB IR")
        # the next upload to that slot must name the new holder
        self.assertEqual(self.post("/api/ir/upload", dict(body, replace=""))[0], 400)
        self.assertEqual(self.post("/api/ir/upload", dict(body, replace="MY CAB IR"))[0], 200)

    def test_upload_validates_slot_and_name_and_token(self):
        base = {"name": "X", "file": "sub/My Cab 57.wav", "replace": ""}
        for bad in (10, -1, "3", None, True):
            self.assertEqual(self.post("/api/ir/upload", dict(base, slot=bad))[0], 400, bad)
        self.assertEqual(self.post("/api/ir/upload", dict(base, slot=0, name="   "))[0], 400)
        self.assertEqual(self.post("/api/ir/upload", dict(base, slot=0), token=False)[0], 403)
        self.assertEqual(self.backend.log, [])

    def test_irlab_limits(self):
        lab = IrLab(None)
        with self.assertRaises(IrError):
            lab.convert(None, None)
        big = base64.b64encode(b"x" * (9 * 1024 * 1024)).decode()
        with self.assertRaises(IrError):
            lab.convert(None, big, "big.wav")


if __name__ == "__main__":
    unittest.main()
