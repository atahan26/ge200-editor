"""Expression pedal assignment: decoding, editing, refusing bad values, tone generator handling (synthetic data)."""
import unittest

from ge200 import tone
from ge200.preset import (EXP_FIELDS, EXP_MODULE_KEYS, EXP_TARGETS, PRESET_SIZE, EditError, Preset, apply_edit,
                          exp_valid)
from tests.test_tone import good_answer, make_presets, FakeClient


def preset_with(exp1=(1, 0), exp2=(2, 5)):
    raw = bytearray(PRESET_SIZE)
    raw[0:9] = bytes(range(1, 10))
    raw[356], raw[357], raw[358], raw[359] = exp1 + exp2
    return Preset.from_bytes(0, bytes(raw))


class ExpTests(unittest.TestCase):
    def test_decoding_and_the_target_tables(self):
        p = preset_with((3, 1), (0, 0))
        self.assertEqual(p.exp, {"exp1_module": 3, "exp1_target": 1, "exp2_module": 0, "exp2_target": 0})
        self.assertEqual(p.to_dict()["exp"], p.exp)
        self.assertEqual(EXP_TARGETS["cab"][1], "DISTANCE")              # (3, 1) = CAB distance
        self.assertEqual(EXP_TARGETS["fx"][0], "POSITION")               # the wah position
        self.assertEqual(EXP_TARGETS["amp"][5], "MST")                   # (2, 5) = amp master, the usual default
        self.assertEqual(set(EXP_TARGETS), set(EXP_MODULE_KEYS))
        self.assertEqual({k: len(v) for k, v in EXP_TARGETS.items()},
                         {"fx": 10, "ds": 3, "amp": 6, "cab": 2, "ns": 4, "eq": 14, "mod": 12, "delay": 6, "reverb": 4})

    def test_edit_changes_only_the_four_bytes(self):
        p = preset_with()
        out = apply_edit(p.raw, {"exp": {"exp1_module": 0, "exp1_target": 3, "exp2_module": 7, "exp2_target": 2}})
        changed = [i for i in range(PRESET_SIZE) if out[i] != p.raw[i]]
        self.assertEqual(changed, [356, 357, 358, 359])
        self.assertEqual(Preset.from_bytes(0, out).exp, {"exp1_module": 0, "exp1_target": 3, "exp2_module": 7, "exp2_target": 2})
        self.assertEqual(apply_edit(p.raw, {"exp": p.exp}), p.raw)         # no-op edit changes nothing
        part = apply_edit(p.raw, {"exp": {"exp1_target": 2}})              # one key at a time is fine
        self.assertEqual(Preset.from_bytes(0, part).exp["exp1_target"], 2)

    def test_bad_values_are_refused(self):
        p = preset_with()
        for bad in ({"exp1_module": 9, "exp1_target": 0}, {"exp1_module": -1}, {"exp1_module": 3, "exp1_target": 2},
                    {"exp2_module": 1, "exp2_target": 3}, {"exp1_module": True}, {"nope": 1}, {"exp1_module": "1"}):
            with self.assertRaises(EditError, msg=str(bad)):
                apply_edit(p.raw, {"exp": bad})
        with self.assertRaises(EditError):
            apply_edit(p.raw, {"exp": 5})
        self.assertTrue(exp_valid(8, 3))
        self.assertFalse(exp_valid(8, 4))

    def test_every_value_in_the_range_is_accepted(self):
        p = preset_with()
        for mi, key in enumerate(EXP_MODULE_KEYS):
            for t in range(len(EXP_TARGETS[key])):
                out = apply_edit(p.raw, {"exp": {"exp1_module": mi, "exp1_target": t}})
                self.assertEqual((out[356], out[357]), (mi, t))


