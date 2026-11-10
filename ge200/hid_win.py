"""Minimal Windows HID transport (stdlib ctypes only).

Opens the GE200's HID interface like the vendor editor does (overlapped, shared read/write) and moves
64-byte reports. Windows wants a leading report-ID byte (0) on the I/O buffer even though the device
uses no report IDs; this module hides that.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from typing import List, Optional

VID, PID = 0x0483, 0x5703
REPORT_SIZE = 64
IO_SIZE = REPORT_SIZE + 1

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("ge200.hid_win only works on Windows")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_hid = ctypes.WinDLL("hid")
_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.ResetEvent.argtypes = [wt.HANDLE]
_k32.CancelIo.argtypes = [wt.HANDLE]
_setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                                       ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]

_INVALID = wt.HANDLE(-1).value
_GENERIC_RW, _SHARE_RW, _OPEN_EXISTING, _FLAG_OVERLAPPED = 0xC0000000, 3, 3, 0x40000000
_ERROR_IO_PENDING = 997


class _GUID(ctypes.Structure):
    _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]


class _SPDID(ctypes.Structure):  # SP_DEVICE_INTERFACE_DATA
    _fields_ = [("cbSize", wt.DWORD), ("guid", _GUID), ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


def find_device_paths(vid: int = VID, pid: int = PID) -> List[str]:
    """Return the HID device paths whose id contains the given VID/PID (read-only enumeration)."""
    guid = _GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(guid))
    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
    info = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if info == _INVALID:
        raise OSError(ctypes.get_last_error(), "SetupDiGetClassDevs failed")
    needle = f"vid_{vid:04x}&pid_{pid:04x}"
    paths: List[str] = []
    try:
        i = 0
        while True:
            did = _SPDID()
            did.cbSize = ctypes.sizeof(_SPDID)
            if not _setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), i, ctypes.byref(did)):
                break
            i += 1
            need = wt.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(did), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is 8 on 64-bit, 6 on 32-bit
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(did), buf, need.value, None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if needle in path.lower():
                    paths.append(path)
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


class HidDevice:
    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            candidates = find_device_paths()
            # the HID interface is the one with mi_03 in the path; fall back to the only candidate
            preferred = [p for p in candidates if "mi_03" in p.lower()] or candidates
            if not preferred:
                raise OSError("GE200 not found (VID 0483 PID 5703). Is it plugged in?")
            path = preferred[0]
        self.path = path
        self._h = _k32.CreateFileW(path, _GENERIC_RW, _SHARE_RW, None, _OPEN_EXISTING, _FLAG_OVERLAPPED, None)
        if self._h == _INVALID:
            raise OSError(ctypes.get_last_error(), f"cannot open {path}")
        self._wev = _k32.CreateEventW(None, True, False, None)
        self._rev = _k32.CreateEventW(None, True, False, None)
        self._rbuf = ctypes.create_string_buffer(IO_SIZE)
        self._rov = _OVERLAPPED()
        self._rov.hEvent = self._rev
        self._pending = False

    def close(self) -> None:
        if self._h:
            _k32.CancelIo(self._h)
            _k32.CloseHandle(self._h)
            _k32.CloseHandle(self._wev)
            _k32.CloseHandle(self._rev)
            self._h = None

    def __enter__(self) -> "HidDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def write_report(self, report: bytes) -> None:
        if len(report) != REPORT_SIZE:
            raise ValueError("report must be 64 bytes")
        buf = ctypes.create_string_buffer(b"\x00" + report, IO_SIZE)
        ov = _OVERLAPPED()
        ov.hEvent = self._wev
        _k32.ResetEvent(self._wev)
        n = wt.DWORD(0)
        ok = _k32.WriteFile(self._h, buf, IO_SIZE, ctypes.byref(n), ctypes.byref(ov))
        if not ok and ctypes.get_last_error() == _ERROR_IO_PENDING:
            _k32.WaitForSingleObject(self._wev, 2000)
            ok = _k32.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(n), False)
        if not ok:
            raise OSError(ctypes.get_last_error(), "HID write failed")

    def start_read(self) -> None:
        """Queue an input read (call once before sending a request so no report is missed)."""
        if self._pending:
            return
        _k32.ResetEvent(self._rev)
        n = wt.DWORD(0)
        ok = _k32.ReadFile(self._h, self._rbuf, IO_SIZE, ctypes.byref(n), ctypes.byref(self._rov))
        err = ctypes.get_last_error()
        if not ok and err != _ERROR_IO_PENDING:
            raise OSError(err, "HID read failed")
        self._pending = True

    def read_report(self, timeout_ms: int = 200) -> Optional[bytes]:
        """Return one 64-byte report, or None on timeout."""
        self.start_read()
        if _k32.WaitForSingleObject(self._rev, timeout_ms) != 0:
            return None
        n = wt.DWORD(0)
        if not _k32.GetOverlappedResult(self._h, ctypes.byref(self._rov), ctypes.byref(n), False):
            raise OSError(ctypes.get_last_error(), "HID read failed")
        self._pending = False
        return self._rbuf.raw[1:n.value] if n.value else b""


# argtypes for the overlapped calls (declared late because of the structure types)
_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL]
