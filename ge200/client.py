"""High-level, read-only client: send the hello and collect the device's state dump."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import protocol as p
from .preset import PRESET_SIZE, Preset


@dataclass
class Snapshot:
    version: str = ""
    active_index: Optional[int] = None
    presets: Dict[int, Preset] = field(default_factory=dict)
    frames: List[p.Frame] = field(default_factory=list)
    reports: List[bytes] = field(default_factory=list)   # raw 64-byte reports, in arrival order
    bad_crc: int = 0

    @classmethod
    def from_reports(cls, reports: List[bytes]) -> "Snapshot":
        snap = cls(reports=list(reports))
        parser = p.StreamParser()
        for r in reports:
            for f in parser.feed_report(r):
                if not f.crc_ok:
                    snap.bad_crc += 1
                    continue
                snap.frames.append(f)
                snap._ingest(f)
        return snap

    def name_tables(self) -> Dict[int, List[str]]:
        """The two 10-entry name tables of the hello reply (0x85 and 0xE3): the unit's IR names and amp-capture names
        (which is which is inferred from the content, and is checked by watching which table changes on an IR upload)."""
        out: Dict[int, List[str]] = {}
        for f in self.frames:
            if f.cmd in (0x85, 0xE3) and len(f.data) == 150:
                out[f.cmd] = [f.data[i:i + 15].split(b"\x00")[0].decode("latin-1").strip() for i in range(0, 150, 15)]
        return out

    def _ingest(self, f: p.Frame) -> None:
        if f.cmd == p.CMD_PRESET and len(f.data) == 1 + PRESET_SIZE:
            self.presets[f.data[0]] = Preset.from_bytes(f.data[0], f.data[1:])
        elif f.cmd == p.CMD_ACTIVE_PRESET and len(f.data) == 1:
            self.active_index = f.data[0]
        elif f.cmd == p.CMD_VERSION:
            self.version = f.data.split(b"\x00")[0].decode("latin-1")


def load_raw(data: bytes) -> List[bytes]:
    """Parse a raw dump file: concatenated reports, either 64-byte or 65-byte (leading report ID) records."""
    size = 65 if (len(data) % 65 == 0 and data[:2] and data[0] == 0 and data[1] <= 63) else 64
    return [data[i + size - 64:i + size] for i in range(0, len(data) - size + 1, size)]


def dump_raw(reports: List[bytes]) -> bytes:
    return b"".join(reports)


CMD_LOAD_LIVE = 0x83     # [512-byte preset]       -> replaces the working copy (not stored)
CMD_WRITE_SLOT = 0xE5    # [slot][512-byte preset] -> stores the preset in a slot
CMD_SELECT = 0xA6        # [slot]                  -> makes a slot the active preset


class WriteNotAllowed(RuntimeError):
    pass


class Ge200:
    """Access to a GE200.

    Reading only ever sends the vendor editor's own hello packet. The write methods (select/load/store) were
    each verified on a real device (see PROTOCOL.md) but change device state, so they need ``allow_write=True``.
    Nothing here can touch firmware.
    """

    def __init__(self, hid=None, allow_write: bool = False) -> None:
        if hid is None:
            from .hid import HidDevice
            hid = HidDevice()
        self.hid = hid
        self.allow_write = allow_write
        self.passive_reads = False   # True when another thread owns all reads (e.g. the web editor's reader)

    # ----- writes (guarded) -----
    def send_hello(self) -> None:
        """Ask the device for its full state; the answer (about 16 s of frames) arrives via the read side."""
        for report in p.frame_to_reports(p.build_frame(p.CMD_HELLO)):
            self.hid.write_report(report)

    def _send(self, cmd: int, data: bytes, settle_s: float) -> None:
        if not self.allow_write:
            raise WriteNotAllowed("open the device with Ge200(allow_write=True) to change its state")
        if not self.passive_reads:
            self.hid.start_read()
        for report in p.frame_to_reports(p.build_frame(cmd, data)):
            self.hid.write_report(report)
        time.sleep(settle_s)

    def select_preset(self, slot: int, settle_s: float = 0.7) -> None:
        if not 0 <= slot <= 199:
            raise ValueError("slot must be 0..199")
        self._send(CMD_SELECT, bytes([slot]), settle_s)

    def load_live(self, raw: bytes, settle_s: float = 1.0) -> None:
        """Replace the working copy of the active preset (not stored; the editor's name field is not applied)."""
        if len(raw) != PRESET_SIZE:
            raise ValueError(f"preset must be {PRESET_SIZE} bytes")
        self._send(CMD_LOAD_LIVE, bytes(raw), settle_s)

    def write_slot(self, slot: int, raw: bytes, settle_s: float = 2.0) -> None:
        """Store a 512-byte preset image in a slot (persistent)."""
        if not 0 <= slot <= 199:
            raise ValueError("slot must be 0..199")
        if len(raw) != PRESET_SIZE:
            raise ValueError(f"preset must be {PRESET_SIZE} bytes")
        self._send(CMD_WRITE_SLOT, bytes([slot]) + bytes(raw), settle_s)

    # ----- impulse responses (frames deduced from the vendor editor; see PROTOCOL.md, "IR slots") -----
    def write_ir(self, slot: int, samples, name: str, settle_s: float = 0.25) -> None:
        """Upload an IR (exactly 512 floats in -1..1) to a user IR slot 0..9. Overwrites what the slot holds."""
        from . import ir
        for payload in ir.write_payloads(slot, samples, name):
            self._send(ir.CMD_IR_WRITE, payload, settle_s)
        time.sleep(0.5)

    def clear_ir(self, slot: int) -> None:
        from . import ir
        self._send(ir.CMD_IR_CLEAR, ir.clear_payload(slot), 0.5)

    def close(self) -> None:
        self.hid.close()

    def __enter__(self) -> "Ge200":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def drain(self, max_s: float = 1.0) -> int:
        """Throw away reports that are already waiting (needed when the handle is kept open between calls)."""
        n, t0 = 0, time.time()
        while time.time() - t0 < max_s and self.hid.read_report(20):
            n += 1
        return n

    @staticmethod
    def _complete(snap: "Snapshot") -> bool:
        return len(snap.presets) >= 200 and snap.active_index is not None and bool(snap.version) and not snap.bad_crc

    def read_state(self, quiet_s: float = 1.5, timeout_s: float = 20.0, attempts: int = 3) -> Snapshot:
        """Read the full state; a dump that is missing frames (seen once on a cold handle) is read again."""
        best = Snapshot()
        self.last_read_attempts = 0
        for _ in range(attempts):
            self.last_read_attempts += 1
            snap = self._read_state_once(quiet_s, timeout_s)
            if self._complete(snap):
                return snap
            if len(snap.presets) >= len(best.presets):
                best = snap
            self.drain(0.5)
        return best

    def _read_state_once(self, quiet_s: float, timeout_s: float) -> Snapshot:
        self.hid.start_read()                      # queue a read first so nothing is missed
        for report in p.frame_to_reports(p.build_frame(p.CMD_HELLO)):
            self.hid.write_report(report)
        reports: List[bytes] = []
        t0 = last = time.time()
        while time.time() - t0 < timeout_s:
            r = self.hid.read_report(200)
            if r:
                reports.append(r)
                last = time.time()
            elif reports and time.time() - last > quiet_s:
                break
        return Snapshot.from_reports(reports)
