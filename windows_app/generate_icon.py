r"""One-time generator for windows_app/copyclip.ico: zero dependencies (pure struct, no
PIL), same clipboard board/tab/line motif as android_app/.../ic_launcher_foreground.xml, so
the tray icon and the Android app icon are visibly the same app. Run once:

    python windows_app\generate_icon.py

Not imported by tray.py at runtime; this is a build-time asset generator, the .ico file it
produces is what actually ships. The ico now holds 16 to 256 px so the tray, taskbar, Alt-Tab
and the app window are all sharp at any display scale; ui/icon.png is the 256 px version.

Board and tab carry a dark outline so the near-white board no longer washes out on a
light-theme tray. The content line is neutral grey, matching the Android icon's other two
lines (no clip-history feature exists, so it no longer wears the "synced" accent). That
accent lives on the status dot at the board's corner instead, a presence indicator, not a
sync-in-progress spinner, since the daemon is a continuous background mirror, not an
on-demand sync action.
"""
from __future__ import annotations

import os
import struct

SIZE = 32
SUPERSAMPLE = 8  # sub-samples per axis per output pixel, so edges and the dot are smooth

TRANSPARENT = (0, 0, 0, 0)
BOARD = (0xEC, 0xE6, 0xE4, 0xFF)     # BGRA for #E4E6EC
TAB = (0xFF, 0xA2, 0x7A, 0xFF)       # BGRA for #7AA2FF
OUTLINE = (0x58, 0x4E, 0x4A, 0xFF)   # BGRA for #4A4E58
LINE = (0xB2, 0xA1, 0x9A, 0xFF)      # BGRA for #9AA1B2
DOT = (0xC7, 0xD8, 0x6F, 0xFF)       # BGRA for #6FD8C7, the old LINE teal, moved here

DOT_CX, DOT_CY, DOT_R, RING_R = 24.0, 25.0, 5.0, 6.0


def pixel_at(x: float, y: float) -> tuple[int, int, int, int]:
    # dot uses true continuous distance (not floored) so supersampling renders it as a
    # smooth circle rather than a blocky diamond
    dist2 = (x - DOT_CX) ** 2 + (y - DOT_CY) ** 2
    if dist2 <= DOT_R * DOT_R:
        return DOT
    if dist2 <= RING_R * RING_R:
        return OUTLINE

    ix, iy = int(x), int(y)
    if 12 <= ix <= 20 and 1 <= iy <= 4:
        if ix in (12, 20) or iy in (1, 4):
            return OUTLINE
        return TAB
    if 6 <= ix <= 25 and 4 <= iy <= 27:
        if ix in (6, 25) or iy in (4, 27):
            return OUTLINE
        if 9 <= ix <= 22 and 11 <= iy <= 13:
            return LINE
        return BOARD
    return TRANSPARENT


def _supersampled_pixel(x: int, y: int, size: int = SIZE) -> tuple[int, int, int, int]:
    """Average colour of output pixel (x, y) when the 32-unit design is rendered at `size`."""
    scale = SIZE / size                      # design units per output pixel
    n_axis = SUPERSAMPLE if size <= 64 else 4
    step = 1.0 / n_axis
    totals = [0.0, 0.0, 0.0, 0.0]
    for sy in range(n_axis):
        fy = (y + (sy + 0.5) * step) * scale
        for sx in range(n_axis):
            fx = (x + (sx + 0.5) * step) * scale
            for i, channel in enumerate(pixel_at(fx, fy)):
                totals[i] += channel
    n = n_axis * n_axis
    return tuple(round(total / n) for total in totals)


def render(size: int) -> list[tuple[int, int, int, int]]:
    """Row-major BGRA pixels, top row first."""
    return [_supersampled_pixel(x, y, size) for y in range(size) for x in range(size)]


def _bmp_entry(size: int) -> bytes:
    px = render(size)
    color_data = bytearray()
    for y in range(size - 1, -1, -1):  # BMP rows are bottom-to-top
        for x in range(size):
            color_data += bytes(px[y * size + x])
    and_mask = bytes((((size + 31) // 32) * 4) * size)   # alpha carries transparency; mask unused
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(color_data) + len(and_mask), 0, 0, 0, 0)
    return header + bytes(color_data) + and_mask


def png_bytes(size: int) -> bytes:
    """Minimal RGBA PNG writer (stdlib only)."""
    import zlib
    px = render(size)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            b, g, r, a = px[y * size + x]
            raw += bytes([r, g, b, a])

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_ico(path: str, sizes: tuple[int, ...] = ICON_SIZES) -> None:
    """Multi-resolution .ico: small sizes as BMP, 64 and up as embedded PNG (Vista and later)."""
    images = [(s, png_bytes(s) if s >= 64 else _bmp_entry(s)) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    with open(path, "wb") as f:
        f.write(bytes(out))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    build_ico(os.path.join(here, "copyclip.ico"))
    with open(os.path.join(here, "ui", "icon.png"), "wb") as f:
        f.write(png_bytes(256))
    print("wrote copyclip.ico (%s) and ui/icon.png" % ", ".join(str(x) for x in ICON_SIZES))
