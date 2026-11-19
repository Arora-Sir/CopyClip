# CopyClip Future Architectural Study: Rust Rewrite and Internal Watchdog Architecture

## Status
* **Status**: RFC / Future Research Only. Scheduled for a later session, not started.
* **Target Milestone**: Post-V1 Stability. Do not implement until the current Python and Java Dex codebase is 100% complete, hardened, and verified in continuous daily production.
* **Date Documented**: 2026-11-15
* **Maintainer decisions (2026-11-19)**: a full rewrite is acceptable. It happens on a separate branch in parallel, so the Python version stays in production until the Rust version is fully tested, then everything moves over at once. Dependencies are allowed wherever they make the project better, which waives the zero-dependency rule for the Rust branch only. A standalone app window instead of a Chromium app window is a wanted outcome.

### Review notes (2026-11-18 and 2026-11-19), to settle before any code
These points in the study are wrong, unproven or smaller than stated. None is measured on this hardware yet.
1. A Windows Service runs in session 0 and cannot reach the interactive user's clipboard or tray. Pattern 3 only works for a headless service split from a user-session agent.
2. `RegisterApplicationRestart` only restarts an app that ran at least 60 seconds, and not after a clean exit or a hang kill.
3. Removing `adb.exe` means implementing ADB RSA authentication and, on Android 11 and later, the wireless pairing flow. The current design (plain `adb`, shell UID, no app) is the proven one.
4. Tokio, the `windows` crate and `tray-icon` are dependencies. That is now allowed on the Rust branch and must be written into `CLAUDE.md` when the branch starts.
5. The SHA-256 and Base64 gains are probably negligible, because Python's `hashlib` already uses OpenSSL and releases the GIL on large buffers.
6. The Rust column in the benchmark table is a projection. The Python column is real, but the RAM and startup figures should be re-measured on an idle PC, on Python 3.14 (the table says 3.11).
7. The UI is an HTML app served by the local control API. A single exe needs the assets embedded and a window host, which is where the standalone window decision applies.
8. The real bugs found so far were OS and device behaviour (locked devices, the clipboard 1418 error, Samsung echo, a browser process that lingers after its window closes, see BUG-025). A new language does not remove these, so the Win32 quirks must be reproduced in the Rust node.

### First steps when it starts
Re-measure the Python baseline on an idle PC, spike the clipboard node with the 1418 retry against the existing tests, choose the UI approach, then decide go or no-go. The Python unit and system tiers stay as the safety net, and `ClipBridge.java` stays as the on-device helper.

---

## 1. Executive Summary

This study documents the feasibility, quantitative trade-offs, and architecture of a potential future rewrite of the Windows CopyClip hub from Python into native Rust, alongside an internal supervisor watchdog design.

The current implementation (Python on Windows + Java Dex under Shell UID on Android) is nearing completion and has resolved over 24 subtle platform bugs. While Rust provides tangible systemic advantages for distribution, memory footprint, and process supervision, it does not alter Android framework constraints or Wi-Fi network physics.

---

## 2. Empirical Benchmark Comparison

Metrics are derived from live production benchmarks on Windows 11 (Predator Helios 300), Samsung Galaxy S24 Ultra, and Samsung Galaxy Tab S10 Ultra across a Tailscale WireGuard mesh.

| Metric | Current Stack (Python 3.11) | Future Stack (Rust Native) | Net Real-World Impact |
| :--- | :--- | :--- | :--- |
| **Windows Idle RAM** | 45 MB to 55 MB | 4 MB to 8 MB | ~45 MB RAM saved on Windows host |
| **10 MB Transfer Peak RAM** | 90 MB to 110 MB | 18 MB to 22 MB | Spikes reduced by ~75 percent with zero GC pauses |
| **Idle CPU Usage** | 0.0 percent (verified on PC and S24) | 0.0 percent (IOCP sleep) | Identical battery and thermal impact |
| **Active CPU Spike (10 MB)** | 4 to 7 percent for ~200 ms | 1 to 2 percent for ~40 ms | Minor CPU burst reduction |
| **10 MB Transfer Time (TCP)** | 1.52 seconds (6.74 MB/s) | 1.40 to 1.45 seconds | Only ~0.1s faster (limited by Wi-Fi and flash I/O) |
| **Legacy ADB Chunking (10 MB)** | 3.20 seconds | 2.80 seconds | ~0.4s faster due to pipe serialization removal |
| **Text Sync (Phone to PC)** | 0.2 milliseconds | 0.1 milliseconds | Imperceptible (both instantaneous) |
| **Text Sync (PC to Phone)** | 364 ms (median) | ~330 milliseconds | Imperceptible (dominated by Android Binder and sleep wake) |
| **Cold Startup Time** | 300 ms to 800 ms (or PyInstaller unpack) | 5 ms to 10 milliseconds | Instant daemon launch |
| **Deployment Footprint** | Python runtime or 80 MB bundle | Single 6 MB to 8 MB stripped `.exe` | Clean zero-dependency deployment |

