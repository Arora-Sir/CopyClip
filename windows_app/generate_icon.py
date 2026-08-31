r"""One-time generator for windows_app/copyclip.ico -- zero dependencies (pure struct, no
PIL), same clipboard board/tab/line motif as android_app/.../ic_launcher_foreground.xml, so
the tray icon and the Android app icon are visibly the same app. Run once:

    python windows_app\generate_icon.py

Not imported by tray.py at runtime -- this is a build-time asset generator, the .ico file it
produces is what actually ships.
"""
from __future__ import annotations

import os
import struct

SIZE = 32

TRANSPARENT = (0, 0, 0, 0)
BOARD = (0xEC, 0xE6, 0xE4, 0xFF)   # BGRA for #E4E6EC
TAB = (0xFF, 0xA2, 0x7A, 0xFF)     # BGRA for #7AA2FF
LINE = (0xC7, 0xD8, 0x6F, 0xFF)    # BGRA for #6FD8C7


def pixel_at(x: int, y: int) -> tuple[int, int, int, int]:
    if 13 <= x <= 18 and 5 <= y <= 8:
        return TAB
    if 9 <= x <= 22 and 8 <= y <= 25:
        if 12 <= x <= 19 and 13 <= y <= 15:
            return LINE
        return BOARD
    return TRANSPARENT


def build_ico(path: str) -> None:
    color_data = bytearray()
    for y in range(SIZE - 1, -1, -1):  # BMP rows are bottom-to-top
        for x in range(SIZE):
            b, g, r, a = pixel_at(x, y)
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
