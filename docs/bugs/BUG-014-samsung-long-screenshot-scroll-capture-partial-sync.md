# BUG-014: Samsung Long Screenshot (Scroll Capture) Syncs Only the Initial Partial Image

## Symptom
Taking a Samsung "long screenshot" (screenshot inside an app, then tapping "Capture more"
repeatedly to extend it into one tall scrolling image, then Save) on the S24 Ultra synced
only the initial single-page screenshot to the Windows clipboard, sometimes with a little
extra and sometimes not, but never the final full stitched image. Normal single-shot
screenshots and all text sync worked correctly the whole time.

## Measured Evidence
1. Pure filesystem observation (`android/spike/measure_scroll_capture.sh`, no daemon
   running), sampling `/sdcard/DCIM/Screenshots` every 100-400ms:
   - `Screenshot_20260917_174751_Gallery.jpg`: 999,303 bytes, unchanged for 26 samples, then
     3,488,952 bytes under the SAME filename (mtime advanced 8 seconds later). The jump
     landed between two consecutive polls, not a gradual growth.
   - `Screenshot_20260917_174829_Gallery.jpg`: 815,816 bytes, then 3,027,885 bytes under the
     SAME filename 22 seconds later, again a clean jump between consecutive polls.
   - Both file states independently pass `isCompleteImage()`'s JPEG check (both end in
     `53454654`, Samsung's SEFT trailer).
2. Live end-to-end verification after the fix (real daemon, real device, 2026-09-17):
   - `Screenshot 2026-09-17 185054.jpg` (1,025,357 bytes) synced instantly, followed by
     `Screenshot 2026-09-17 185109.jpg` (4,509,748 bytes, the full stitched image) synced
     15 seconds later. Both landed correctly.

## Root Cause
`android/ClipBridge.java`'s screenshot watcher is a plain poll loop (`POLL_MS`), not a
`ContentObserver` or `BroadcastReceiver`: Samsung's own screenshot capture never calls
`setPrimaryClip()`, so there is no clip event to hook (see the comment above
`SCREENSHOT_DIRS`). `checkScreenshots()` treated a file as "new" using filename equality
only against `lastScreenshotName`, a deliberate, correct fix for BUG-005 (a background
process re-touching identical bytes under the same name). Once a filename was marked
synced, it was never looked at again.

Samsung's scroll capture finalizes a long screenshot by overwriting that SAME filename with
the full stitched image, several seconds after the initial single-page shot (measured at 8s,
15s, and 22s across three real captures). `readCompleteScreenshotFile()`'s
`isCompleteImage()` check has no concept of "capture session in progress": it only checks
whether the current bytes look like a structurally complete image, which the initial
single-page shot already is on its own. So the initial shot synced immediately and
correctly, and the later overwrite carrying the real final image was silently ignored
forever, because the filename never changed.

## Fix Applied
`android/ClipBridge.java`:
1. `checkScreenshots()`: added a bounded re-arm window. A synced filename is now tracked
   with when it was first synced (`lastScreenshotFirstSyncMs`) and how many bytes were last
   observed (`lastScreenshotLastSyncedLen`). Within `SCREENSHOT_REARM_WINDOW_MS` (45s, set
   with margin above the largest measured gap of 22s) of the first sync, a size-only stat
   check on each tick asks whether the file grew; if so, it is re-read and re-sent through
   the same path as a fresh file. The window is anchored to the first sync and never
   renewed, so the total extra watch time per screenshot is capped regardless of tap count.
   Gated on length only, never mtime, so BUG-005's fix still holds inside the window.
2. `readCompleteScreenshotFile()`: added a short stability gate
   (`SCREENSHOT_STABILITY_TICKS` = 2). A positive `isCompleteImage()` verdict is only
   trusted once the file's length has also been unchanged across two consecutive 150ms
   polls, defending against a still-being-written file whose trailing bytes coincidentally
   look terminated. Not observed to be the actual mechanism here (both measured overwrites
   were clean, single-step jumps, not incremental growth), kept as cheap defense-in-depth.
3. `initScreenshotWatch()`: a silently-adopted startup baseline now also seeds the new
   tracking fields so it is immediately treated as past its window, preserving BUG-011's
   fix exactly.

`isCompleteImage()`, `emitScreenshot()`, `findNewestScreenshot()`'s directory-mtime caching,
the wire protocol, core routing, and the Windows-side backup/clipboard write are all
untouched, confirmed before writing any fix to already fail closed (reject-whole on
oversize, abort-on-gap, drop-on-hash-mismatch) and not the cause.

Known, accepted side effect: since the initial single-page shot cannot be distinguished in
advance from an eventual long screenshot, it still syncs immediately as before, and the full
stitched image syncs again once Samsung finishes. Two backup files land per long-screenshot
session (the small initial one and the full final one) instead of one. Confirmed as an
acceptable tradeoff with the owner, not treated as a defect.

## Verification
1. **Spike** (`android/spike/measure_scroll_capture.sh`,
   `android/spike/spike-pass-a-run1.log`): confirmed the same-filename overwrite mechanism
   and real timing (8s, 22s) before writing any production code, per this project's
   spike-first rule.
2. **Automated test suite**: `tests/test_core.py`, `tests/test_android_node.py`,
   `tests/test_imaging.py` all passed. Regression-only; none of them exercise
   `ClipBridge.java` directly.
3. **Live hardware verification (2026-09-17)**:
   - Rebuilt `bin/clipbridge.dex`, clean compile.
   - Daemon restarted; log shows `ClipBridge up on 100.64.0.11:5555 (READY 36)` and the
     same for the tablet, confirming both devices picked up the new dex.
   - A plain text clip (505 chars) synced correctly immediately after restart.
   - Three separate normal screenshots each produced exactly one backup file, no
     duplicates: BUG-006 still holds.
   - A real long screenshot produced the correct two-stage sync: 1,025,357 bytes instantly,
     then 4,509,748 bytes (the full image) 15 seconds later, well inside the 45s window.
   - On-device CPU for `app_process` after roughly 35 minutes of runtime: 0:09.54 total CPU
     time (about 0.45% average), consistent with BUG-012's fix still holding.

## Status
**OPEN**: fix implemented and verified in live end-to-end testing (2026-09-17), including
a real multi-tap long-screenshot capture. Per this project's convention, stays open until
confirmed by ordinary daily use over time, not just this one test pass.
