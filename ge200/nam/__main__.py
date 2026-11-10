"""``python -m ge200.nam inspect|list|render|bench|make-test-model`` (render/bench need the NAM environment)."""
import argparse
import json
import sys

from .info import NamFileError, inspect_nam, scan_library


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ge200.nam", description="NAM engine tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect"); p.add_argument("model")
    p = sub.add_parser("list"); p.add_argument("directory")
    p = sub.add_parser("render")
    p.add_argument("model"); p.add_argument("input_wav"); p.add_argument("output_wav")
    p.add_argument("--ir"); p.add_argument("--gain-db", type=float, default=0.0)
    p.add_argument("--peak-db", type=float, default=-3.0)
    p = sub.add_parser("bench"); p.add_argument("model"); p.add_argument("--seconds", type=float, default=5.0)
    p = sub.add_parser("make-test-model"); p.add_argument("outdir"); p.add_argument("--channels", type=int, default=4)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "inspect":
            print(json.dumps(inspect_nam(a.model), indent=1))
        elif a.cmd == "list":
            for m in scan_library(a.directory):
                print(f"{m['name']:40} {m['architecture']:8} {m['sample_rate']} Hz  {m['gear_make'] or ''} {m['gear_model'] or ''}")
        elif a.cmd == "render":
            from .render import render
            print(json.dumps(render(a.model, a.input_wav, a.output_wav, a.ir, a.gain_db, a.peak_db)))
        elif a.cmd == "bench":
            from .render import benchmark
            print(json.dumps(benchmark(a.model, a.seconds)))
        elif a.cmd == "make-test-model":
            from .render import build_test_model
            print(build_test_model(a.outdir, channels=a.channels))
    except NamFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
