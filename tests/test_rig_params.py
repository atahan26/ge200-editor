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

    def test_model_tables_cover_every_model_with_the_right_knob_count(self):
        counts = {"fx": (8, 4), "ds": (20, 3), "amp": (65, 6), "ns": (3, 3), "eq": (3, 6), "mod": (20, 4), "reverb": (7, 4)}
        for key, (models, knobs) in counts.items():
            table = params.MODEL_LABELS[key]
            self.assertEqual(sorted(table), list(range(1, models + 1)), key)
            self.assertTrue(all(len(v) == knobs for v in table.values()), key)

    def test_wide_values_roundtrip_through_the_preset_image(self):
        from ge200.preset import WIDE_FIELDS, Preset, apply_edit, EditError
        raw = bytes(512)
        out = apply_edit(raw, {"wide": {"delay_time": 470, "eq_freq_2": 600}})
        pr = Preset.from_bytes(0, out)
        self.assertEqual((pr.wide["delay_time"], pr.wide["eq_freq_2"], pr.wide["delay_time_b"]), (470, 600, 0))
        self.assertEqual(out[350:352], (470).to_bytes(2, "little"))
        self.assertEqual(sum(1 for a, b in zip(raw, out) if a != b), 4)           # 470=0x01D6 and 600=0x0258: 4 bytes
        self.assertIn("delay_time", pr.to_dict()["wide"])
        for bad in ({"wide": {"nope": 1}}, {"wide": {"delay_time": -5}}, {"wide": {"delay_time": 99999}},
                    {"wide": {"delay_time": "x"}}):
            with self.assertRaises(EditError, msg=str(bad)):
                apply_edit(raw, bad)

    def test_noop_edit_keeps_every_byte_even_stale_name_bytes(self):
        from ge200.preset import Preset, apply_edit
        raw = bytearray(512)
        stale = b"FROGGING\x00RY\x00\x00\x00"                # stale bytes after the NUL, as seen in real presets
        raw[12:12 + len(stale)] = stale
        raw[0:9] = bytes(range(1, 10))
        pr = Preset.from_bytes(0, bytes(raw))
        d = pr.to_dict()
        edit = {"name": d["name"], "chain_order": d["chain_order"], "wide": d["wide"],
                "modules": {k: {"type": m["type"], "enabled": m["enabled"], "params": m["params"]}
                            for k, m in d["modules"].items()}}
        self.assertEqual(apply_edit(bytes(raw), edit), bytes(raw))
        edit["name"] = "NEW"
        self.assertEqual(Preset.from_bytes(0, apply_edit(bytes(raw), edit)).name, "NEW")

    def test_labels_come_from_the_device_tables(self):
        self.assertEqual(params.label("mod", 2, model=15), "Q")              # Q-filter: RATE, MIX, Q
        self.assertEqual(params.label("mod", 3, model=15), "(unused)")
        self.assertEqual(params.label("ns", 0, model=3), "ATTACK")           # noise gate: ATTACK, RELEASE, THRES
        self.assertEqual(params.label("ns", 2, model=3), "THRES")
        self.assertEqual(params.label("ns", 0, model=1), "THRES")            # noise killer
        self.assertEqual(params.label("eq", 5, model=3), "3.2KHZ")
        self.assertEqual(params.label("reverb", 0), "PRE DELAY")
        self.assertEqual(params.label("delay", 3, model=1), "SUB-D")
        self.assertEqual(params.label("delay", 4, model=8), "THRES")         # DYNAMIC delay
        self.assertEqual(params.label("delay", 4, model=1), "(unused)")
        self.assertEqual((params.label("delay", 2, model=9), params.label("delay", 5, model=9)), ("TIME A", "SUB B"))
        self.assertEqual(params.label("cab", 1, model=7), "CENTER")
        self.assertEqual(params.confidence("mod", 0, model=5), "high")
        self.assertEqual(params.confidence("delay", 3), "high")              # note division, confirmed by the data

    def test_prompt_text_lists_models_and_unused_knobs(self):
        names = {"mod": [f"M{i}" for i in range(1, 21)]}
        text = params.describe_for_prompt(names)
        self.assertIn("- ds: all models: p0=VOLUME, p1=TONE, p2=GAIN", text)
        self.assertIn("models 15=M15: p0=RATE, p1=MIX, p2=Q, p3=-", text)
        self.assertIn("models 1=M1, 2=M2, 3=M3: p0=RATE, p1=LEVEL, p2=DEPTH", text)
        self.assertIn("models 9: p0=LEVEL, p1=F.BACK, p2=TIME A, p3=SUB A, p4=-, p5=SUB B", text)
        self.assertIn("the delay time is `wide.delay_time` in ms", text)
        self.assertIn("1/16D", text)


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
        self.assertIn("p2=GAIN", a)
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
        self.assertEqual(st["model_labels"]["mod"]["15"], ["RATE", "MIX", "Q", None])
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
