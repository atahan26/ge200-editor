"""Local web editor for the GE200 (stdlib only).

Safety: binds to 127.0.0.1, checks the Host header (DNS-rebinding), requires a per-run token on every POST
(so other web pages cannot make your browser write to the device), and saves a full backup before the first
write of a session.

Live editing: slider moves are *coalesced* (only the newest state is ever sent, by one worker thread) and are
tagged with the UI "epoch" (bumped whenever you open another preset), so a queued update for a preset you have
already left can never be applied to the device.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

from .. import params as param_info
from .. import protocol as p
from .. import rig as rig_mod
from .. import llm, tone
from ..client import Ge200, Snapshot, load_raw
from ..preset import EditError, Preset, apply_edit, param_ranges

STATIC = Path(__file__).with_name("static")
IDLE_RESELECT_S = 4.0     # after this long without device traffic, re-select the slot before a live update


class MemoryBackend:
    """In-memory stand-in for a device (tests, UI demos with a saved dump)."""

    def __init__(self, snap: Snapshot) -> None:
        self.version, self.active = snap.version, snap.active_index or 0
        self.presets = {i: p.raw for i, p in snap.presets.items()}
        self.live = self.presets[self.active]
        self.log: List[str] = []

    gen = 0
    cheap_verify = True

    def refresh(self, wait: bool = True, timeout: float = 0) -> int:
        self.gen += 1
        return self.gen

    def snapshot(self) -> Snapshot:
        snap = Snapshot(version=self.version, active_index=self.active)
        snap.presets = {i: Preset.from_bytes(i, r) for i, r in self.presets.items()}
        return snap

    def verified(self, slot: int, gen: int) -> bool:
        return True

    def select(self, slot: int) -> None:
        self.active, self.live = slot, self.presets[slot]
        self.log.append(f"select {slot}")

    def load_live(self, raw: bytes) -> None:
        self.live = raw
        self.log.append("live")

    def write_slot(self, slot: int, raw: bytes) -> None:
        self.presets[slot] = raw
        self.log.append(f"write {slot}")

    def close(self) -> None:
        pass


class DeviceBackend:
    """One HID handle kept open, with a reader thread that consumes everything the device sends.

    The device answers a hello with its whole state at only ~12 presets per second (about 16 s), so nothing
    waits for that unless it has to: the reader thread keeps the cached snapshot up to date and marks, per
    slot, in which "generation" (hello number) the slot was last received (``fresh``).
    """

    cheap_verify = False       # a full read-back takes ~16 s, so saves are not re-read automatically

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self._dev: Optional[Ge200] = None
        self._reader: Optional[threading.Thread] = None
        self.snap = Snapshot()
        self.fresh: dict = {}
        self.gen = 0
        self.rx = 0                # number of input reports seen so far
        self.last_rx = 0.0
        self.dumping = False       # the device is (probably) still streaming its state
        self.error: Optional[str] = None

    # ----- connection / reader thread -----
    def _ensure(self) -> Ge200:
        if self._dev is None:
            dev = Ge200(allow_write=True)
            dev.passive_reads = True
            self._dev = dev
            self._reader = threading.Thread(target=self._read_loop, args=(dev,), daemon=True)
            self._reader.start()
        return self._dev

    def _read_loop(self, dev: Ge200) -> None:
        parser = p.StreamParser()
        while self._dev is dev:
            try:
                report = dev.hid.read_report(200)
                if not report:
                    continue
                with self.cond:
                    self.rx += 1
                    self.last_rx = time.time()
                for f in parser.feed_report(report):
                    if not f.crc_ok:
                        continue
                    with self.cond:
                        self.snap._ingest(f)
                        if f.cmd == p.CMD_PRESET and f.data:
                            self.fresh[f.data[0]] = self.gen
                            if self._complete(self.gen):
                                self.dumping = False
                        self.cond.notify_all()
            except Exception as e:                       # never let the reader die silently
                self.error = f"reader stopped: {type(e).__name__}: {e}"
                break
        with self.cond:
            if self._dev is dev:
                self._dev = None           # broken handle: reopened on next use
            self.cond.notify_all()

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if self._reader is not None:
            self._reader.join(1.0)
        if dev is not None:
            dev.close()

    def _run(self, fn):
        for attempt in (0, 1):
            try:
                return fn(self._ensure())
            except OSError:
                self.close()
                if attempt:
                    raise

    # ----- state -----
    def _flush_device_parser(self) -> None:
        """The device ignores everything while its input parser waits for the rest of a truncated frame
        (seen after a write collided with the state dump). A few zero chunks complete any such frame."""
        filler = bytes([63]) + bytes(63)
        for _ in range(11):
            self._run(lambda d: d.hid.write_report(filler))
        time.sleep(0.5)

    def refresh(self, wait: bool = True, timeout: float = 40.0) -> int:
        """Send a hello. With ``wait`` block until all 200 presets of this generation have arrived."""
        for attempt in range(3):
            self._wait_idle()
            with self.cond:
                self.gen += 1
                gen = self.gen
                rx0 = self.rx
                self.dumping = True
            self._run(lambda d: d.send_hello())
            if not wait:
                return gen
            with self.cond:                                    # the device must start answering quickly
                started = self.cond.wait_for(lambda: self.rx > rx0, 5.0)
            if started:
                with self.cond:
                    ok = self.cond.wait_for(lambda: self._complete(gen), timeout)
                if ok:
                    return gen
                break
            self.error = "no answer to hello, flushing the device parser"
            self._flush_device_parser()
        with self.cond:
            self.dumping = False
        raise RuntimeError(f"incomplete state from the device ({len(self.snap.presets)} presets)")

    def _wait_idle(self, timeout: float = 25.0) -> None:
        """Never write while the device is streaming its state: a write that collides with the dump can be
        truncated and leaves the device's input parser stuck."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.cond:
                quiet = time.time() - self.last_rx > 2.0
                if not self.dumping or quiet:
                    self.dumping = False
                    return
            time.sleep(0.05)

    def _complete(self, gen: int) -> bool:
        return (sum(1 for g in self.fresh.values() if g >= gen) >= 200
                and self.snap.active_index is not None and bool(self.snap.version))

    def snapshot(self) -> Snapshot:
        return self.snap

    def verified(self, slot: int, gen: int) -> bool:
        return self.fresh.get(slot, 0) >= gen

    # ----- writes -----
    def select(self, slot: int) -> None:
        self._wait_idle()
        self._run(lambda d: d.select_preset(slot, settle_s=0.4))
        with self.cond:
            self.snap.active_index = slot

    def load_live(self, raw: bytes) -> None:
        self._wait_idle()
        self._run(lambda d: d.load_live(raw, settle_s=0.05))

    def write_slot(self, slot: int, raw: bytes) -> None:
        self._wait_idle()
        self._run(lambda d: d.write_slot(slot, raw, settle_s=1.0))
        with self.cond:                    # optimistic; replaced by the device's own copy on the next dump
            self.snap.presets[slot] = Preset.from_bytes(slot, raw)


