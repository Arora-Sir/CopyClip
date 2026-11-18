# BUG-018: Opening the Tk dashboard crashes the whole tray process, and with it clipboard sync

## Status
FIXED IN CODE (2026-11-12), **OPEN until confirmed in real use**. The window now runs in a separate process. The Edge app window itself could not be launched from the agent's sandbox, so its first real launch is unverified: run `python tools\demo_ui.py --open` once to confirm.

## Severity
High. The crash is not cosmetic: the dashboard lived inside the tray process, so a Tk fault ended clipboard sync for every device until the tray was restarted.

## Symptom
Opening the dashboard shows a white or half-drawn window and sometimes closes the whole app.

## Evidence (measured)
The Windows Application event log holds repeated `Application Error` events for the same process, all with the same module, exception and offset:

| When | Faulting module | Exception | Fault offset |
| :--- | :--- | :--- | :--- |
| 2026-10-20 | `tcl86t.dll` 8.6.2.15 | `0x80000003` | `0xfee74` |
| 2026-10-23 | same | same | same |
| 2026-10-29 | same | same | same |
| 2026-11-01 | same | same | same |
| 2026-11-02 | same | same | same |
| 2026-11-12 | same | same | same |

An identical offset every time means one deterministic code path, not random memory corruption. `0x80000003` is a breakpoint exception, which is what Tcl raises when it panics.

## Root cause (inferred, not reproduced)
- `windows_app/dashboard.py` created `tk.Tk()` on a background thread (`open_dashboard` starts `_run` in a daemon thread) inside the process whose main thread runs the tray's Win32 message loop. Tk is not safe off its creating thread, and a Tcl panic aborts the process.
- The window was built synchronously from one roughly 1,150-line function, after DWM attributes were applied to a still-empty window, which explains the white flash.
- No DPI awareness was set anywhere, so the window was also blurry or small on scaled displays.

## Fix
- The UI is now HTML served by a loopback-only control API inside the tray and shown in a chromeless Chromium app window (`windows_app/launcher.py`). A crash or kill of that window cannot touch sync.
- The old dashboard remains behind `python windows_app\tray.py --legacy-ui` for one release.
- `windows_app/tray.py` wraps every window launch so an exception there never reaches the message loop (`tests/unit/test_status_tray.py::test_ui_failure_never_propagates_to_the_message_loop`).

## Verified on the maintainer's PC (2026-11-13)
- The tray restarts itself in about 2 seconds through the new restart endpoint, and `--replace` takes over from the renamed `CopyClip_Python.exe`.
- On this PC, `msedge.exe --app` exited with code 0 and showed no window, with an isolated profile and with the normal one. The default browser here is Brave, which supports the same `--app` mode, so the launcher now prefers the default Chromium browser. A window titled `CopyClip` opened in Brave with no fallback.
- Live sync with the tray running new code: 17 of 17 checks in `tests/test_live_tray_sync.py` (PC, S24 Ultra, Tab S10 Ultra, pause and resume, settings round trip).

- Follow-up on window behaviour and polish: [BUG-022](BUG-022-tray-click-opens-many-windows-and-white-focus-ring.md).

## Not verified
- Startup time (about 2 seconds for the first launch of the isolated Brave profile in one measurement) and memory of the app window.
- Why Edge shows no window on this PC (not investigated; possibly policy or profile related).
