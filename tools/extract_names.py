"""Build a LOCAL model-name table from the vendor editor you have installed.

The names are vendor data, so this repository does not ship them: run this on your own machine and keep
the output outside the repo (e.g. E:\\mooer-data\\vendor\\model_names.json), then pass it to the CLI with
``--names``. The script only reads the editor executable.

Usage: python tools/extract_names.py "<path to MOOER_GE200.exe>" model_names.json
"""
import json
import re
import sys

# (key, first entry, number of entries) -- tables are stored back to back as UTF-16 strings
TABLES = [
    ("fx", "CRY WAH", 8),
    ("ds", "TUBE DR", 20),
    ("amp", "65 US DX", None),     # up to the first cab name
    ("cab", "US DLX 112", None),   # up to the first "OFF" (power tube list)
    ("tube", "OFF", 5),
    ("mic", "SM57", 10),
    ("ns", "NOISE KILLER", 3),
    ("eq", "MOOER G", 4),
    ("mod", "PHASER", 20),
    ("delay", "DIGITAL", 9),
    ("reverb", "ROOM", 7),
]


def main(exe: str, out: str) -> None:
    data = open(exe, "rb").read()
    strings = [m.group().decode("utf-16le") for m in re.finditer(rb"(?:[ -~]\x00){2,}", data)]
    result = {}
    for key, first, count in TABLES:
        start = strings.index(first, strings.index("CRY WAH"))
        if count is None:
            stop_at = {"amp": "US DLX 112", "cab": "OFF"}[key]
            end = strings.index(stop_at, start + 1)
            result[key] = [s.strip() for s in strings[start:end]]
        else:
            result[key] = [s.strip() for s in strings[start:start + count]]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print({k: len(v) for k, v in result.items()})


if __name__ == "__main__":
    main(*sys.argv[1:3])
