# BUG-012: Screenshot directory traversal storm, keyguard poll wakeups, and companion app flash wear

**Status**: CONFIRMED, fixed and verified live on hardware (2026-09-04)
**Severity**: High (battery drain, continuous CPU wakeups, flash wear)
**Affects**: `ClipBridge.java`, `SyncForegroundService.kt`, `transports/adb_transport.py`

---

## 1. Symptoms

1. **Abnormal Background Battery Drain**: Samsung Galaxy S24 Ultra (`SM-S928B`) and Tab S10 Ultra were exhibiting battery drain and elevated thermals while resting idle.
2. **Persistent Background CPU Activity**: `ClipBridge` was consuming continuous CPU cycles on Android even when neither clipboard activity nor screenshot capture was taking place.
3. **Repeated Flash Writes**: The companion Android app service was performing periodic file write operations every 4 seconds to internal flash memory.

---

## 2. Measured Evidence

### A. Directory Traversal Volume
Inspecting `/sdcard/DCIM/Screenshots` on the Samsung Galaxy S24 Ultra revealed **728 existing screenshot files**.
Because `findNewestScreenshot()` executed on every 250ms tick of `serve()`:
$$\text{Syscalls per second} = 728 \text{ files} \times 4 \text{ ticks/sec} = 2,912 \text{ stat() calls/sec}$$
Each tick allocated an array of 728 `java.io.File` objects on the Java heap, forcing the Dalvik/ART runtime into continuous garbage collection cycles.

### B. Locked Device Polling Overhead
`dumpsys trust` confirmed that when the phone was in the pocket or resting locked:
- Android 10+ strictly returned `null` for `getPrimaryClip()`.
- The bridge continued querying `getDescription()` and `ITrustManager.isDeviceLocked()` every 250ms via Binder IPC.
- Generated 14,400 Binder IPC wakeups per hour with zero possibility of actionable data.

### C. Companion App Flash Wear Loop
Inspection of `SyncForegroundService.kt` showed that every 4 seconds, the service handler inspected `/data/local/tmp/copyclip_conf.json`. Because it checked timestamp inequalities without checking whether the configuration content actually changed, it rewrote the file continuously:
$$\text{Redundant writes per day} = \frac{86,400 \text{ sec}}{4 \text{ sec}} = 21,600 \text{ flash writes/day}$$

---

## 3. Root Cause

1. **Missing Directory Inode Timestamp Guard**: `findNewestScreenshot()` unconditionally invoked `dir.listFiles()` regardless of whether any file had actually been created, deleted, or modified in the screenshot directories.
2. **Uniform Polling Frequency Under Keyguard Lock**: `serve()` used the exact same 250ms polling interval whether the phone was unlocked and in active use or locked in a pocket.
3. **Uncached Configuration Synchronization**: The companion foreground service lacked a file modification timestamp cache (`lastProcessedConfMtime`) and content equality check before updating `/data/local/tmp/copyclip_conf.json`.
4. **Unconditional DEX Push**: `AdbTransport._push_dex()` pushed the 31.5 KB DEX file on every reconnect without inspecting if the remote file on `/data/local/tmp/` was already byte-identical.

---

## 4. Fix Applied

### 1. `android/ClipBridge.java`: Inode `mtime` Short-Circuit
Added `lastDirMtimes` and `cachedNewestScreenshot`:
```java
private static final File[] SCREENSHOT_DIRS = new File[] {
    new File("/sdcard/DCIM/Screenshots"),
    new File("/sdcard/Pictures/Screenshots"),
};
private static final long[] lastDirMtimes = new long[SCREENSHOT_DIRS.length];
private static File cachedNewestScreenshot = null;

private static File findNewestScreenshot() {
    boolean anyDirChanged = false;
    for (int i = 0; i < SCREENSHOT_DIRS.length; i++) {
        File dir = SCREENSHOT_DIRS[i];
        long mtime = dir.lastModified();
        if (mtime != lastDirMtimes[i]) {
            anyDirChanged = true;
            lastDirMtimes[i] = mtime;
        }
    }
    if (!anyDirChanged && cachedNewestScreenshot != null) {
        return cachedNewestScreenshot;
    }
    // Only enumerate files if directory inode mtime actually changed
    File newest = null;
    long newestTime = -1L;
    for (File dir : SCREENSHOT_DIRS) {
        File[] files = dir.listFiles();
        if (files == null) continue;
        for (File f : files) {
            if (!f.isFile()) continue;
            long m = f.lastModified();
            if (newest == null || m > newestTime) {
                newest = f;
                newestTime = m;
            }
        }
    }
    cachedNewestScreenshot = newest;
    return newest;
}
```

### 2. `android/ClipBridge.java`: Adaptive Keyguard Backoff
Added `POLL_MS_LOCKED = 1500L`. If `isLocked()` is true, the polling loop sleeps for 1.5s instead of 250ms. When transitioning from locked to unlocked, `lastStamp` is reset to `-1L` and `flushPending()` is called immediately to guarantee zero-latency resync upon screen unlock.

### 3. `android_app/SyncForegroundService.kt`: Configuration Flash Write Guard
Cached `lastProcessedConfMtime` and compared target settings against existing on-disk JSON prior to writing.

### 4. `transports/adb_transport.py`: Smart Remote DEX Caching
Queried remote DEX size via `stat -c %s /data/local/tmp/clipbridge.dex` prior to pushing. If remote size matches local DEX (31,452 bytes), push is skipped.

---

## 5. Live Verification

1. **Inode Modification Behavior**:
   Empirically confirmed on S24 Ultra: creating or deleting a file in `/sdcard/DCIM/Screenshots` updates the directory inode `mtime` instantly (`1788476182` -> `1788480494`).
2. **Idle CPU Measurement**:
   Ran `top -b -n 1 -p 30868 -o %CPU` on S24 Ultra while idle:
   ```text
   %CPU
    0.0
   ```
   Idle CPU dropped to strictly **0.0%**.
3. **Autonomous Multi-Device Mesh Test Suite**:
   Executed [`tests/test_autonomous_mesh.py`](../tests/test_autonomous_mesh.py): 13/13 PASSED.
4. **Cartesian Speed Benchmark**:
   Executed [`tests/benchmark_mesh_speed.py`](../tests/benchmark_mesh_speed.py): All 12 directed device routes within SLA limits. Screenshot capture sync latency from S24 Ultra measured at **842 ms average** (P50: 835 ms).

---

## 6. Rollback Plan

If directory mtime detection fails on an unsupported custom Android kernel:
1. Revert `android/ClipBridge.java` lines 1168-1202 to iterate `SCREENSHOT_DIRS` directly.
2. Recompile DEX via `powershell -File android\build_dex.ps1`.
3. Restart daemon.
