"""Fast unit tests for winimage.py: PNG encode/decode, DIB<->PNG conversion, encoder
determinism. Uses the real WIC (Windows Imaging Component) via ctypes -- these are
Windows-only and real (not mocked), but need no clipboard and no devices, so they run in
under a second.

    python tests\\test_imaging.py
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import winimage  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def _gradient_bgra(w: int, h: int) -> bytes:
    buf = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 4
            buf[o + 0] = (x * 255) // max(w - 1, 1)
            buf[o + 1] = (y * 255) // max(h - 1, 1)
            buf[o + 2] = 128
            buf[o + 3] = 255 if (x + y) % 2 == 0 else 128  # checkered alpha
    return bytes(buf)


def test_png_roundtrip_with_alpha():
    w, h = 37, 23  # odd, non-power-of-2 dims to catch stride bugs
    bgra = _gradient_bgra(w, h)
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=True)
    check("PNG signature correct", png[:8] == b"\x89PNG\r\n\x1a\n")
    decoded, dw, dh = winimage.decode(png)
    check("decoded dimensions match", (dw, dh) == (w, h))
    check("round trip byte-identical including alpha", decoded == bgra)


def test_png_roundtrip_without_alpha():
    w, h = 16, 16
    bgra = _gradient_bgra(w, h)
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=False)
    decoded, dw, dh = winimage.decode(png)
    expected = bytearray(bgra)
    expected[3::4] = bytes([255]) * (w * h)  # alpha forced opaque -- source had none
    check("RGB-only round trip forces opaque alpha on decode", decoded == bytes(expected))


def test_encoder_determinism():
    """Load-bearing for own-write echo suppression in nodes/windows_node.py: a re-encode
    of identical pixels must produce identical bytes, or a written-then-read-back image
    would never recognise itself and would loop.
    """
    w, h = 50, 50
    bgra = _gradient_bgra(w, h)
    png1 = winimage.png_from_bgra(bgra, w, h, has_alpha=True)
    png2 = winimage.png_from_bgra(bgra, w, h, has_alpha=True)
    check("same input encodes to identical bytes twice", png1 == png2)


def test_dibv5_roundtrip():
    w, h = 12, 9
    bgra = _gradient_bgra(w, h)
    dibv5 = winimage.png_to_dibv5(bgra, w, h)
    png = winimage.dib_to_png(dibv5)
    decoded, dw, dh = winimage.decode(png)
    check("DIBV5 round trip dimensions correct", (dw, dh) == (w, h))
    check("DIBV5 round trip byte-identical, alpha preserved (not the ClipCascade "
          "alpha-loss bug)", decoded == bgra)


def test_dib_24bpp_bottom_up_with_padding():
    """Hand-built BITMAPINFOHEADER DIB, bottom-up storage, 4-byte row padding -- exercises
    dib_to_png's header parsing directly rather than through our own encoder.
    """
    w, h = 5, 3
    row_stride = ((w * 3 + 3) // 4) * 4
    pixel_data = bytearray(row_stride * h)
    expected_bgra = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            b, g, r = (x * 10) % 256, (y * 20) % 256, 77
            src_row = h - 1 - y  # bottom-up storage
            off = src_row * row_stride + x * 3
            pixel_data[off:off + 3] = bytes((b, g, r))
            dst = (y * w + x) * 4
            expected_bgra[dst:dst + 4] = bytes((b, g, r, 255))
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0)
    dib = header + bytes(pixel_data)

    png = winimage.dib_to_png(dib)
    decoded, dw, dh = winimage.decode(png)
    check("24bpp BITMAPINFOHEADER dimensions correct", (dw, dh) == (w, h))
    check("24bpp DIB decodes correctly (opaque alpha added)",
          decoded == bytes(expected_bgra))


def test_malformed_input_raises_not_crashes():
    try:
        winimage.decode(b"not an image")
        check("garbage input raises ImageDecodeError", False)
    except winimage.ImageDecodeError:
        check("garbage input raises ImageDecodeError", True)

    try:
        winimage.dib_to_png(b"too short")
        check("truncated DIB raises ImageDecodeError", False)
    except winimage.ImageDecodeError:
        check("truncated DIB raises ImageDecodeError", True)


def test_real_jpeg_decode():
    """Decodes a real Windows system JPEG (not one of our own PNGs) through WIC, to prove
    the COM plumbing actually talks to a real third-party codec, not just our own encoder
    talking to itself.
    """
    candidates = [
        r"C:\Windows\Web\Screen\img100.jpg",
        r"C:\Windows\Web\4K\Wallpaper\Windows\img0_1920x1200.jpg",
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        print("  [SKIP] no system JPEG sample found on this machine")
        return
    with open(path, "rb") as f:
        jpg = f.read()
    bgra, w, h = winimage.decode(jpg)
    check("real JPEG decodes to expected buffer size", len(bgra) == w * h * 4)
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=False)
    bgra2, w2, h2 = winimage.decode(png)
    check("re-encoded PNG decodes back to identical pixels",
          (w2, h2) == (w, h) and bgra2 == bgra)


def test_decode_from_multiple_threads():
    """CoInitializeEx is per-thread: a module-global "already initialized" flag lets one
    thread's call satisfy the check for every other thread, leaving those threads' COM
    apartments genuinely uninitialized and CoCreateInstance failing with
    CO_E_NOTINITIALIZED. decode() runs on multiple long-lived threads in production (the
    clipboard watcher thread, each device's pump thread), so this must be exercised from
    fresh threads that have never called decode() before, not just the main thread.
    """
    w, h = 24, 18
    bgra = _gradient_bgra(w, h)
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=True)

    errors: list[BaseException] = []
    results: list[bool] = []

    def worker() -> None:
        try:
            decoded, dw, dh = winimage.decode(png)
            results.append((dw, dh) == (w, h) and decoded == bgra)
        except BaseException as exc:  # noqa: BLE001 - must not let a thread's raise vanish
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("decode from 8 fresh threads raises nothing",
          not errors, f"{len(errors)} error(s): {errors[:2]}")
    check("every thread decoded the identical, correct pixels",
          len(results) == 8 and all(results), f"{sum(results)}/8 correct")


def test_performance_1080p():
    """Not a hard pass/fail gate -- just a visible sanity signal that a realistic
    screenshot-sized image doesn't take an unreasonable amount of time.
    """
    w, h = 1920, 1080
    bgra = _gradient_bgra(w, h)
    t0 = time.time()
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=True)
    t1 = time.time()
    decoded, _, _ = winimage.decode(png)
    t2 = time.time()
    check(f"1920x1080 encode+decode completes in reasonable time "
          f"(encode={t1 - t0:.2f}s decode={t2 - t1:.2f}s)", (t2 - t0) < 5.0)
    check("1080p round trip byte-identical", decoded == bgra)


def main() -> int:
    for name, fn in [
        ("png-roundtrip-alpha", test_png_roundtrip_with_alpha),
        ("png-roundtrip-no-alpha", test_png_roundtrip_without_alpha),
        ("encoder-determinism", test_encoder_determinism),
        ("dibv5-roundtrip", test_dibv5_roundtrip),
        ("dib-24bpp-bottom-up", test_dib_24bpp_bottom_up_with_padding),
        ("malformed-input", test_malformed_input_raises_not_crashes),
        ("real-jpeg-decode", test_real_jpeg_decode),
        ("decode-from-multiple-threads", test_decode_from_multiple_threads),
        ("performance-1080p", test_performance_1080p),
    ]:
        print(f"{name}:")
        fn()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all imaging unit tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
