# BUG-023: One screenshot arrived twice with different bytes, and the daemon could exit without leaving any trace

## Status
FIXED IN CODE (2026-11-13), unit tested. **OPEN until a live screenshot on the S24 Ultra and the Tab S10 Ultra is confirmed.**

## Symptom
1. A screenshot taken on the tablet reached the laptop twice, one second apart, and was saved as two files.
2. The tray process was found restarted with no error in the log (11-11, 17:50 to 17:53).

## Measurement
* `logs/copyclip.log` has 82 pairs of image deliveries from the same device within 4 seconds. They fall in two groups: a preview thumbnail at about 4 percent of the full size (270x560 against 1080x2243), and twins at a size ratio of 0.999 to 1.000.
* Twin pair on the tablet at 09:59:54 and 09:59:55: 705,806 and 706,323 bytes, both 2960x1848. Decoded, they are pixel-for-pixel identical (mean difference 0, maximum 0). The second file has the same SHA-256 as the original on the tablet (`/sdcard/DCIM/Screenshots/..._Super Artemis.jpg`, pulled read-only after checking `ro.product.model` was SM-X920). The first file has no Exif block and different compressed data: it is the clipboard copy. The same twin pattern exists on the phone (11-11 12:21:44, 465,318 and 465,541 bytes).
* Windows Application log, 11-10 to 11-13: `CopyClip_Python.exe` crashed in `tcl86t.dll` (0x80000003) on 11-12 at 14:48 and 14:51, which is the old in-process Tk dashboard from BUG-018. `adb.exe` crashed inside `ucrtbase.dll` (0xc0000409) on 11-11 at 17:53. Other crashes in that window belong to Monica, Sunshine, Explorer plugins and Grammarly, not to CopyClip.

## Cause
1. The device saves one screenshot twice, once through the clipboard path and once as a file. The bytes differ, so hash dedup cannot match them.
2. Under pythonw there is no console, so a native crash or an unhandled thread exception left nothing in the log. The cause of the 11-11 exit is not proven.

## Fix
* `core/imagemeta.py` reads the pixel size and Exif presence from the file header, stdlib only.
* `Hub._is_resaved_image`: same device, same pixel size, byte size within 2 percent, within 4 seconds. The twin is skipped and recorded as skipped in Activity. When the twin has Exif and the first copy has none, `LaptopNode.replace_last_screenshot` swaps the saved file for the original (same pixels, with capture metadata).
* Images need 15 seconds before a repeat announcement counts as a re-copy (`IMAGE_RECOPY_GUARD_S`). Text keeps its 0.3 second rule.
* `core/logsetup.install_crash_diagnostics`: `faulthandler` writes thread stacks to `logs/crash.log`, unhandled exceptions in the main thread, worker threads and finalizers are logged with a traceback, and `state/running.marker` lets the next start report an abrupt end. A deliberate `--replace` does not count as abrupt.

## Not verified
* Live behaviour on both devices after the change.
* Whether a thumbnail can still slip through (BUG-006 family): four reached the laptop on 11-13 from the S24.