class ExpToneTests(unittest.TestCase):
    def setUp(self):
        self.presets = make_presets()
        self.base = self.presets[1]

    def test_schema_and_prompt_describe_exp(self):
        self.assertIn("exp", tone.SCHEMA["required"])
        self.assertEqual(set(tone.SCHEMA["properties"]["exp"]["properties"]), set(EXP_FIELDS))
        prompt = tone.build_system_prompt(self.presets)
        self.assertIn("exp1_module", prompt)
        self.assertIn("0=POSITION", prompt)
        self.assertIn("5=MST", prompt)

    def test_sanitize_keeps_valid_pairs_and_falls_back_for_invalid_ones(self):
        edit = json_answer(exp={"exp1_module": 0, "exp1_target": 0, "exp2_module": 3, "exp2_target": 9})
        out = tone.sanitize_edit(edit, self.base, self.presets)
        self.assertEqual((out["exp"]["exp1_module"], out["exp"]["exp1_target"]), (0, 0))                # taken
        self.assertEqual((out["exp"]["exp2_module"], out["exp"]["exp2_target"]),
                         (self.base.exp["exp2_module"], self.base.exp["exp2_target"]))                  # CAB has no target 9
        self.assertEqual(tone.sanitize_edit(json_answer(), self.base, self.presets)["exp"], self.base.exp)  # missing = base

    def test_generate_returns_the_exp_edit(self):
        fake = FakeClient([good_answer(exp={"exp1_module": 0, "exp1_target": 0, "exp2_module": 1, "exp2_target": 0})])
        edit = tone.generate("wah lead", self.base, self.presets, client=fake)["edit"]
        self.assertEqual(edit["exp"]["exp1_module"], 0)
        self.assertEqual(edit["exp"]["exp2_module"], 1)


def json_answer(**over):
    import json
    return json.loads(good_answer(**over))


if __name__ == "__main__":
    unittest.main()


class MergeTests(unittest.TestCase):
    def setUp(self):
        raw = bytearray(PRESET_SIZE)
        raw[0:9] = bytes(range(1, 10))
        self.base = bytes(raw)

    def test_decoding_layout_and_default(self):
        from ge200.preset import MERGE_SLOTS, merge_slot
        p = Preset.from_bytes(0, self.base)
        self.assertEqual((p.merge["on"], len(p.merge["heel"]), len(p.merge["toe"])), (False, MERGE_SLOTS, MERGE_SLOTS))
        self.assertEqual(merge_slot(2, 0), 12)        # AMP first knob (GAIN)
        self.assertEqual(merge_slot(3, 1), 19)        # CAB second knob, preset byte 55
        self.assertEqual(merge_slot(7, 4), 46)        # DELAY fifth knob, preset byte 90
        self.assertEqual(merge_slot(8, 5) + 1, MERGE_SLOTS)

    def test_edit_writes_heel_toe_and_flag_only(self):
        out = apply_edit(self.base, {"merge": {"on": True, "heel": {12: 21, "13": 60}, "toe": {12: 33, 13: 45}}})
        changed = {i: out[i] for i in range(PRESET_SIZE) if out[i] != self.base[i]}
        self.assertEqual(changed, {126: 1, 128 + 12: 21, 128 + 13: 60, 182 + 12: 33, 182 + 13: 45})
        p = Preset.from_bytes(0, out)
        self.assertTrue(p.merge["on"])
        self.assertEqual((p.merge["heel"][12], p.merge["toe"][13]), (21, 45))
        full = apply_edit(out, {"merge": p.merge})                         # a round trip changes nothing
        self.assertEqual(full, out)
        off = apply_edit(out, {"merge": {"on": False}})                    # switching off keeps the data
        self.assertEqual((off[126], off[128 + 12]), (0, 21))

    def test_bad_merge_values_are_refused(self):
        for bad in ({"on": 1}, {"heel": [0] * 53}, {"heel": {54: 1}}, {"heel": {-1: 1}}, {"toe": {0: 256}},
                    {"toe": {0: True}}, {"heel": 5}, {"nope": 1}, {"heel": {"x": 1}}):
            with self.assertRaises(EditError, msg=str(bad)):
                apply_edit(self.base, {"merge": bad})
        with self.assertRaises(EditError):
            apply_edit(self.base, {"merge": []})
