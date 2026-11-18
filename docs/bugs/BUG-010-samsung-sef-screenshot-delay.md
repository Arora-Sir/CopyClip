# BUG-010: Samsung SEF metadata trailer causes 60-90s screenshot copy delay on S24 Ultra

**Status**: CONFIRMED, fixed in real use (2026-09-03)
**Severity**: High (severe latency regression, 60-90s sync delay)
**Affects**: Samsung Galaxy S24 Ultra, Samsung Galaxy Tab S10 Ultra (One UI 6.x / Android 14+)

---

## 1. Symptom

Taking a screenshot on Samsung Galaxy S24 Ultra took 60 to 90+ seconds to sync to the Windows laptop and appear in `C:\Users\you\Pictures\Screenshots\Temporary Screenshots\`. Previously, screenshots synced near-instantly (< 1-2 seconds) after capture.

---

## 2. Measured Evidence

### Binary Inspection of Real Hardware Files
Inspecting the exact binary bytes of `Screenshot 2026-09-03 173137.jpg` (661,842 bytes) captured on S24 Ultra:
- Standard JPEG End of Image (EOI) marker `0xFF 0xD9` was present at byte offset **661,645** (197 bytes before EOF).
- The trailing 197 bytes contained Samsung Extended Format (SEF) metadata:
  ```
  \xff\xd9\x00\x00Q\x0c\x14\x00\x00\x00Samsung_Capture_InfoScreenshot...Captured_App_Info...SEFT
  ```
- The file terminated with ASCII `SEFT` (`0x53 0x45 0x46 0x54`), NOT `0xFF 0xD9`.
- The exact same trailer was confirmed on Galaxy Tab S10 Ultra screenshots.

### Live Daemon Logs
Live logs from `logs/copyclip.log` showed:
```
2026-09-03 17:30:42 INFO copyclip.adb: ClipBridge up on 100.64.0.11:5555
...
2026-09-03 17:31:37 [Screenshot captured on phone]
... 60-90s elapsed with repeated connection stalls ...
2026-09-03 17:33:17 INFO copyclip.node.windows: [laptop] screenshot backed up to Temporary Screenshots
```

---

## 3. Root Cause

### A. Strict JPEG End-of-File Check in `ClipBridge.java`
On 2026-09-01, an `isCompleteImage()` guard was added to prevent truncated PNG screenshot reads on older devices (Pixel 1).
For JPEGs, it required:
```java
if ((data[0] & 0xFF) == 0xFF && (data[1] & 0xFF) == 0xD8) {
    return len >= 2 && (data[len - 2] & 0xFF) == 0xFF && (data[len - 1] & 0xFF) == 0xD9;
}
```
Because Samsung One UI Smart Capture appends ~197 bytes of SEF metadata trailing after `FF D9`, `isCompleteImage()` evaluated to `false` for **100% of screenshots on Samsung devices**.

### B. Infinite 8-Second Blocking Freeze on Main Thread
Inside `readCompleteScreenshotFile(newest)`:
1. `isCompleteImage()` returned `false`.
2. It looped with `Thread.sleep(150)` for **8 full seconds**, freezing `ClipBridge`'s main polling thread in `serve()`.
3. After 8s, it timed out and returned `null`.
4. In `checkScreenshots()`, because `raw == null`, `lastScreenshotName` was **never updated**.
5. On the very next tick, `newest.getName().equals(lastScreenshotName)` was still false, immediately re-triggering another 8-second freeze.
6. The thread starved indefinitely, delaying clipboard polling and image descriptor holds by 60 to 90+ seconds until a network reconnect reset the loop.

### C. False Chunk ACK Timeouts Over Tailscale
During large image transfers, `ACK_WINDOW = 8` waited for an ACK every 8 chunks (~524KB). Over Tailscale Wi-Fi, transmitting 524KB over an ADB shell pipe occasionally took longer than `ack_timeout_s` (4.0s), causing `nodes/android_node.py` to kill the transport and enter reconnect backoff loops.

---

## 4. Fix Applied

1. **`android/ClipBridge.java`**:
   - Updated `isCompleteImage()`:
     - Recognized standard JPEGs ending in `FF D9`.
     - Recognized Samsung JPEGs ending with ASCII `SEFT` (`data[len-4]=='S' && data[len-3]=='E' && data[len-2]=='F' && data[len-1]=='T'`).
     - Added backward scan (up to 64KB) for `FF D9` to support any JPEG with trailing metadata or padding.
     - Added backward scan (up to 4KB) for `IEND` in PNGs.
   - In `checkScreenshots()`: If `readCompleteScreenshotFile()` times out after 8s, advance `lastScreenshotName` and emit a `SKIP` line to prevent infinite thread-freezing retry loops.
   - In `findNewestScreenshot()`: Cached `newest.lastModified()` in a local variable to halve JNI/kernel stat syscalls across hundreds of files in `SCREENSHOT_DIRS`.
2. **`bin/clipbridge.dex`**:
   - Recompiled via `android/build_dex.ps1` (30.4 KB) using Android SDK 36 d8.
3. **`nodes/android_node.py`**:
   - Increased image chunk wait times to `chunk_timeout = max(self.ack_timeout_s, 12.0)` to eliminate false transport terminations during large image streaming over Tailscale Wi-Fi.

---

## 5. Verification

1. **Automated Unit Tests**:
   - Codec tests on real S24 Ultra JPEG (661,842 bytes): **PASS** (`isCompleteImage` returned `true` in <1ms).
   - Incomplete writes: **PASS** (returned `false`).
   - `python tests\test_imaging.py`: **11/11 PASS**.
   - `python tests\test_android_node.py`: **12/12 PASS**.
2. **Exhaustive Multi-Device Cartesian Benchmark (120 Live Runs)**:
   - Evaluated all 12 directed device pairs for Text (60 runs) and Images (60 runs).
   - **S24 Ultra Screenshot to Laptop**: Reduced from **60-90+ seconds** to **842 ms average** (728 ms - 998 ms), a **75x speedup**.
   - **Pass Rate**: 100% (120/120 runs succeeded with 0 timeouts).