---

## 3. What Rust Would Improve

### 3.1 Single Binary Zero-Dependency Deployment
* Eliminates the need for a local Python runtime, virtual environments, or `uv`.
* Replaces PyInstaller bundles that unpack DLLs into `%TEMP%` on boot.
* Compiles into a single statically-linked `CopyClip.exe` that runs directly from any path or Windows Startup folder.

### 3.2 Deterministic Memory Management and Zero GC Pauses
* Python holds boxed `PyObject` wrappers, Base64 strings, and raw image byte arrays in heap memory until garbage collection cycles trigger.
* Rust leverages RAII and zero-copy byte buffers (`bytes::Bytes`). Memory is freed the precise nanosecond a network chunk receives an ACK.

### 3.3 True Multithreading Without the Global Interpreter Lock (GIL)
* In Python, CPU-bound tasks such as SHA-256 hashing, Base64 transcoding, and DIBv5 bitmap header synthesis hold the GIL.
* Rust utilizes Tokio async tasks and hardware-accelerated AVX2 SIMD routines across physical CPU cores. A large file transfer never causes micro-jitter on concurrent text clipboard events.

### 3.4 Direct Socket-Level ADB Protocol
* Current Python code spawns `adb.exe` child processes via `subprocess.Popen`, introducing `CreateProcessW` overhead and potential zombie process leaks during network disconnects.
* Rust can speak directly to the ADB server on TCP port 5037 or maintain direct wireless adb TCP connections, eliminating external process invocation entirely.

### 3.5 Typestate Compile-Time State Machine Safety
* Rust typestate patterns enforce node connection states (Disconnected, Handshake, Streaming, Backoff) at compile time.
* It is impossible to call data transmission methods on a disconnected node type. Data races are eliminated via `Send` and `Sync` compiler guarantees.

---

## 4. What Rust CANNOT Change (Platform Invariants)

### 4.1 Android Bridge Remains Java Dex
* Android 10 and newer strictly restrict background clipboard access to the Shell UID (UID 2000).
* Android clipboard management resides in the framework Java Binder service `android.content.IClipboard`.
* Accessing `IClipboard` requires reflection and execution via `app_process`.
* Rust cannot replace `ClipBridge.dex` on Android. The Android bridge will always remain Java bytecode.

### 4.2 Win32 Clipboard Timing Locks and Quirks
* Errors such as `ERROR_CLIPBOARD_NOT_OPEN` (1418), locks held by third-party applications (Office, Chromium, Excel), and synthetic clipboard loops occur at the Windows OS kernel and user32 layer.
* Rust must implement identical exponential backoff retry loops, message pump windows (`WM_CLIPBOARDUPDATE`), and dual-format DIB/DIBv5 placement as currently implemented in `winclip.py`.

---

## 5. Internal Watchdog Architecture

Operating systems terminate all process memory upon a crash. A terminated process cannot restart itself. The standard resilient architectural pattern is a dual-tier Supervisor-Worker architecture.

### 5.1 Pattern 1: Supervisor-Worker Process Model (Recommended)
1. **Launch Sequence**: The user or system launches `CopyClip.exe`.
2. **Role Determination**:
   * If invoked with no flags, the process acts as the **Supervisor** (memory footprint ~1 MB).
   * The Supervisor immediately launches `CopyClip.exe --worker` as a hidden child process.
3. **Execution**:
   * The Worker process runs the actual clipboard listener, webview/tray UI, network sockets, and Android synchronization.
   * The Supervisor process calls Windows API `WaitForSingleObject(worker_process_handle, INFINITE)` with 0 percent CPU utilization.
