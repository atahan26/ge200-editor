"""Command line interface (read-only).

  ge200 devices                      list matching HID interfaces (no I/O)
  ge200 info                         firmware string, active preset (sends the editor's hello)
  ge200 list                         list all presets
  ge200 show N                       show one preset
  ge200 dump out.json [--raw f.bin]  export all presets (+ optionally the raw HID reports)
  ge200 select N --write             make preset N active                  (changes the device)
  ge200 write-slot N SRC --write     store a 512-byte image / JSON entry   (changes the device, persistent)

Every command accepts ``--from-raw FILE`` to work offline on a saved raw dump instead of the device,
and ``--names FILE`` with a local JSON model-name table (see tools/extract_names.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

from .client import Ge200, Snapshot, dump_raw, load_raw


def _load_names(path: Optional[str]) -> Optional[Dict[str, List[str]]]:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _snapshot(args) -> Snapshot:
    if args.from_raw:
        with open(args.from_raw, "rb") as f:
            return Snapshot.from_reports(load_raw(f.read()))
    with Ge200() as dev:
        snap = dev.read_state()
    if not snap.presets:
        raise SystemExit("the device did not return any presets")
    return snap


def _fmt_module(key: str, m, names) -> str:
    table = (names or {}).get(key)
    model = f" {table[m.type - 1]}" if table and 1 <= m.type <= len(table) else ""
    return f"{key:6} {'ON ' if m.enabled else 'off'} type {m.type:2}{model}  params {m.params}"


def cmd_devices(args) -> int:
    from .hid import find_device_paths
    paths = find_device_paths()
    for p in paths:
        print(p)
    if not paths:
        print("no GE200 found", file=sys.stderr)
    return 0 if paths else 1


def cmd_info(args) -> int:
    snap = _snapshot(args)
    print(f"firmware string : {snap.version or '?'}")
    print(f"presets         : {len(snap.presets)}")
    if snap.active_index is not None:
        act = snap.presets.get(snap.active_index)
        print(f"active preset   : {snap.active_index}" + (f" ({act.name})" if act else ""))
    if snap.bad_crc:
        print(f"warning: {snap.bad_crc} frame(s) failed the CRC check", file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    snap = _snapshot(args)
    for i in sorted(snap.presets):
        mark = "*" if i == snap.active_index else " "
        print(f"{mark}{i:3}  {snap.presets[i].name}")
    return 0


def cmd_show(args) -> int:
    snap = _snapshot(args)
    pr = snap.presets.get(args.index)
    if pr is None:
        raise SystemExit(f"no preset {args.index}")
    names = _load_names(args.names)
    print(f"preset {pr.index}: {pr.name}")
    print(f"chain order: {pr.chain_order}")
    for key, m in pr.modules.items():
        print("  " + _fmt_module(key, m, names))
    return 0


def cmd_dump(args) -> int:
    snap = _snapshot(args)
    names = _load_names(args.names)
    out = {
        "firmware_string": snap.version,
        "active_index": snap.active_index,
        "presets": [snap.presets[i].to_dict(names) for i in sorted(snap.presets)],
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {len(snap.presets)} presets to {args.output}")
    if args.raw:
        with open(args.raw, "wb") as f:
            f.write(dump_raw(snap.reports))
        print(f"wrote {len(snap.reports)} raw reports to {args.raw}")
    return 0


def _require_write_flag(args) -> None:
    if not args.write:
        raise SystemExit("this command changes the device: add --write to confirm")


def cmd_select(args) -> int:
    _require_write_flag(args)
    with Ge200(allow_write=True) as dev:
        dev.select_preset(args.slot)
        snap = dev.read_state()
    ok = snap.active_index == args.slot
    print(f"active preset is now {snap.active_index}" + ("" if ok else "  (expected " + str(args.slot) + ")"))
    return 0 if ok else 1


def cmd_write_slot(args) -> int:
    """Store a preset image in a slot. SOURCE is a 512-byte .bin or a JSON written by `dump` (then --source picks
    the preset index inside it, default = the slot number)."""
    _require_write_flag(args)
    if args.source.lower().endswith(".json"):
        with open(args.source, encoding="utf-8") as f:
            doc = json.load(f)
        want = args.source_index if args.source_index is not None else args.slot
        entry = next((p for p in doc["presets"] if p["index"] == want), None)
        if entry is None:
            raise SystemExit(f"preset {want} not in {args.source}")
        raw = bytes.fromhex(entry["raw"])
    else:
        with open(args.source, "rb") as f:
            raw = f.read()
    with Ge200(allow_write=True) as dev:
        before = dev.read_state()
        dev.write_slot(args.slot, raw)
        after = dev.read_state()
    stored = after.presets.get(args.slot)
    ok = stored is not None and stored.raw == raw
    others = all(after.presets[i].raw == before.presets[i].raw for i in before.presets if i != args.slot)
    print(f"slot {args.slot}: stored image matches: {ok}; other slots unchanged: {others}")
    return 0 if ok and others else 1


def cmd_ir_preview(args) -> int:
    """Offline: show how IR files survive the conversion to the device's 512-sample 44.1 kHz format."""
    import numpy as np
    import soundfile as sf
    from . import ir
    os.makedirs(args.out_dir, exist_ok=True) if args.out_dir else None
    for path in args.files:
        x, rate = ir.load_audio(path)
        y = ir.prepare(x, rate)
        print(f"{os.path.basename(path)}: {len(x)} samples @ {rate} Hz ({1000 * len(x) / rate:.0f} ms) -> 512 samples @ 44100 Hz; "
              f"peak {float(np.max(np.abs(y))):.3f}")
        if args.out_dir:
            out = os.path.join(args.out_dir, os.path.splitext(os.path.basename(path))[0] + " [GE200].wav")
            sf.write(out, y, 44100, subtype="PCM_24")
            print("   wrote", out)
    return 0


