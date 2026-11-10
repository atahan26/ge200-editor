"""Frame and HID-report layer of the GE200 USB protocol (see PROTOCOL.md).

A *frame* is ``AA 55 | LEN_lo LEN_hi | CMD data... | CRC_hi CRC_lo``.
Frames travel over 64-byte HID reports of the form ``[chunk length][chunk data <= 63 bytes][padding]``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

SYNC = b"\xaa\x55"
REPORT_SIZE = 64
CHUNK_SIZE = REPORT_SIZE - 1

CMD_HELLO = 0xE0
CMD_PRESET = 0xA5
CMD_ACTIVE_PRESET = 0xA6
CMD_CURRENT_PRESET = 0x86
CMD_VERSION = 0x84


def crc16(data: bytes) -> int:
    """CRC-16: poly 0x1021, init 0, MSB first, final XOR 0xFFFF."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def build_frame(cmd: int, data: bytes = b"") -> bytes:
    payload = bytes([cmd]) + bytes(data)
    body = len(payload).to_bytes(2, "little") + payload
    return SYNC + body + crc16(body).to_bytes(2, "big")


def frame_to_reports(frame: bytes) -> List[bytes]:
    """Split a frame into 64-byte HID reports (without the Windows report-ID byte)."""
    reports = []
    for i in range(0, len(frame), CHUNK_SIZE):
        chunk = frame[i:i + CHUNK_SIZE]
        reports.append((bytes([len(chunk)]) + chunk).ljust(REPORT_SIZE, b"\x00"))
    return reports


@dataclass(frozen=True)
class Frame:
    cmd: int
    data: bytes
    crc_ok: bool


class StreamParser:
    """Reassembles frames from a sequence of 64-byte input reports."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_reports = 0

    def feed_report(self, report: bytes) -> List[Frame]:
        n = report[0] if report else 0
        if n == 0 or n > CHUNK_SIZE or len(report) < 1 + n:
            self.bad_reports += 1
            return []
        self._buf += report[1:1 + n]
        return self._drain()

    def _drain(self) -> List[Frame]:
        out: List[Frame] = []
        buf = self._buf
        while True:
            i = buf.find(SYNC)
            if i < 0:
                del buf[:max(0, len(buf) - 1)]  # keep a possible trailing 0xAA
                break
            if i:
                del buf[:i]
            if len(buf) < 6:
                break
            length = buf[2] | (buf[3] << 8)
            total = 4 + length + 2
            if length == 0:
                del buf[:2]
                continue
            if len(buf) < total:
                break
            body = bytes(buf[2:4 + length])
            crc = int.from_bytes(buf[4 + length:total], "big")
            out.append(Frame(cmd=buf[4], data=bytes(buf[5:4 + length]), crc_ok=crc16(body) == crc))
            del buf[:total]
        return out


def frames_from_reports(reports: Iterable[bytes]) -> List[Frame]:
    parser = StreamParser()
    frames: List[Frame] = []
    for r in reports:
        frames.extend(parser.feed_report(r))
    return frames