4. **Crash Recovery Loop**:
   * If the Worker exits with code `0` (user selected 'Quit' in tray), the Supervisor exits cleanly.
   * If the Worker crashes with an unhandled panic, segmentation fault, or exit code != 0:
     * The Supervisor wakes up instantaneously (0 millisecond latency).
     * The Supervisor records the crash timestamp and exit code to `logs/crash.log`.
     * The Supervisor waits 500 milliseconds (backoff guard against crash loops).
     * The Supervisor spawns a fresh `CopyClip.exe --worker` instance.
     * If more than 5 crashes occur within 60 seconds, the Supervisor aborts relaunching and logs a fatal error to avoid battery and CPU thrashing.

### 5.2 Pattern 2: Windows Native Application Recovery and Restart (ARR)
* Windows provides `RegisterApplicationRestart` in `kernel32.dll`.
* On startup, the application registers with Windows Error Reporting:
  `RegisterApplicationRestart(L"--crash-recovered", 0)`
* If the application crashes due to an unhandled access violation or C++ exception, the Windows OS Error Reporting subsystem automatically re-launches the application.
* Limitation: Does not trigger if the process is terminated via Task Manager or killed by an external tool.

### 5.3 Pattern 3: Windows Service Failure Actions
* If installed as a Windows background service via the `windows-service` Rust crate:
* Service Control Manager (SCM) failure recovery can be configured via:
  `sc.exe failure CopyClip reset= 86400 actions= restart/2000/restart/5000/restart/10000`
* SCM automatically revives the service on failure without any secondary supervisor process.

---

## 6. AutoHotkey Fleet Integration Comparison

### Current Python Setup in BackgroundAutomations.ahk
* Requires a 60-line helper function `RestartNamedPythonServer()`.
* Must copy `pythonw.exe` to `PythonExes/CopyClip_Python.exe` so Task Manager shows an identifiable process name.
* Must run WMI queries (`SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'python%'`) to parse command-line paths and terminate duplicate daemons.

### Future Rust Setup in AutoHotkey
The entire WMI and process renaming overhead is eliminated.

1. **Watchdog Registration in Watchdog.ahk**:
   `CopyClip.exe` registers directly in the existing `WATCHDOG_APPS` array:
   ```ahk
   { name: "CopyClip.exe", path: PATH_COPYCLIP "\CopyClip.exe" }
   ```
   `Watchdog.ahk` natively checks `ProcessExist("CopyClip.exe")` every 10 seconds. If missing, it relaunches silently.

2. **Reload on Hotkey (Win+Ctrl+Alt+R) in BackgroundAutomations.ahk**:
   Replaces `RestartNamedPythonServer` with a clean two-line call:
   ```ahk
   if ProcessExist("CopyClip.exe")
       ProcessClose("CopyClip.exe")
   Run('"' PATH_COPYCLIP '\CopyClip.exe"', PATH_COPYCLIP, "Hide")
   ```

3. **Defense in Depth**:
   * **Internal Supervisor**: Provides sub-second (0 ms to 100 ms) instant recovery from runtime bugs, socket panics, or memory faults.
   * **AutoHotkey Watchdog**: Provides host-level insurance if the entire process tree is terminated or after a system sleep/reboot event.

---

## 7. Migration Prerequisite & Staging Plan

When the project reaches post-V1 stability and if this migration is scheduled:

1. **Phase 1: Winclip Rust Port (Crates: `windows`, `tokio`)**:
   Port Windows clipboard listening (`WM_CLIPBOARDUPDATE`), message pump, and DIB/DIBv5 synthetic bitmap headers. Verify byte-for-byte equivalence with existing unit test suite `tests/test_imaging.py`.
2. **Phase 2: Network & Framing Port (Crates: `tokio`, `ring` or `sha2`, `base64`)**:
   Implement the line-based text and chunked image protocol matching `transports/framing.py` and `nodes/android_node.py`. Keep `ClipBridge.dex` untouched on Android.
3. **Phase 3: Supervisor Implementation**:
   Implement the `--worker` argument switch and `WaitForSingleObject` supervisor loop with crash backoff guards.
4. **Phase 4: Tray & Webview UI**:
   Port the lightweight webview control API or tray menu using native Windows tray crates (`tray-icon`).
5. **Phase 5: AutoHotkey Watchdog Switch**:
   Update `LocalPaths.ahk` and `BackgroundAutomations.ahk` to point to `CopyClip.exe`.