def cmd_ir_upload(args) -> int:
    """Upload an IR into one of the unit's 10 user IR slots (overwrites it). Shows which name table changed."""
    _require_write_flag(args)
    from . import ir
    x, rate = ir.load_audio(args.file)
    samples = ir.prepare(x, rate)
    name = args.name or args.file
    with Ge200(allow_write=True) as dev:
        before = dev.read_state().name_tables()
        dev.write_ir(args.slot, samples, name)
        after = dev.read_state().name_tables()
    for cmd, names in after.items():
        old = before.get(cmd, [])
        changed = [i for i, n in enumerate(names) if i >= len(old) or old[i] != n]
        print(f"table {cmd:#04x}: changed entries {changed}" + (f" -> {[names[i] for i in changed]}" if changed else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ge200", description="Read-only access to a Mooer GE200 (unofficial).")
    # common options are accepted both before and after the sub-command
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--from-raw", metavar="FILE", default=argparse.SUPPRESS,
                        help="use a saved raw dump instead of the device")
    common.add_argument("--names", metavar="FILE", default=argparse.SUPPRESS, help="local JSON model-name table")
    ap.add_argument("--from-raw", metavar="FILE", default=None)
    ap.add_argument("--names", metavar="FILE", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", parents=[common]).set_defaults(fn=cmd_devices)
    sub.add_parser("info", parents=[common]).set_defaults(fn=cmd_info)
    sub.add_parser("list", parents=[common]).set_defaults(fn=cmd_list)
    s = sub.add_parser("show", parents=[common])
    s.add_argument("index", type=int)
    s.set_defaults(fn=cmd_show)
    w = argparse.ArgumentParser(add_help=False)
    w.add_argument("--write", action="store_true", help="confirm that this command changes the device")
    sel = sub.add_parser("select", parents=[common, w], help="make a slot the active preset")
    sel.add_argument("slot", type=int)
    sel.set_defaults(fn=cmd_select)
    ws = sub.add_parser("write-slot", parents=[common, w], help="store a preset image in a slot")
    ws.add_argument("slot", type=int)
    ws.add_argument("source", help="512-byte .bin or a JSON from `dump`")
    ws.add_argument("--source-index", type=int, default=None, help="preset index inside the JSON")
    ws.set_defaults(fn=cmd_write_slot)
    ip = sub.add_parser("ir-preview", help="offline: convert IR files to the device format and report")
    ip.add_argument("files", nargs="+")
    ip.add_argument("--out-dir", help="also write the converted 512-sample WAVs here")
    ip.set_defaults(fn=cmd_ir_preview)
    iu = sub.add_parser("ir-upload", parents=[common, w], help="upload an IR file to a user IR slot 0..9 (UNVERIFIED on a device)")
    iu.add_argument("slot", type=int)
    iu.add_argument("file")
    iu.add_argument("--name", help="name shown on the unit (15 characters)")
    iu.set_defaults(fn=cmd_ir_upload)
    d = sub.add_parser("dump", parents=[common])
    d.add_argument("output")
    d.add_argument("--raw", metavar="FILE", help="also save the raw HID reports")
    d.set_defaults(fn=cmd_dump)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
