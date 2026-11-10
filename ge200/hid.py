"""Picks the HID transport for the current platform: hidraw on Linux, the Win32 API on Windows."""
import sys

if sys.platform == "win32":
    from .hid_win import HidDevice, find_device_paths  # noqa: F401
else:
    from .hid_linux import HidDevice, find_device_paths  # noqa: F401
