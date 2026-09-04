# BUG-011: Pixel 1 Screenshot Sync Baseline Blindspot and SendClip Fallback

## Symptom
Taking a screenshot on Pixel 1 did not appear on laptop or phone/tab clipboard. Tapping 'Send Clip' in the CopyClip Android app on Pixel 1 also failed to propagate the screenshot, pushing stale text instead.

## Measured Evidence
1. **Timestamped On-Device File Discovery**:
   - /sdcard/Pictures/Screenshots/Screenshot_20260903-183231.png was created at 18:32:31 (1,248,756 bytes).
2. **Channel Reconnect Storm in Daemon Logs**:
   - 2026-09-03 18:32:09 WARNING copyclip.node.android: [pixel] no final ACK/DEFER for image SET within 12.0s; treating channel as dead
   - 2026-09-03 18:32:09 WARNING copyclip.node.android: [pixel] channel lost; reconnecting in 1s
   - 2026-09-03 18:32:11 INFO copyclip.adb: ClipBridge up on 100.64.0.13:5555 (READY 29)
   - 2026-09-03 18:32:37 WARNING copyclip.node.android: [pixel] no final ACK/DEFER for image SET within 12.0s; treating channel as dead
   - 2026-09-03 18:32:39 INFO copyclip.adb: ClipBridge up on 100.64.0.13:5555 (READY 29)
3. **Manual Push Discrepancy**:
   - 2026-09-03 18:33:13 INFO copyclip.push: [pixel] manual push (253589 chars)
   - Tapping 'Send Clip' sent 253,589 chars of old clipboard text because Stock Android (Pixel 1) does not place screenshots on the system clipboard automatically.

## Root Cause
1. **initScreenshotWatch() Startup Baseline Suppression**:
   When ClipBridge spawned on Pixel 1 at 18:32:39, it unconditionally adopted the newest screenshot on disk (Screenshot_20260903-183231.png, created 8 seconds earlier at 18:32:31) as its silent starting baseline. checkScreenshots() therefore treated it as already synced.
2. **Aggressive 12.0s Chunk ACK Deadline Over Tailscale**:
   Pixel 1 (Snapdragon 821, Wi-Fi AC) streaming 1.5MB+ images over Tailscale required ~14-16s. The 12.0s timeout triggered continuous reconnect loops.
3. **SendClipActivity Was Strictly Clipboard-Bound**:
   SendClipActivity only called clipboard.primaryClip. Because Android saves screenshots to disk without setting primaryClip, manual pushes ignored screenshots.
4. **MIME Sniffing Blindspot**:
   mimeOf(ClipDescription desc) only inspected slots for text, returning slot 0 (pplication/octet-stream), which caused ClipBridge to discard image content URIs as non-text.

## Fix Applied
1. **ndroid/ClipBridge.java**:
   - In initScreenshotWatch(): Only screenshots older than 90 seconds (geMs >= 90_000L) are adopted as baseline. Fresh screenshots (< 90s) leave lastScreenshotName = null, emitting immediately on the first poll tick.
   - In mimeOf(): Iterates through all declared MIME types to prioritize image/* before defaulting to slot 0.
2. **
odes/android_node.py**:
   - Increased chunk_timeout and inal_timeout to max(self.ack_timeout_s, 30.0) to allow smooth transfers for older Wi-Fi hardware.
3. **ndroid_app/SendClipActivity.kt**:
   - Implemented indRecentScreenshot(): Prioritizes screenshots taken within the last 90 seconds via MediaStore and filesystem fallback.
   - Redesigned HUD UI with design system tokens: dark slate surface card (#181A1F), 20dp corners, high-contrast typography, and explicit feedback ('Sending latest screenshot...' vs 'Sending clipboard...').
   - Added READ_EXTERNAL_STORAGE and equestLegacyExternalStorage=" true\ in AndroidManifest.xml.
 - Set ndroid:exported=\true\ on SendClipActivity.
4. **Binary and Asset Updates**:
 - Recompiled in/clipbridge.dex (30.5 KB) and updated ndroid_app/app/src/main/assets/clipbridge.dex.
 - Rebuilt APK (gradlew.bat assembleDebug) and deployed to Pixel 1.

## Verification
- **Automatic Screenshot Sync**: Screencap captured on Pixel 1 synced to laptop in 4,140 ms, backed up to Temporary Screenshots\Screenshot 2026-09-03 190012.png, and routed to all active devices.
- **Manual Send Clip Fallback**: Invoked SendClipActivity on Pixel 1; logcat confirmed clip=screenshot (1418 KB) and desktop log confirmed [pixel] manual push (1451406 chars) received in under 10 seconds.
- **Unit Tests**: All 11 imaging unit tests and all 12 Android node unit tests passed clean.

## Status
**CONFIRMED, fixed and verified live on hardware (2026-09-03)**
