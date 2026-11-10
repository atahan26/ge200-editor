"""Tone generator tests with a fake Claude client (no network, no API key, synthetic presets)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from ge200 import tone
from ge200.client import Snapshot
from ge200.preset import MODULE_LAYOUT, PRESET_SIZE, Preset, param_ranges
from ge200.web.server import App, MemoryBackend, make_server


def make_presets(n=30):
    out = {}
    for i in range(n):
        raw = bytearray(PRESET_SIZE)
        raw[0:9] = bytes(range(1, 10))
        name = f"PRESET {i}" if i % 5 else "EMPTY"
        raw[12:12 + len(name)] = name.encode()
        for k, (key, off, length) in enumerate(MODULE_LAYOUT):
            raw[off] = 1 + (i + k) % 4                      # type 1..4
            raw[off + 1] = (i + k) % 2
            for j in range(2, length):
                raw[off + j] = (10 * (i + j)) % 61           # params 0..60
        out[i] = Preset.from_bytes(i, bytes(raw))
    return out


class FakeClient:
    """Mimics client.beta.messages.create / client.messages.create and records the requests."""

    def __init__(self, answers, stop_reason="end_turn"):
        self.answers, self.stop_reason, self.calls = list(answers), stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self._create("beta", **kw)))
        self.messages = SimpleNamespace(create=lambda **kw: self._create("plain", **kw))

    def _create(self, where, **kwargs):
        self.calls.append((where, kwargs))
        text = self.answers.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def good_answer(**over):
    mods = {k: {"type": 2, "enabled": True, "params": [30] * (n - 2)} for k, _, n in MODULE_LAYOUT}
    ans = {"name": "DREAM CLEAN", "notes": "Clean amp with a long reverb.", "chain_order": list(range(1, 10)),
           "modules": mods}
    ans.update(over)
    return json.dumps(ans)


class ToneTests(unittest.TestCase):
    def setUp(self):
        self.presets = make_presets()
        self.base = self.presets[1]

    def test_system_prompt_is_stable_and_description_goes_in_the_user_message(self):
        a = tone.build_system_prompt(self.presets, {"fx": ["WAH ONE", "COMP TWO"]})
        b = tone.build_system_prompt(self.presets, {"fx": ["WAH ONE", "COMP TWO"]})
        self.assertEqual(a, b)                                   # stable => cacheable
        self.assertIn("1=WAH ONE", a)
        self.assertIn("Observed ranges", a)
        examples = [l for l in a.splitlines() if l.startswith("{")]
        self.assertTrue(examples)
        self.assertFalse(any('"name":"EMPTY"' in l for l in examples))        # only named presets are examples
        self.assertIn("warm jazz", tone.build_user_prompt("warm jazz", self.base))
        self.assertNotIn("warm jazz", a)

    def test_request_shape(self):
        fake = FakeClient([good_answer()])
        res = tone.generate("a dreamy clean", self.base, self.presets, client=fake)
        where, kw = fake.calls[0]
        self.assertEqual(where, "beta")
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("thinking", kw)                          # always-on on this model: must not be sent
        self.assertNotIn("temperature", kw)
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")
        self.assertEqual(res["notes"], "Clean amp with a long reverb.")

    def test_without_fallbacks_uses_plain_endpoint(self):
        fake = FakeClient([good_answer()])
        tone.generate("x", self.base, self.presets, client=fake, fallbacks=False)
        self.assertEqual(fake.calls[0][0], "plain")
        self.assertNotIn("fallbacks", fake.calls[0][1])

    def test_answer_is_sanitized(self):
        mods = {k: {"type": 999, "enabled": 1, "params": [9999] * 2} for k, _, _ in MODULE_LAYOUT}   # too big, too short
        fake = FakeClient([good_answer(name="café " + "X" * 40, chain_order=[1, 1, 1], modules=mods)])
        edit = tone.generate("x", self.base, self.presets, client=fake)["edit"]
        self.assertTrue(all(32 <= ord(c) <= 126 for c in edit["name"]) and len(edit["name"]) <= 16)
        self.assertEqual(edit["chain_order"], list(self.base.chain_order))        # invalid order falls back to base
        rg = param_ranges(list(self.presets.values()))
        for k, _, n in MODULE_LAYOUT:
            m = edit["modules"][k]
            self.assertLessEqual(m["type"], max(rg[k]["type"][1], 1))
            self.assertEqual(len(m["params"]), n - 2)
            self.assertTrue(all(0 <= v <= rg[k]["params"][i][1] for i, v in enumerate(m["params"])))

    def test_retry_once_on_bad_json_then_fail(self):
        fake = FakeClient(["not json", good_answer()])
        self.assertEqual(tone.generate("x", self.base, self.presets, client=fake)["edit"]["name"], "DREAM CLEAN")
        self.assertEqual(len(fake.calls), 2)
        with self.assertRaises(tone.ToneError):
            tone.generate("x", self.base, self.presets, client=FakeClient(["nope", "still no"]))

    def test_refusal_and_input_checks(self):
        with self.assertRaises(tone.ToneError):
            tone.generate("x", self.base, self.presets, client=FakeClient([good_answer()] * 2, stop_reason="refusal"))
        with self.assertRaises(tone.ToneError):
            tone.generate("   ", self.base, self.presets, client=FakeClient([]))
        with self.assertRaises(tone.ToneError):
            tone.generate("x" * 1001, self.base, self.presets, client=FakeClient([]))


class ToneWebTests(unittest.TestCase):
    def setUp(self):
        snap = Snapshot(version="t", active_index=1)
        snap.presets = make_presets(6)
        self.backend = MemoryBackend(snap)
        self.app = App(self.backend, {}, None)
        self.app.llm_client = FakeClient([good_answer()])
        self.srv = make_server(self.app, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-GE200-Token"] = self.app.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/tone", data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_tone_endpoint_returns_edit_and_never_touches_the_device(self):
        code, r = self.post({"slot": 2, "description": "dreamy clean", "edit": {}})
        self.assertEqual(code, 200)
        self.assertEqual(r["edit"]["name"], "DREAM CLEAN")
        self.assertEqual(self.backend.log, [])

    def test_tone_endpoint_errors_and_token(self):
        self.assertEqual(self.post({"slot": 2, "description": ""})[0], 400)
        self.assertEqual(self.post({"slot": 2, "description": 5})[0], 400)
        self.assertEqual(self.post({"slot": 2, "description": "x"}, token=False)[0], 403)

    def test_state_reports_tone_availability(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state") as r:
            self.assertTrue(json.loads(r.read())["tone"]["available"])


if __name__ == "__main__":
    unittest.main()
