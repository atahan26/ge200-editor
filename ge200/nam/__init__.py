"""NAM (Neural Amp Modeler) engine: a software amp/IR chain next to the GE200 hardware presets.

``info`` (inspecting and listing ``.nam`` files) needs nothing but the standard library. ``render`` (running a model
over audio) needs PyTorch and the ``neural-amp-modeler`` package, which live in a separate virtual environment
(``.venv-nam``) so the main environment stays light; the web editor calls it as a subprocess.
"""
from .info import inspect_nam, scan_library  # noqa: F401
