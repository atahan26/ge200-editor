"""IR import for the web editor: turn an audio file into the unit's 512-sample IR, preview it, upload it.

The audio can come from the browser (a base64 body, so any file on the computer works) or from the folder given with
``--ir-dir`` (by name, validated against that folder's listing, never a free path). Reading audio needs the optional
audio extras (numpy, scipy, soundfile).
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from typing import List, Optional, Tuple

from .. import ir
from .namlab import _list_audio

MAX_BYTES = 8 * 1024 * 1024          # decoded size of an uploaded audio file


class IrError(ValueError):
    """Bad input for the IR tools (message is safe to show)."""


def _suggest_name(label: str) -> str:
    stem = os.path.splitext(os.path.basename(label.replace("\\", "/")))[0]
    stem = re.sub(r"[^A-Za-z0-9 _.-]+", " ", stem)
    return re.sub(r"\s+", " ", stem).strip().upper()[: ir.NAME_LENGTH].strip()


class IrLab:
    def __init__(self, ir_dir: Optional[str] = None) -> None:
        self.ir_dir = ir_dir

    def files(self) -> List[str]:
        return _list_audio(self.ir_dir)

    @staticmethod
    def available() -> dict:
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError as e:
            return {"available": False, "reason": f"missing package ({e.name}): pip install numpy scipy soundfile"}
        return {"available": True, "reason": ""}

    def _read(self, file: Optional[str], data: Optional[str], filename: str) -> Tuple[bytes, str]:
        if data:
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise IrError("the file data is not valid base64") from None
            if len(raw) > MAX_BYTES:
                raise IrError(f"the file is too large (limit {MAX_BYTES // (1024 * 1024)} MB)")
            return raw, filename or "uploaded file"
        if file:
            if file not in self.files():
                raise IrError("that file is not in the IR folder")
            with open(os.path.join(self.ir_dir, file), "rb") as f:
                raw = f.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise IrError(f"the file is too large (limit {MAX_BYTES // (1024 * 1024)} MB)")
            return raw, file
        raise IrError("choose an IR file first")

    def convert(self, file: Optional[str], data: Optional[str], filename: str = "", normalize: bool = False):
        """Returns ``(samples, info)``: 512 floats ready for the unit, and what the preview shows."""
        st = self.available()
        if not st["available"]:
            raise IrError(st["reason"])
        import numpy as np
        raw, label = self._read(file, data, filename)
        try:
            x, rate = ir.load_audio_bytes(raw)
        except Exception:
            raise IrError("could not read that file as audio (WAV, FLAC or AIFF are supported)") from None
        try:
            y = ir.prepare(x, rate, normalize_peak=0.9 if normalize else 0.0)
        except ValueError as e:
            raise IrError(str(e)) from None
        info = {
            "label": label, "name": _suggest_name(label), "rate": int(rate), "seconds": round(len(x) / rate, 3),
            "peak_in": round(float(np.max(np.abs(x))), 4), "peak_out": round(float(np.max(np.abs(y))), 4),
            "kept_ms": round(1000 * ir.IR_SAMPLES / ir.DEVICE_RATE, 1),
            "cut": len(x) / rate * 1000 > 1000 * ir.IR_SAMPLES / ir.DEVICE_RATE + 1,
            "wave": [round(float(v), 4) for v in y],
        }
        return y, info
