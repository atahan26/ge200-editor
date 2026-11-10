"""Linux HID transport via ``/dev/hidraw*`` (stdlib only), so a small always-on computer such as a Raspberry Pi can
sit between the GE200 and your phone/laptop and serve the web editor over WiFi.

UNVERIFIED on real hardware: written from the hidraw API and tested with fake I/O only. The GE200's HID interface uses
no report IDs, so a report is exactly the 64 bytes (no leading 0 byte as on Windows).

Permissions: either run as root, or add a udev rule such as
``SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5703", MODE="0666"``.
"""
from __future__ import annotations

import os
import re
import select
from typing import List, Optional

VID, PID = 0x0483, 0x5703
REPORT_SIZE = 64


def find_device_paths(vid: int = VID, pid: int = PID, sysfs: str = "/sys/class/hidraw", dev: str = "/dev") -> List[str]:
    """``/dev/hidrawN`` nodes whose HID_ID matches the vendor/product id (read-only enumeration)."""
    out: List[str] = []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return out
    for name in names:
        try:
            with open(f"{sysfs}/{name}/device/uevent", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=([0-9A-Fa-f]+):([0-9A-Fa-f]+):([0-9A-Fa-f]+)", text)
        if m and int(m.group(2), 16) == vid and int(m.group(3), 16) == pid:
            out.append(f"{dev}/{name}")
    return out


class FdIO:
    """The few file-descriptor operations the transport needs (replaceable in tests)."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def readable(self, timeout_s: float) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout_s)
        return bool(r)

    def read(self, n: int) -> bytes:
        return os.read(self.fd, n)

    def write(self, data: bytes) -> int:
        return os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class HidDevice:
    """Same interface as :class:`ge200.hid_win.HidDevice`: ``write_report``, ``start_read``, ``read_report``, ``close``."""

    def __init__(self, path: Optional[str] = None, io=None) -> None:
        if io is None:
            if path is None:
                paths = find_device_paths()
                if not paths:
                    raise OSError("GE200 not found (VID 0483 PID 5703). Is it plugged in, and do you have access to /dev/hidraw*?")
                path = paths[0]
            io = FdIO(os.open(path, os.O_RDWR | os.O_NONBLOCK))
        self.path = path
        self._io = io

    def close(self) -> None:
        if self._io is not None:
            self._io.close()
            self._io = None

    def __enter__(self) -> "HidDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def write_report(self, report: bytes) -> None:
        if len(report) != REPORT_SIZE:
            raise ValueError("report must be 64 bytes")
        if self._io.write(bytes(report)) != REPORT_SIZE:
            raise OSError("short HID write")

    def start_read(self) -> None:
        """No-op: the kernel queues input reports for an open hidraw node."""

    def read_report(self, timeout_ms: int = 200) -> Optional[bytes]:
        """Return one 64-byte report, or None on timeout."""
        if not self._io.readable(timeout_ms / 1000.0):
            return None
        data = self._io.read(REPORT_SIZE)
        return data if data else None
