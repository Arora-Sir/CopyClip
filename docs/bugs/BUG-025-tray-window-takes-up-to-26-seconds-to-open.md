# BUG-025: Clicking the tray icon sometimes took 16 to 26 seconds to show the window

## Status
FIXED IN CODE, unit-tested, and the live re-measurement is recorded below. **OPEN until confirmed in everyday use.**

## Severity
Medium. Nothing is lost, but the window looked dead and extra clicks were ignored for 20 seconds.

## Symptoms
Reported by the maintainer: the tray icon sometimes needed one very long wait before the window appeared, and clicking again did nothing.

## Measurements (2026-11-19, through the live daemon with `POST /api/ui/open`)
- A cold open takes about 0.9 to 1.3 s with Brave, and about 0.6 to 1.4 s with Chrome. Measured in isolation.
- Five open and close cycles with gaps of 1, 4, 8, 15 and 4 s: two of the five opens took 16 and 23 to 26 s. The log showed `no window appeared; trying msedge.exe`, then `trying chrome.exe`, and Chrome finally opened the window.
- Direct launches of the same command line, repeated four times with a 1.5 s window life and 3 s gap, succeeded every time (1.1 to 1.2 s). So the fault needs the daemon's timing, not the command line.
- Snapshot during a slow open: the Brave main process from the previous window (same pid) was still alive several seconds after that window closed, on CopyClip's private profile. No leftover process was seen in two other cycles, so the length of Brave's shutdown varies.

## Root cause
1. After the window closes, Brave's main process on the private profile can linger for a while. The next `brave.exe --app=...` hands off to that dying instance and exits at once. The dying instance never opens a window.
2. `_wait_for_window` checked whether the process had exited only after the full 12 s `WINDOW_WAIT_S`, so a browser that exited in 0.1 s still cost 12 s.
3. The next browser, Edge, fails the same way on this PC (it exits without a window), costing another 12 s. Chrome then opens the window at about 24 s.
4. `WindowSlot` ignores clicks for `LAUNCH_SETTLE_S` (20 s) after a launch, so clicking again looked like nothing happened.

## Fix (`windows_app/launcher.py`)
A first attempt stopped the leftover process before relaunching. It was dropped: stopping it takes as long as waiting for it (measured below), and the WMI process scan it needed took 15 s or more inside the daemon.
- `_wait_for_window` now reports "gone" `GONE_GRACE_S` (2 s) after the process exits with no window, instead of waiting out the full 12 s. The grace keeps the case where a launcher hands off and the window shows a moment later.
- Each browser has `PROFILE_SLOTS` (4) private profiles: `ui-profile\brave`, then `brave-2`, `brave-3`, `brave-4`. `pick_profile` uses the first slot whose last launched process has ended and which has not just failed. A closing browser keeps its own slot busy, so the next window opens on a free one.
- `_remember_launch` never lets a hand-off launcher that exited at once replace the still-running process that owns the profile.
- `_ensure_window` takes `retry=(exe, normal_profile)`. When an isolated-profile browser exits without a window, that profile is marked bad for `BAD_PROFILE_S` (120 s) and the same browser starts once more on another profile, before any other browser is tried. A normal-profile launch is never retried.

## Evidence
- Stop-time measurement (2026-11-19, system under load): after the window was closed, the Brave main process took 21 s to exit. A forced `p.kill()` took 22.6 s and `taskkill /T /F` took 38.3 s. So killing the leftover does not help, which is why the fix avoids the profile instead.
- `tests/unit/test_launcher.py`: `WaitForWindowTests` (fast failure, hand-off grace, long wait kept for a running browser), `ProfileRotationTests` (slot naming, busy and bad slots skipped, expiry, all-busy fallback, registry rules) and `RetryOnAnotherProfileTests`. 47 launcher tests pass.
- Live check through `POST /api/ui/open`, 8 open and close cycles with gaps of 1 to 12 s, after the fix: no launch ended in `exited without a window`, and the Edge and Chrome fallbacks were never needed (before: 5 of 8 cycles hit the fallback chain). Times ranged 2.3 to 13.8 s, and one cold start did not show a window within 40 s, but the PC was at 100 percent CPU with two other emulators and Gradle running, so those absolute times say nothing about an idle PC.
- Full unit tier: 386 tests pass (plus the 8 added here). One earlier run printed `RESULT: FAIL` and one printed a `ConnectionResetError` traceback while passing. The cause was not identified and later reruns passed. Treat it as an unexplained flake.

## Not verified
- Time-to-window on an idle PC after the fix (measured 0.9 to 1.3 s before it, with Brave, when idle).
- Why Brave's shutdown is so slow here. Load, Defender (`MsMpEng`) and the process teardown are suspects, none measured.
- Why Edge exits without a window with its private profile on this PC.
- A real tray click by a person. The measurements use the same `open_window` path through the API.
