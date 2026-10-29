# BUG-017: DIBV5 Windows Paste Synthesis Latency in Modern Desktop Apps

## Status
CONFIRMED, fixed and verified live on hardware (2026-10-29).

## Severity
Medium-High (10-second latency before paste is available in target applications).

## Symptom
Screenshots synced from mobile devices to Windows arrived in the local `Temporary Screenshots` folder within 2 seconds, but taking `Ctrl+V` in desktop applications (Discord, Slack, WhatsApp, Chromium, Word) stalled for up to 10 seconds or failed to paste.

## Root Cause
1. `winclip.py` placed only `CF_DIBV5` (format 17) onto the Windows clipboard.
2. Most Windows GUI applications query standard `CF_DIB` (format 8).
3. Windows provides automatic clipboard format synthesis, attempting to convert `CF_DIBV5` to `CF_DIB` on the first paste request. Under Windows 11 with clipboard history metadata flags active, this lazy conversion blocked the calling thread and resulted in multi-second stalls.

## Fix
1. Implemented `png_to_dib(bgra, width, height)` in `winimage.py` to create a standard 40-byte `BITMAPINFOHEADER` with bottom-up 32bpp BGRA pixel data.
2. Updated `winclip.write_image()` to write both `CF_DIBV5` and `CF_DIB` in the same clipboard transaction:
```python
items = [
    (CF_DIBV5, dibv5_data),
    (CF_DIB, dib_data),
]
```
3. Writing both formats eliminates the on-demand synthesis step entirely.

## Verification
Verified with `tests/test_imaging.py` byte-level round trip and live hardware regression in `tests/test_live_features.py`. Latency from device screenshot capture to instant `Ctrl+V` paste availability dropped to under 1 second.
