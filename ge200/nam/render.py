"""Run a NAM capture (and an optional cabinet IR) over a DI recording. Needs PyTorch + neural-amp-modeler.

Offline rendering: load the ``.nam`` model, resample the DI to the model's rate, run the network, convolve with the
IR, normalise the peak and write a 24-bit WAV at the DI's original sample rate.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .info import DEFAULT_SAMPLE_RATE


def load_model(path: str):
    """Return ``(model, sample_rate)`` for a ``.nam`` file."""
    from nam.models import init_from_nam
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    model = init_from_nam(cfg)
    model.eval()
    sr = int(cfg.get("sample_rate") or getattr(model, "sample_rate", None) or DEFAULT_SAMPLE_RATE)
    return model, sr


def run_model(model, x):
    """Run a mono float32 numpy signal through the network; returns a numpy array of the same length."""
    import torch
    with torch.no_grad():
        y = model(torch.from_numpy(x).float()[None, :], pad_start=True)
    return y[0].cpu().numpy()


def _mono(data):
    return data.mean(axis=1)


def apply_ir(y, ir, sr: int, ir_sr: int, max_ms: float = 500.0):
    """Convolve ``y`` with the impulse response ``ir`` (resampled to ``sr``, trimmed to ``max_ms``)."""
    from math import gcd
    from scipy.signal import fftconvolve, resample_poly
    if ir_sr != sr:
        g = gcd(int(ir_sr), int(sr))
        ir = resample_poly(ir, sr // g, ir_sr // g)
    ir = ir[: int(sr * max_ms / 1000)]
    return fftconvolve(y, ir)[: len(y)]


def render(model_path: str, input_wav: str, out_wav: str, ir_path: Optional[str] = None,
           input_gain_db: float = 0.0, peak_db: float = -3.0, max_seconds: float = 120.0) -> dict:
    """Render ``input_wav`` through the model (+ IR) into ``out_wav``. Returns timing / level statistics."""
    import numpy as np
    import soundfile as sf
    from math import gcd
    from scipy.signal import resample_poly

    t0 = time.time()
    model, sr = load_model(model_path)
    data, in_sr = sf.read(input_wav, always_2d=True, dtype="float32")
    x = _mono(data)[: int(in_sr * max_seconds)]
    if in_sr != sr:
        g = gcd(int(in_sr), sr)
        x = resample_poly(x, sr // g, int(in_sr) // g).astype(np.float32)
    x = x * float(10 ** (input_gain_db / 20))
    t1 = time.time()
    y = run_model(model, x)
    t2 = time.time()
    if ir_path:
        ir_data, ir_sr = sf.read(ir_path, always_2d=True, dtype="float32")
        y = apply_ir(y, _mono(ir_data), sr, int(ir_sr))
    if in_sr != sr:
        g = gcd(int(in_sr), sr)
        y = resample_poly(y, int(in_sr) // g, sr // g)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0 and peak_db is not None:
        y = y * (10 ** (peak_db / 20) / peak)
    sf.write(out_wav, y.astype(np.float32), int(in_sr), subtype="PCM_24")
    return {
        "model_sample_rate": sr, "input_sample_rate": int(in_sr), "seconds": round(len(x) / sr, 2),
        "inference_s": round(t2 - t1, 3), "realtime_factor": round((t2 - t1) / max(len(x) / sr, 1e-9), 3),
        "total_s": round(time.time() - t0, 3), "peak_before_normalise": round(peak, 4),
    }


def benchmark(model_path: str, seconds: float = 5.0) -> dict:
    """How fast is this model on this machine? realtime_factor < 1 means faster than real time (needed for live use)."""
    import numpy as np
    model, sr = load_model(model_path)
    x = (np.random.default_rng(0).standard_normal(int(sr * seconds)) * 0.1).astype("float32")
    run_model(model, x[: sr // 2])                       # warm-up
    t = time.time()
    run_model(model, x)
    dt = time.time() - t
    return {"sample_rate": sr, "seconds": seconds, "inference_s": round(dt, 3), "realtime_factor": round(dt / seconds, 3)}


def build_test_model(outdir: str, basename: str = "tiny", channels: int = 4) -> str:
    """Write an *untrained* small WaveNet as a valid ``.nam`` file (for tests and for timing the pipeline)."""
    import pathlib
    from nam.models import factory
    n = max(3, channels)
    cfg = {
        "layers_configs": [{
            "input_size": 1, "condition_size": 1, "channels": n,
            "kernel_sizes": [3, 3, 3], "dilations": [1, 2, 4],
            "activation": "Tanh", "gated": False,
            "head": {"out_channels": 1, "kernel_size": 1, "bias": True},
        }],
        "head_scale": 1.0,
    }
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    net = factory.init("WaveNet", args=[cfg])
    net.export(out, basename=basename)
    return str(out / f"{basename}.nam")
