# CopyClip bug tracker

Open issues live here as markdown, one file per bug, so the current state of the system is
written down rather than remembered. A bug stays **OPEN** until the owner confirms it is fixed
in real use, not merely when a test passes.

## Status

| ID | Title | Severity | Status |
| :--- | :--- | :--- | :--- |
| [BUG-001](BUG-001-clipboard-toast-storm.md) | Toast storm on every copy; Link to Windows reads the clipboard despite being "off" | Medium (noise, privacy) | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-002](BUG-002-windows-emptyclipboard-1418.md) | `EmptyClipboard failed (err=1418)` drops the clip entirely | **High (data loss)** | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-003](BUG-003-duplicate-daemon-instances.md) | Two daemons run at once: every laptop copy lands on the phone twice, and log lines go missing | High | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-004](BUG-004-com-thread-init.md) | `CoCreateInstance` fails with `CO_E_NOTINITIALIZED` on some threads, silently dropping an incoming image | High (data loss) | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-005](BUG-005-duplicate-screenshot-push.md) | A screenshot re-touched in place by a background process (media scanner, thumbnail regen, ...) gets re-pushed as if it were new | Medium-High | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-006](BUG-006-screenshot-clipboard-preview-duplicate.md) | A companion app's clipboard accessibility service places a small preview image on the real Android clipboard at screenshot time, synced as a spurious duplicate of the real capture | Low-Medium | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-007](BUG-007-long-link-copy-rejected-as-non-text.md) | Copying a link (vs. plain-selected text) on the phone reports a non-standard mime (`text/x-moz-url`) the laptop doesn't recognize, so the clip is silently rejected as "non-text" and never syncs | Medium-High | **CONFIRMED, fixed in real use (2026-08-23)** |
| [BUG-008](BUG-008-restart-drops-baseline.md) | An AHK-triggered daemon restart silently drops the next phone/tablet clip as an unsynced "baseline" | High | **CONFIRMED, fixed in real use (2026-08-23)** |
| [BUG-009](BUG-009-pixel-clipbridge-printstream-crash.md) | ClipBridge crashes instantly on Android 10 (Pixel) - PrintStream constructor missing from core-oj.jar; read freeze eliminated via 30s chunk timeout | High | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-010](BUG-010-samsung-sef-screenshot-delay.md) | Samsung One UI SEF metadata trailers on JPEGs fail `isCompleteImage()`, causing an infinite 8-second thread freeze loop and 60-90s screenshot copy delay | High | **CONFIRMED, fixed in real use (2026-09-03)** |
| [BUG-011](BUG-011-pixel-screenshot-sync-and-sendclip.md) | Pixel 1 screenshot sync suppressed by `initScreenshotWatch` baseline race, 12s chunk ACK timeout, and SendClip clipboard limitation | High | **CONFIRMED, fixed and verified live on hardware (2026-09-03)** |
| [BUG-012](BUG-012-screenshot-traversal-storm-and-battery-drain.md) | High idle CPU and flash wear: 2,912 File.listFiles() stats/sec screenshot traversal storm, keyguard polling loop, and companion app 4s config write loop | High (battery, CPU, flash wear) | **CONFIRMED, fixed and verified live on hardware (2026-09-04)** |

## Companion files

- **[DEVICE-STATE.md](DEVICE-STATE.md)** — measured baseline of both devices: settings,
  package enabled-states, `appops` clipboard permissions, running cross-device services.
  Regenerate with `python tools\device_audit.py`. It is read-only and safe to run any time.
- **[SPIKE-IMAGE-CLIPBOARD.md](SPIKE-IMAGE-CLIPBOARD.md)** — Phase 0 device spike for image
  clipboard sync: proves `FakeContext` + `MediaStore` insert + `setPrimaryClip` works under
  `app_process` as shell UID, with two real bugs found and fixed in the ported scrcpy code.
  Two sub-questions (visual paste confirmation, reading a third-party app's clip URI) are
  **OPEN, awaiting confirmation in real use**, same convention as the bugs below.
- **[SPIKE-PIXEL-ACCESSIBILITY.md](SPIKE-PIXEL-ACCESSIBILITY.md)** — tested whether an enabled
  `AccessibilityService` unlocks background clipboard reads on the pixel's Android 10, the
  premise the planned pixel fallback tier depended on. **Measured false**: Android's own
  `ClipboardService` log shows a strict focus-or-system-service check with no accessibility
  exemption, denying CopyClip and three unrelated apps (including a Google system app)
  identically. Plausibly explains, via the same root cause, why Sefirah's own pixel -> laptop
  direction has never worked either. **Follow-up (2026-09-01), same file**: also measured
  that the plain clipboard-change *listener* never fires in the background at all on this
  device, a stronger, separate negative result (not just "reads get denied", "you never even
  find out a change happened"). Item E's code (`ClipAccessibilityService`, the listener,
  its manifest entry) was removed outright as a result, not kept around, there is no
  mechanism left to build on for this Android version. Closed, not blocked.
- **[SPIKE-ANDROID-USERSERVICE.md](SPIKE-ANDROID-USERSERVICE.md)**, the mechanism itself:
  a real Android app (`android_app/`) can run ClipBridge via a Shizuku `UserService` that
  spawns the same unmodified `clipbridge.dex` and speaks the existing TCP protocol, instead
  of `adb shell app_process`. Confirmed live, automatic, both directions, on phone and tab.
  **Since walked back off phone and tab the same day** (see ARCHITECTURE.md §8, "The
  installed app was walked back off phone/tab"): the reboot-survival concern that motivated
  it there turned out to already be solved by plain `adb`, so the mechanism this spike
  proved is now used only where it's actually needed, `pixel`, whose Shizuku only ever
  grants root there (see BUG below), the app-rewrite plan's phone/tab automation goal is
  superseded, not open.

## Conventions

Each bug file carries: symptom as reported, **measured** evidence (commands and their real
output, never recollection), root cause, the fix, how to verify, how to roll back, and
status.

Two rules that came out of BUG-001 and are worth keeping:

1. **Measure before concluding.** The first pass of BUG-001 misread `enabled=0` as
   "disabled" (it means DEFAULT, i.e. enabled) and a broken shell loop made every package
   look disabled. Both wrong conclusions came from not checking the raw output.
2. **Say when a root cause is inferred rather than proven.** BUG-002 fixes three real
   defects but the exact interleaving that triggered it was never reproduced, and the file
   says so.
3. **Measure content, not just events.** BUG-003's first measurement counted clipboard
   *changes* and appeared to confirm a duplicate that was really unrelated copying on the
   laptop. Counting transitions into a known content hash gave the correct answer.
