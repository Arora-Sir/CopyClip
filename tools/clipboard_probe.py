r"""Read-only diagnostic: dumps every format currently on the Windows clipboard, and what
winclip.read_text()/read_image() make of it.

Exists because "why didn't this image sync" needs a ground-truth answer, not a guess --
same discipline as tools/device_audit.py for the Android side. Changes nothing on the
clipboard; safe to run any time, as many times as needed.

    python tools\clipboard_probe.py
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip  # noqa: E402
import winimage  # noqa: E402

user32 = winclip.user32

# Well-known predefined format IDs (winuser.h). Anything not in this table is either a
# registered format (name resolved via GetClipboardFormatNameW) or, for IDs below
# CF_MAX with no name available, reported as "predefined(<n>)".
KNOWN_FORMATS = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK", 5: "CF_DIF",
    6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE", 10: "CF_PENDATA",
    11: "CF_RIFF", 12: "CF_WAVE", 13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE",
    15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
}
CF_MAX = 18  # anything >= this (and < 0xC000) with no name is a private/unregistered range


def format_name(fmt: int) -> str:
    if fmt in KNOWN_FORMATS:
        return KNOWN_FORMATS[fmt]
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClipboardFormatNameW(fmt, buf, 256)
    if n > 0:
        return f'"{buf.value}"'
    if fmt < CF_MAX:
        return f"predefined({fmt})"
    return f"unregistered({fmt})"


def dump_formats() -> None:
    print("=== raw formats on the clipboard (EnumClipboardFormats) ===")
    with winclip._Opened():
        fmt = 0
        count = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            count += 1
            print(f"  {fmt:>6}  {format_name(fmt)}")
        if count == 0:
            print("  (clipboard is empty, or holds only formats with no enumerable id)")


def dump_read_results() -> None:
    print()
    print("=== winclip.read_text() ===")
    try:
        text = winclip.read_text()
        if text is None:
            print("  None (no CF_UNICODETEXT)")
        else:
            preview = text if len(text) <= 80 else text[:80] + "..."
            print(f"  {len(text)} chars: {preview!r}")
    except Exception as exc:
        print(f"  RAISED {type(exc).__name__}: {exc}")

    print()
    print("=== winclip.read_image() ===")
    try:
        result = winclip.read_image()
        if result is None:
            print("  None (no recognised image format)")
        else:
            data, mime = result
            print(f"  SUCCESS: mime={mime}, {len(data)} bytes")
    except winimage.ImageDecodeError as exc:
        print(f"  RAISED ImageDecodeError: {exc}")
    except Exception as exc:
        import traceback
        print(f"  RAISED {type(exc).__name__}: {exc}")
        traceback.print_exc()


def main() -> int:
    print("Clipboard probe -- take whatever screenshot/copy action you want to diagnose,")
    print("then run this script immediately after. Read-only; changes nothing.\n")
    dump_formats()
    dump_read_results()
    return 0


if __name__ == "__main__":
    sys.exit(main())
