"""Custom impulse responses for the GE200's 10 user IR slots (see PROTOCOL.md, "IR slots").

Format deduced from the vendor editor's import path (static analysis; the device-side behaviour of an upload is
verified separately, see PROTOCOL.md):

* the device runs at 44.1 kHz and takes **512 samples**, 24-bit signed;
* each sample is the vendor's conversion ``trunc(clamp(x, +-0.99999899) * 2**23)`` as 24-bit two's complement;
* an upload is four ``CMD 0xE1`` frames ``[slot][phase][data]``: phase 1 = the 512 high bytes, phase 2 = the 512 middle
  bytes, phase 3 = the 512 low bytes, phase 4 = the 15-byte name (upper-case ASCII, NUL padded);
* clearing a slot is ``CMD 0xE4 [slot]`` (and the editor shows the name EMPTY).

Needs the optional audio extras (numpy, scipy, soundfile): ``pip install .[audio]``.
"""
from __future__ import annotations

import os
from typing import List, Tuple

IR_SLOTS = 10
IR_SAMPLES = 512
DEVICE_RATE = 44100
NAME_LENGTH = 15
CMD_IR_WRITE = 0xE1
CMD_IR_CLEAR = 0xE4
_LIMIT = 0.9999989867210388       # the vendor clamps to +-this value before scaling


def to_int24(samples) -> List[int]:
    """Vendor conversion: clamp, scale by 2**23, truncate toward zero; returned as unsigned 24-bit patterns."""
    out = []
    for x in samples:
        x = max(-_LIMIT, min(_LIMIT, float(x)))
        out.append(int(x * 8388608.0) & 0xFFFFFF)
    return out


def planes(values: List[int]) -> Tuple[bytes, bytes, bytes]:
    """Split 24-bit samples into the (high, middle, low) byte planes the device receives."""
    return (bytes((v >> 16) & 0xFF for v in values),
            bytes((v >> 8) & 0xFF for v in values),
            bytes(v & 0xFF for v in values))


def clean_name(name: str) -> bytes:
    """File name -> device name: no extension, upper-case printable ASCII, 15 bytes NUL padded."""
    base = os.path.splitext(os.path.basename(name))[0].upper()
    base = "".join(c for c in base if 32 <= ord(c) <= 126)[:NAME_LENGTH]
    return base.encode("ascii").ljust(NAME_LENGTH, b"\x00")


def write_payloads(slot: int, samples, name: str) -> List[bytes]:
    """The four ``CMD_IR_WRITE`` payloads for one upload (``samples``: exactly 512 floats in -1..1)."""
    if not 0 <= slot < IR_SLOTS:
        raise ValueError(f"IR slot must be 0..{IR_SLOTS - 1}")
    if len(samples) != IR_SAMPLES:
        raise ValueError(f"an IR must have exactly {IR_SAMPLES} samples")
    hi, mid, lo = planes(to_int24(samples))
    return [bytes([slot, 1]) + hi, bytes([slot, 2]) + mid, bytes([slot, 3]) + lo,
            bytes([slot, 4]) + clean_name(name)]


def clear_payload(slot: int) -> bytes:
    if not 0 <= slot < IR_SLOTS:
        raise ValueError(f"IR slot must be 0..{IR_SLOTS - 1}")
    return bytes([slot])


# ---------------------------------------------------------------- audio files (optional dependencies)
def load_audio(path: str):
    """Read a WAV/FLAC/AIFF file as mono float32; returns (samples, sample_rate)."""
    import numpy as np
    import soundfile as sf
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    return np.mean(data, axis=1), rate          # channels averaged to mono


def load_audio_bytes(data: bytes):
    """Like ``load_audio`` for a file already in memory (an upload from the browser)."""
    import io as _io
    import numpy as np
    import soundfile as sf
    audio, rate = sf.read(_io.BytesIO(data), dtype="float32", always_2d=True)
    return np.mean(audio, axis=1), rate


def prepare(samples, rate: int, *, trim_silence: bool = True, preroll: int = 8, fade: int = 48,
            normalize_peak: float = 0.0):
    """Turn an arbitrary IR recording into the 512-sample, 44.1 kHz array the device takes.

    * resample to 44.1 kHz when needed;
    * ``trim_silence``: drop leading silence (keeps ``preroll`` samples before the first sample above -60 dB of the peak);
    * the first 512 samples are kept, the last ``fade`` samples get a half-Hann fade-out (no click at the cut);
    * ``normalize_peak`` > 0 scales the peak to that value (0 = leave the level alone, but never exceed +-0.99).
    """
    import numpy as np
    from math import gcd
    from scipy.signal import resample_poly

    x = np.asarray(samples, dtype=np.float64)
    if rate != DEVICE_RATE:
        g = gcd(int(rate), DEVICE_RATE)
        x = resample_poly(x, DEVICE_RATE // g, int(rate) // g)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak == 0.0:
        raise ValueError("the impulse response is silent")
    if trim_silence:
        first = int(np.argmax(np.abs(x) >= peak * 10 ** (-60 / 20)))
        x = x[max(0, first - preroll):]
    x = x[:IR_SAMPLES]
    if x.size < IR_SAMPLES:
        x = np.pad(x, (0, IR_SAMPLES - x.size))
    if fade:
        x[-fade:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade)))
    peak = float(np.max(np.abs(x)))
    if normalize_peak > 0:
        x = x * (min(normalize_peak, 0.99) / peak)
    elif peak > 0.99:
        x = x * (0.99 / peak)
    return x.astype(np.float32)
