import contextlib
import io
import json
import os
import tempfile
import unittest

from ge200 import cli


def dump(path, presets):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"presets": [{"index": i, "name": n, "raw": raw.hex()} for i, (n, raw) in presets.items()]}, f)


class DiffTests(unittest.TestCase):
    def test_lists_only_changed_bytes(self):
        base = bytes(512)
        changed = bytearray(base)
        changed[104], changed[140] = 1, 33
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
            dump(a, {0: ("ONE", base), 1: ("TWO", base)})
            dump(b, {0: ("ONE", base), 1: ("TWO", bytes(changed))})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["diff", a, b]), 0)
        text = out.getvalue()
        self.assertIn("preset 1 (TWO): 2 byte(s) differ", text)
        self.assertIn("[104]   0 ->   1", text)
        self.assertIn("[140]   0 ->  33", text)
        self.assertNotIn("preset 0", text)
        self.assertIn("1 preset(s) differ, 1 identical", text)


if __name__ == "__main__":
    unittest.main()
