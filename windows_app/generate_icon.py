r"""One-time generator for windows_app/copyclip.ico: zero dependencies (pure struct, no
PIL), same clipboard board/tab/line motif as android_app/.../ic_launcher_foreground.xml, so
the tray icon and the Android app icon are visibly the same app. Run once:

    python windows_app\generate_icon.py

Not imported by tray.py at runtime; this is a build-time asset generator, the .ico file it
produces is what actually ships.

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


def _supersampled_pixel(x: int, y: int) -> tuple[int, int, int, int]:
    step = 1.0 / SUPERSAMPLE
    totals = [0.0, 0.0, 0.0, 0.0]
    for sy in range(SUPERSAMPLE):
        fy = y + (sy + 0.5) * step
        for sx in range(SUPERSAMPLE):
            fx = x + (sx + 0.5) * step
            for i, channel in enumerate(pixel_at(fx, fy)):
                totals[i] += channel
    n = SUPERSAMPLE * SUPERSAMPLE
    return tuple(round(total / n) for total in totals)


def build_ico(path: str) -> None:
    color_data = bytearray()
    for y in range(SIZE - 1, -1, -1):  # BMP rows are bottom-to-top
        for x in range(SIZE):
            b, g, r, a = _supersampled_pixel(x, y)
            color_data += bytes([b, g, r, a])

    and_row_bytes = ((SIZE + 31) // 32) * 4
    and_mask = bytes(and_row_bytes * SIZE)  # alpha channel handles transparency; mask unused

    bmp_header = struct.pack(
        "<IiiHHIIiiII",
        40, SIZE, SIZE * 2, 1, 32, 0,
        len(color_data) + len(and_mask), 0, 0, 0, 0,
    )
    image_data = bmp_header + bytes(color_data) + and_mask

    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII",
        SIZE, SIZE, 0, 0, 1, 32, len(image_data), 6 + 16,
    )
    with open(path, "wb") as f:
        f.write(icondir + entry + image_data)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copyclip.ico")
    build_ico(out)
    print(f"wrote {out}")
