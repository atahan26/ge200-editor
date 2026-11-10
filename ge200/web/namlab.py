"""The "NAM lab" of the web editor: pick a NAM capture, an optional cabinet IR and a DI recording, render and listen.

Rendering runs in a subprocess of the separate NAM environment (``.venv-nam``), so PyTorch never loads into the web
server. Only files that sit in the configured library folders can be chosen (names are validated against a listing,
never used as paths directly).
"""
from __future__ import annotations

import json
import math
import os
import secrets
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..nam.info import scan_library

AUDIO_EXT = (".wav", ".flac", ".aif", ".aiff")
BUILTIN_DI = "(built-in test chord)"


def _list_audio(directory: Optional[str]) -> List[str]:
    if not directory or not os.path.isdir(directory):
        return []
    out = []
    for root, _d, files in os.walk(directory):
        for fn in files:
            if fn.lower().endswith(AUDIO_EXT):
                out.append(os.path.relpath(os.path.join(root, fn), directory).replace("\\", "/"))
    return sorted(out, key=str.lower)


def make_test_chord(path: str, seconds: float = 4.0, rate: int = 44100) -> None:
    """A plucked power-chord-like DI substitute (Karplus-Strong strings), 16-bit mono WAV, stdlib only."""
    notes = [82.41, 123.47, 164.81]                       # E2, B2, E3
    n = int(seconds * rate)
    mix = [0.0] * n
    for f in notes:
        period = int(rate / f)
        buf = [(((i * 2654435761) >> 7) & 0xFFFF) / 32768.0 - 1.0 for i in range(period)]   # deterministic noise burst
        for i in range(n):
            j = i % period
            nxt = buf[(j + 1) % period]
            buf[j] = 0.5 * (buf[j] + nxt) * 0.9985
            mix[i] += buf[j]
    peak = max(abs(v) for v in mix) or 1.0
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v / peak * 0.5)) * 32767)) for v in mix))


def default_runner(python_exe: str, args: List[str], timeout: int = 600) -> dict:
    proc = subprocess.run([python_exe, "-I", "-m", "ge200.nam", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "render failed").strip().splitlines()[-1][:300])
    return json.loads(proc.stdout.strip().splitlines()[-1])


class NamLab:
    def __init__(self, nam_dir: Optional[str], ir_dir: Optional[str], di_dir: Optional[str],
                 python_exe: Optional[str], runner: Callable = default_runner) -> None:
        self.nam_dir, self.ir_dir, self.di_dir = nam_dir, ir_dir, di_dir
        self.python_exe, self.runner = python_exe, runner
        self.work = Path(tempfile.mkdtemp(prefix="ge200_nam_"))
        self._outputs: Dict[str, Path] = {}

    @property
    def available(self) -> bool:
        return bool(self.python_exe) and os.path.exists(self.python_exe)

    def listing(self) -> dict:
        models = []
        for m in (scan_library(self.nam_dir) if self.nam_dir and os.path.isdir(self.nam_dir) else []):
            rel = os.path.relpath(m["path"], self.nam_dir).replace("\\", "/")
            models.append({k: v for k, v in m.items() if k != "path"} | {"id": rel})
        return {
            "available": self.available,
            "reason": "" if self.available else "the NAM environment (.venv-nam) was not found",
            "models": models,
            "irs": _list_audio(self.ir_dir),
            "dis": [BUILTIN_DI] + _list_audio(self.di_dir),
            "folders": {"nam": self.nam_dir, "ir": self.ir_dir, "di": self.di_dir},
        }

    def render(self, model_id: str, ir_id: str, di_id: str, gain_db: float) -> dict:
        if not self.available:
            raise ValueError("the NAM environment (.venv-nam) is not installed")
        if not isinstance(gain_db, (int, float)) or not -40 <= gain_db <= 40:
            raise ValueError("gain must be between -40 and 40 dB")
        lst = self.listing()
        if model_id not in {m["id"] for m in lst["models"]}:
            raise ValueError("unknown model")
        if ir_id and ir_id not in lst["irs"]:
            raise ValueError("unknown IR")
        if di_id not in lst["dis"]:
            raise ValueError("unknown DI")
        rid = secrets.token_hex(8)
        out = self.work / f"{rid}.wav"
        if di_id == BUILTIN_DI:
            di_path = str(self.work / "builtin_chord.wav")
            if not os.path.exists(di_path):
                make_test_chord(di_path)
        else:
            di_path = os.path.join(self.di_dir, di_id)
        args = ["render", os.path.join(self.nam_dir, model_id), di_path, str(out), "--gain-db", str(gain_db)]
        if ir_id:
            args += ["--ir", os.path.join(self.ir_dir, ir_id)]
        stats = self.runner(self.python_exe, args)
        self._outputs[rid] = out
        return {"id": rid, "stats": stats}

    def audio(self, rid: str) -> bytes:
        path = self._outputs.get(rid)
        if path is None or not path.exists():
            raise KeyError("no such render")
        return path.read_bytes()
