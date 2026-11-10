"""Web API tests with an in-memory device (no hardware, synthetic data)."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from ge200.client import Snapshot
from ge200.preset import EditError, Preset, apply_edit
from ge200.web.server import App, MemoryBackend, make_server
from tests.test_ge200 import synthetic_preset


def make_snapshot() -> Snapshot:
    snap = Snapshot(version="test", active_index=1)
    for i in range(3):
        snap.presets[i] = Preset.from_bytes(i, synthetic_preset(f"P{i}", fx_type=2))
    return snap


class ApplyEditTests(unittest.TestCase):
    def test_edit_fields_and_leaves_rest_untouched(self):
        raw = synthetic_preset("A")
        out = apply_edit(raw, {"name": "NEW", "chain_order": [9, 8, 7, 6, 5, 4, 3, 2, 1],
                               "modules": {"fx": {"type": 4, "enabled": False, "params": [1, 2, 3, 4]}}})
        pr = Preset.from_bytes(0, out)
        self.assertEqual((pr.name, pr.chain_order[0], pr.modules["fx"].type, pr.modules["fx"].enabled,
                          pr.modules["fx"].params), ("NEW", 9, 4, False, [1, 2, 3, 4]))
        changed = [i for i in range(512) if raw[i] != out[i]]
        self.assertTrue(all(i < 9 or 12 <= i < 28 or 28 <= i < 34 for i in changed))

    def test_rejects_bad_edits(self):
        raw = synthetic_preset()
        for bad in ({"name": "x" * 17}, {"name": "café"}, {"chain_order": [1, 1, 3, 4, 5, 6, 7, 8, 9]},
                    {"modules": {"nope": {}}}, {"modules": {"fx": {"params": [1, 2]}}},
                    {"modules": {"fx": {"params": [1, 2, 3, 999]}}}, {"modules": {"fx": {"type": 0}}},
                    {"surprise": 1}):
            with self.assertRaises(EditError, msg=str(bad)):
                apply_edit(raw, bad)


class SlowBackend(MemoryBackend):
    """load_live blocks until released, like a slow device."""

    def __init__(self, snap):
        super().__init__(snap)
        self.gate = threading.Event()
        self.entered = threading.Event()

    def load_live(self, raw):
        self.entered.set()
        self.gate.wait(5)
        super().load_live(raw)


class QueueRaceTests(unittest.TestCase):
    def test_queued_updates_of_the_old_preset_never_reach_the_device(self):
        """Regression: slider updates queued while the device was busy used to run *after* the user opened
        another preset and switched the device back to the old one."""
        backend = SlowBackend(make_snapshot())
        app = App(backend, {}, None)
        app.select(0, 1)
        app.live(0, {"name": "A"}, 1)                       # worker picks this up and blocks in load_live
        self.assertTrue(backend.entered.wait(3))
        app.live(0, {"name": "B"}, 1)                       # newest update, still queued
        t = threading.Thread(target=app.select, args=(2, 2))   # user opens preset 2 meanwhile
        t.start()
        time.sleep(0.1)
        backend.gate.set()
        t.join(3)
        time.sleep(0.3)
        self.assertEqual(backend.log, ["select 0", "live", "select 2"])     # B was dropped, no switch back to 0
        self.assertEqual(backend.active, 2)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend(make_snapshot())
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.backend, {"fx": ["a", "b", "c", "d"]}, self.tmp.name)
        self.srv = make_server(self.app, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, token=True, host=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-GE200-Token"] = self.app.token
        if host:
            headers["Host"] = host
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_state_and_preset(self):
        code, st = self.call("/api/state")
        self.assertEqual((code, st["active"], len(st["presets"])), (200, 1, 3))
        self.assertEqual(self.call("/api/preset/2")[1]["name"], "P2")
        self.assertEqual(self.call("/api/preset/50")[0], 400)

    def test_page_embeds_token(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn(self.app.token, r.read().decode())

    def test_security_checks(self):
        self.assertEqual(self.call("/api/select", {"slot": 0}, token=False)[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.backend.log, [])

    def wait_for(self, cond, timeout=3.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def test_live_does_not_store(self):
        code, r = self.call("/api/live", {"slot": 2, "edit": {"name": "TMP"}, "epoch": 1})
        self.assertEqual((code, r.get("queued")), (200, True))
        self.assertTrue(self.wait_for(lambda: self.backend.log == ["select 2", "live"]))
        self.assertEqual(self.backend.presets[2][12:15], b"P2\x00")      # stored image untouched
        self.call("/api/live", {"slot": 2, "edit": {"name": "TMP2"}, "epoch": 1})
        self.assertTrue(self.wait_for(lambda: self.backend.log == ["select 2", "live", "live"]))  # no 2nd select

    def test_stale_live_update_is_dropped(self):
        code, r = self.call("/api/select", {"slot": 2, "epoch": 5})
        self.assertEqual(code, 200)
        code, r = self.call("/api/live", {"slot": 0, "edit": {"name": "OLD"}, "epoch": 4})   # from an older view
        self.assertTrue(r.get("dropped"))
        time.sleep(0.2)
        self.assertEqual(self.backend.log, ["select 2"])

    def test_save_backs_up_first_and_verifies(self):
        code, r = self.call("/api/save", {"slot": 0, "edit": {"name": "SAVED",
                            "modules": {"fx": {"enabled": False}}}})
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["preset"]["name"], "SAVED")
        files = os.listdir(self.tmp.name)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp.name, files[0]), encoding="utf-8") as f:
            backup = json.load(f)
        self.assertEqual(backup["presets"][0]["name"], "P0")              # the backup holds the OLD content
        self.assertEqual(self.backend.log, ["write 0"])

    def test_saving_identical_content_writes_nothing(self):
        code, r = self.call("/api/save", {"slot": 0, "edit": {}})
        self.assertEqual((code, r["ok"], r.get("unchanged")), (200, True, True))
        code, r = self.call("/api/save", {"slot": 0, "edit": {"name": "P0"}})        # same name as stored
        self.assertTrue(r.get("unchanged"))
        self.assertEqual(self.backend.log, [])
        self.assertEqual(os.listdir(self.tmp.name), [])                               # not even a backup was needed

    def test_bad_requests(self):
        self.assertEqual(self.call("/api/save", {"slot": 0, "edit": {"chain_order": [1, 2]}})[0], 400)
        self.assertEqual(self.call("/api/select", {"slot": 500})[0], 400)
        self.assertEqual(self.call("/api/select", {"slot": "1"})[0], 400)
        self.assertEqual(self.call("/api/select", {"slot": 1, "epoch": "x"})[0], 400)
        self.assertEqual(self.backend.log, [])


if __name__ == "__main__":
    unittest.main()
