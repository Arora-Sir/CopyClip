"""Image dimensions from the file header, stdlib only. No decoding, no pixel access."""

from __future__ import annotations

import struct

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) for PNG and JPEG, or None when the header is unknown or damaged."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":
            i = 2
            n = len(data)
            while i + 4 <= n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker in _SOF:
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h) if w and h else None
                if marker == 0xDA:
                    return None
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


def has_exif(data: bytes) -> bool:
    """True when the file carries capture metadata: a JPEG Exif block or a PNG eXIf chunk."""
    if data[:2] == bytes((0xFF, 0xD8)):
        return b"Exif" in data[:4096]
    if data[1:4] == b"PNG":
        return b"eXIf" in data[:4096]
    return False