class App:
    def __init__(self, backend, names=None, backup_dir: Optional[str] = None, rig_path=None, nam=None) -> None:
        self.nam = nam                                              # NamLab or None
        self.backend, self.names = backend, names or {}
        self.backup_dir = backup_dir
        self.rig_path = Path(rig_path) if rig_path else None        # None = keep the rig in memory only
        self.rig: dict = rig_mod.load(self.rig_path) if self.rig_path else {}
        self.lock = threading.RLock()            # serialises all device access
        self.cv = threading.Condition()          # guards epoch / pending
        self.token = secrets.token_hex(16)
        self.snap: Optional[Snapshot] = None
        self.live_slot: Optional[int] = None     # slot whose working copy we believe the device is on
        self.last_op = 0.0
        self.backed_up = False
        self.epoch = 0
        self.pending = None                      # (epoch, slot, raw) newest live update not yet sent
        self.last_error: Optional[str] = None
        self.verify: Optional[dict] = None       # read-back check of the last save
        self.llm_client = None                   # injected in tests (an Anthropic-style client)
        self.llm_provider = None                 # injected in tests (an llm.Provider)
        self.log = collections.deque(maxlen=300)
        self.refresh()
        self._stop = False
        threading.Thread(target=self._worker, daemon=True).start()

    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}")

    def refresh(self, wait: bool = True) -> Snapshot:
        """Ask the device for its state. ``wait`` blocks until all presets arrived (~16 s on real hardware)."""
        with self.lock:
            t0 = time.time()
            self.backend.refresh(wait=wait)
            self.snap = self.backend.snapshot()
            if wait and not self.snap.presets:
                raise RuntimeError("the device returned no presets")
            self.live_slot = self.snap.active_index
            self.last_op = time.time()
            self._log(f"refresh(wait={wait}): active={self.snap.active_index}, {time.time() - t0:.1f}s")
            return self.snap

    def state(self) -> dict:
        s = self.snap
        return {
            "firmware": s.version,
            "active": s.active_index,
            "presets": [{"index": i, "name": s.presets[i].name} for i in sorted(s.presets)],
            "names": self.names,
            "ranges": param_ranges(list(s.presets.values())),
            "backup_dir": self.backup_dir,
            "backed_up": self.backed_up,
            "tone": ({"available": True, "reason": "", "default": "anthropic", "providers": []}
                     if (self.llm_client is not None or self.llm_provider is not None) else tone.available()),
            "rig": self.rig,
            "rig_fields": rig_mod.GENERAL_FIELDS,
            "guitar_fields": rig_mod.GUITAR_FIELDS,
            "params": param_info.PARAMS,
            "model_labels": {k: {str(t): labs for t, labs in v.items()} for k, v in param_info.MODEL_LABELS.items()},
            "wide_info": param_info.WIDE_INFO,
            "note_divisions": param_info.NOTE_DIVISIONS,
        }

    def stored(self, slot: int) -> Preset:
        if slot not in self.snap.presets:
            raise EditError(f"no preset {slot}")
        return self.snap.presets[slot]

    def set_rig(self, rig: dict) -> dict:
        cleaned = rig_mod.clean(rig)
        if self.rig_path:
            rig_mod.save(cleaned, self.rig_path)
        self.rig = cleaned
        self._log("rig saved")
        return self.rig

    def tone(self, slot: int, description: str, base_edit: Optional[dict] = None,
             guitar_index: Optional[int] = None, provider: Optional[str] = None,
             model: Optional[str] = None) -> dict:
        """Ask the LLM for a preset edit. Never touches the device; takes no device lock (the call can take a minute)."""
        base = self.stored(slot)
        if base_edit:                                   # start from what is currently in the editor
            base = Preset.from_bytes(slot, apply_edit(base.raw, base_edit))
        guitar = None
        if guitar_index is not None:
            names = rig_mod.guitar_names(self.rig)
            if not isinstance(guitar_index, int) or not 0 <= guitar_index < len(names):
                raise EditError("unknown guitar")
            guitar = f"Guitar {guitar_index + 1}, {names[guitar_index]}"
        if provider is not None and not (isinstance(provider, str) and provider.isalnum()):
            raise EditError("invalid provider")
        if model is not None and (not isinstance(model, str) or (model and not llm.MODEL_RE.match(model))):
            raise EditError("invalid model name")
        self._log(f"tone request for slot {slot} ({len(description)} chars, guitar={guitar}, "
                  f"provider={provider or 'default'})")
        res = tone.generate(description, base, dict(self.snap.presets), self.names, client=self.llm_client,
                            provider=self.llm_provider or provider or None, model=model or None,
                            rig=self.rig, guitar=guitar)
        self._log("tone: ok")
        return res

    def backup(self) -> str:
        if not self.backup_dir:
            raise EditError("no backup directory configured (--backup-dir)")
        os.makedirs(self.backup_dir, exist_ok=True)
        path = os.path.join(self.backup_dir, datetime.datetime.now().strftime("ge200-backup-%Y%m%d-%H%M%S.json"))
        doc = {"firmware_string": self.snap.version, "active_index": self.snap.active_index,
               "presets": [self.snap.presets[i].to_dict() for i in sorted(self.snap.presets)]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.backed_up = True
        return path

    # ----- epoch handling -----
    def _advance_epoch(self, epoch: int) -> bool:
        """Register a UI epoch; returns False if it is older than one we already know (stale request)."""
        with self.cv:
            if epoch < self.epoch:
                return False
            if epoch > self.epoch:
                self.epoch = epoch
            if self.pending and self.pending[0] < self.epoch:
                self.pending = None
            return True

    def select(self, slot: int, epoch: int) -> dict:
        self.stored(slot)
        if not self._advance_epoch(epoch):
            self._log(f"select {slot} dropped (stale epoch {epoch})")
            return {"dropped": True}
        with self.lock:
            with self.cv:
                if epoch < self.epoch:
                    return {"dropped": True}
            self.backend.select(slot)
            self.snap.active_index = self.live_slot = slot
            self.last_op = time.time()
            self._log(f"select {slot} (epoch {epoch})")
            return {"active": slot}

    def revert(self, slot: int, epoch: int) -> dict:
        return self.select(slot, epoch)

    def live(self, slot: int, edit: dict, epoch: int) -> dict:
        raw = apply_edit(self.stored(slot).raw, edit)          # validates synchronously
        with self.cv:
            if epoch < self.epoch:
                self._log(f"live {slot} dropped (stale epoch {epoch})")
                return {"dropped": True}
            self.epoch = max(self.epoch, epoch)
            self.pending = (epoch, slot, raw)
            self.cv.notify()
        err, self.last_error = self.last_error, None
        return {"queued": True, "error": err}

    def _worker(self) -> None:
        while not self._stop:
            with self.cv:
                while self.pending is None:
                    self.cv.wait()
                epoch, slot, raw = self.pending
                self.pending = None
            with self.lock:
                with self.cv:
                    if epoch < self.epoch:
                        self._log(f"live {slot} skipped (stale epoch {epoch})")
                        continue
                try:
                    if self.live_slot != slot or time.time() - self.last_op > IDLE_RESELECT_S:
                        self.backend.select(slot)
                        self.live_slot = slot
                        self._log(f"worker select {slot}")
                    self.backend.load_live(raw)
                    self.last_op = time.time()
                    self._log(f"live {slot} sent")
                except Exception as e:  # report to the UI on its next call
                    self.last_error = f"{type(e).__name__}: {e}"
                    self._log(f"live failed: {self.last_error}")

    def save(self, slot: int, edit: dict) -> dict:
        with self.lock:
            with self.cv:
                self.pending = None
            raw = apply_edit(self.stored(slot).raw, edit)
            if raw == self.stored(slot).raw:       # identical to what is stored: do not wear the flash for nothing
                self._log(f"save {slot}: unchanged, nothing written")
                return {"ok": True, "pending": False, "unchanged": True,
                        "preset": self.stored(slot).to_dict(self.names)}
            if not self.backed_up:
                if not self.backup_dir:
                    raise EditError("refusing to write without a backup directory (--backup-dir)")
                self.backup()
            t0 = time.time()
            self.backend.write_slot(slot, raw)
            self.last_op = time.time()
            if getattr(self.backend, "cheap_verify", False):
                gen = self.backend.refresh(wait=False)
                self.verify = {"slot": slot, "raw": raw, "gen": gen}
            else:                      # a real device needs ~16 s for a read-back: left to the Refresh button
                self.verify = None
            self.snap = self.backend.snapshot()
            self.live_slot = self.snap.active_index
            st = self.verify_status()
            self._log(f"save {slot}: written in {time.time() - t0:.2f}s, verification={st['state']}")
            return {"ok": {"ok": True, "mismatch": False}.get(st["state"]), "pending": st["state"] == "pending",
                    "preset": self.snap.presets[slot].to_dict(self.names)}

    def verify_status(self) -> dict:
        v = self.verify
        if not v:
            return {"state": "none"}
        if not self.backend.verified(v["slot"], v["gen"]):
            return {"state": "pending", "slot": v["slot"]}
        ok = self.backend.snapshot().presets[v["slot"]].raw == v["raw"]
        if v.get("logged") is None:
            v["logged"] = True
            self._log(f"verify {v['slot']}: {'ok' if ok else 'MISMATCH'}")
        return {"state": "ok" if ok else "mismatch", "slot": v["slot"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ge200-web"

    def log_message(self, fmt, *args):  # quiet
        pass

    @property
    def app(self) -> App:
        return self.server.app

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        try:
            if self.path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.app.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(200, self.app.state())
            if self.path == "/api/log":
                return self._json(200, list(self.app.log))
            if self.path == "/api/nam":
                if self.app.nam is None:
                    return self._json(200, {"available": False, "reason": "NAM lab not configured (--nam-dir)",
                                            "models": [], "irs": [], "dis": [], "folders": {}})
                return self._json(200, self.app.nam.listing())
            if self.path.startswith("/api/nam/audio/"):
                if self.app.nam is None:
                    return self._json(404, {"error": "not found"})
                try:
                    return self._send(200, self.app.nam.audio(self.path.rsplit("/", 1)[1]), "audio/wav")
                except KeyError:
                    return self._json(404, {"error": "no such render"})
            if self.path == "/api/status":
                return self._json(200, {"verify": self.app.verify_status(), "error": self.app.last_error,
                                        "reader_error": getattr(self.app.backend, "error", None)})
            if self.path.startswith("/api/preset/"):
                slot = int(self.path.rsplit("/", 1)[1])
                return self._json(200, self.app.stored(slot).to_dict(self.app.names))
            self._json(404, {"error": "not found"})
        except (EditError, ValueError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # device problems etc.
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if not secrets.compare_digest(self.headers.get("X-GE200-Token", ""), self.app.token):
            return self._json(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 100_000:
                return self._json(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            slot = body.get("slot")
            if self.path == "/api/rig":
                return self._json(200, {"rig": self.app.set_rig(body.get("rig"))})
            if self.path == "/api/nam/render":
                if self.app.nam is None:
                    raise ValueError("NAM lab not configured (--nam-dir)")
                return self._json(200, self.app.nam.render(str(body.get("model", "")), str(body.get("ir", "")),
                                                           str(body.get("di", "")), body.get("gain_db", 0)))
            if self.path not in ("/api/refresh", "/api/backup") and (
                    not isinstance(slot, int) or not 0 <= slot <= 199):
                raise EditError("slot must be an integer 0..199")
            edit = body.get("edit", {})
            if not isinstance(edit, dict):
                raise EditError("edit must be an object")
            epoch = body.get("epoch", 0)
            if not isinstance(epoch, int):
                raise EditError("epoch must be an integer")
            if self.path == "/api/select":
                return self._json(200, self.app.select(slot, epoch))
            if self.path == "/api/live":
                return self._json(200, self.app.live(slot, edit, epoch))
            if self.path == "/api/save":
                return self._json(200, self.app.save(slot, edit))
            if self.path == "/api/revert":
                return self._json(200, self.app.revert(slot, epoch))
            if self.path == "/api/refresh":
                self.app._advance_epoch(epoch)
                self.app.refresh()
                return self._json(200, self.app.state())
            if self.path == "/api/backup":
                return self._json(200, {"path": self.app.backup()})
            if self.path == "/api/tone":
                desc = body.get("description")
                if not isinstance(desc, str):
                    raise EditError("description must be a string")
                return self._json(200, self.app.tone(slot, desc, edit or None, body.get("guitar"),
                                                         body.get("provider") or None, body.get("model") or None))
            self._json(404, {"error": "not found"})
        except (EditError, ValueError, KeyError, json.JSONDecodeError, tone.ToneError, llm.LlmError, rig_mod.RigError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


def make_server(app: App, port: int = 8200) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.app = app
    return srv


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ge200.web", description="Local GE200 web editor")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--names", metavar="FILE", help="local JSON model-name table (tools/extract_names.py)")
    ap.add_argument("--backup-dir", metavar="DIR", required=True, help="where the automatic pre-write backup goes")
    ap.add_argument("--demo-raw", metavar="FILE", help="use a saved raw dump in memory instead of the device")
    ap.add_argument("--nam-dir", metavar="DIR", help="folder with your .nam captures (enables the NAM lab)")
    ap.add_argument("--ir-dir", metavar="DIR", help="folder with cabinet IR wav files")
    ap.add_argument("--di-dir", metavar="DIR", help="folder with DI guitar recordings to render")
    ap.add_argument("--nam-python", metavar="EXE", help="python of the NAM environment (default: <repo>/.venv-nam)")
    args = ap.parse_args(argv)
    names = json.load(open(args.names, encoding="utf-8")) if args.names else {}
    if args.demo_raw:
        backend = MemoryBackend(Snapshot.from_reports(load_raw(open(args.demo_raw, "rb").read())))
    else:
        backend = DeviceBackend()
    from ..env import load_env
    load_env()
    nam = None
    if args.nam_dir:
        from .namlab import NamLab
        py = args.nam_python or str(Path(__file__).resolve().parents[2] / ".venv-nam" / "Scripts" / "python.exe")
        nam = NamLab(args.nam_dir, args.ir_dir, args.di_dir, py)
    app = App(backend, names, args.backup_dir, rig_mod.default_path(), nam)
    srv = make_server(app, args.port)
    print(f"GE200 editor on http://127.0.0.1:{srv.server_address[1]}/  ({'DEMO, in memory' if args.demo_raw else 'device'})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
