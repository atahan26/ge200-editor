"""Rig profile and parameter-knowledge tests (synthetic data, temp files only)."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ge200 import params, rig, tone
from ge200.client import Snapshot
from ge200.preset import MODULE_LAYOUT
from ge200.web.server import App, MemoryBackend, make_server
from tests.test_tone import FakeClient, good_answer, make_presets


class ParamsTests(unittest.TestCase):
    def test_every_module_has_exactly_the_right_number_of_labels(self):
        for key, _, length in MODULE_LAYOUT:
            self.assertEqual(len(params.PARAMS[key]), length - 2, key)
            for i, item in enumerate(params.PARAMS[key]):
                self.assertIn(item["confidence"], (params.HIGH, params.MEDIUM, params.LOW, params.UNKNOWN))
                self.assertEqual(params.label(key, i), item["label"])

    def test_prompt_text_states_confidence(self):
        text = params.describe_for_prompt()
        self.assertIn("ds: p0=LEVEL [high]", text)
        self.assertIn("p2=GAIN [high]", text)
        self.assertIn("fx: p0=P1, p1=P2", text)                  # unknown params carry no confidence tag


class RigTests(unittest.TestCase):
    def test_clean_validates(self):
        self.assertEqual(rig.clean({"guitars": [{"name": "  Strat   style ", "scale": ""}], "notes": ""}),
                         {"guitars": [{"name": "Strat style"}]})
        for bad in ({"nope": "x"}, {"guitars": [{"name": 5}]}, {"guitars": [{"name": "x" * 401}]}, ["list"],
                    {"guitars": [{"bogus": "x"}]}, {"guitars": "no"}, {"guitars": [{"name": "g"}] * 9},
                    {"listening": 3}):
            with self.assertRaises(rig.RigError, msg=str(bad)):
                rig.clean(bad)
        self.assertEqual(rig.clean({"guitars": [{}, {"name": ""}]}), {})          # empty guitars are dropped

    def test_old_single_guitar_layout_is_migrated(self):
        old = {"guitar": "Les Paul", "neck_pickup": "PAF", "strings_tuning": "10-46, E standard", "likes": "warm"}
        self.assertEqual(rig.clean(old), {"guitars": [{"name": "Les Paul", "neck_pickup": "PAF",
                                                       "tuning": "10-46, E standard"}], "likes": "warm"})

    def test_save_load_roundtrip_is_atomic_and_private_to_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "rig.json"
            saved = rig.save({"guitars": [{"name": "Les Paul", "neck_pickup": "PAF humbucker"}]}, p)
            self.assertEqual(rig.load(p), saved)
            self.assertEqual([f for f in os.listdir(p.parent)], ["rig.json"])      # no temp files left
            self.assertEqual(rig.load(Path(d) / "missing.json"), {})
            (Path(d) / "bad.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(rig.load(Path(d) / "bad.json"), {})

    def test_env_override_for_the_path(self):
        os.environ["GE200_RIG"] = r"C:\somewhere\rig.json"
        try:
            self.assertEqual(rig.default_path(), Path(r"C:\somewhere\rig.json"))
        finally:
            os.environ.pop("GE200_RIG")

    def test_prompt_includes_rig_and_stays_stable(self):
        presets = make_presets()
        r = rig.clean({"guitars": [{"name": "Ibanez RG", "bridge_pickup": "active humbucker, high output", "strings": "7"},
                                   {"name": "PRS SE"}], "listening": "headphones"})     # clean() fixes the field order
        a = tone.build_system_prompt(presets, None, rig=r)
        self.assertIn("Guitar 1: Ibanez RG; Strings: 7; Bridge pickup: active humbucker, high output", a)
        self.assertIn("Guitar 2: PRS SE", a)
        self.assertIn("Listening setup: headphones", a)
        self.assertEqual(rig.guitar_names(r), ["Ibanez RG", "PRS SE"])
        base = presets[1]
        self.assertIn("Guitar 2, PRS SE", tone.build_user_prompt("x", base, "Guitar 2, PRS SE"))
        self.assertNotIn("will play this preset with", tone.build_user_prompt("x", base))
        self.assertEqual(a, tone.build_system_prompt(presets, None, rig=r))
        self.assertNotIn("guitarist's own rig", tone.build_system_prompt(presets, None))     # no rig -> no block
        self.assertIn("p2=GAIN [high]", a)
        self.assertIn("You cannot hear audio", a)


class RigWebTests(unittest.TestCase):
    def setUp(self):
        snap = Snapshot(version="t", active_index=1)
        snap.presets = make_presets(6)
        self.tmp = tempfile.TemporaryDirectory()
        self.rig_file = Path(self.tmp.name) / "rig.json"
        self.app = App(MemoryBackend(snap), {}, None, self.rig_file)
        self.app.llm_client = FakeClient([good_answer()])
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

    def test_rig_saved_exposed_and_sent_to_the_generator(self):
        rig_in = {"guitars": [{"name": "Tele"}, {"name": "7-string", "strings": "7 (10-56)"}], "likes": "glassy cleans"}
        code, r = self.post("/api/rig", {"rig": rig_in})
        self.assertEqual((code, r["rig"]["guitars"][0]["name"]), (200, "Tele"))
        self.assertEqual(rig.load(self.rig_file)["likes"], "glassy cleans")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state") as resp:
            st = json.loads(resp.read())
        self.assertEqual(st["rig"]["guitars"][1]["name"], "7-string")
        self.assertIn("likes", st["rig_fields"])
        self.assertIn("scale", st["guitar_fields"])
        self.assertIn("ds", st["params"])
        code, _ = self.post("/api/tone", {"slot": 2, "description": "clean", "edit": {}, "guitar": 1})
        self.assertEqual(code, 200)
        kw = self.app.llm_client.calls[0][1]
        self.assertIn("Guitar 1: Tele", kw["system"][0]["text"])
        self.assertIn("Guitar 2, 7-string", kw["messages"][0]["content"])
        self.assertEqual(self.post("/api/tone", {"slot": 2, "description": "clean", "guitar": 5})[0], 400)

    def test_rig_endpoint_checks(self):
        self.assertEqual(self.post("/api/rig", {"rig": {"bogus": "x"}})[0], 400)
        self.assertEqual(self.post("/api/rig", {"rig": {"likes": "x"}}, token=False)[0], 403)
        self.assertFalse(self.rig_file.exists())


if __name__ == "__main__":
    unittest.main()
