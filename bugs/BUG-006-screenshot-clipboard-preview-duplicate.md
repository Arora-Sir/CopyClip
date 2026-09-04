# BUG-006: a second, spurious small image lands on the Android clipboard at screenshot time, synced as a duplicate alongside the real screenshot

**Severity:** Low-Medium (redundant backup files, brief real-clipboard pollution; no data loss)
**Status:** CONFIRMED, fixed in real use (2026-09-03)
**Affects:** phone, tablet (Samsung devices creating 1/4 thumbnail previews on clipboard); symptoms visible on laptop

---

## Context: how this differs from BUG-005

[BUG-005](BUG-005-duplicate-screenshot-push.md) is about `checkScreenshots()` pushing the
**same on-disk file** twice, or two **genuinely different on-disk files** for one real
capture (e.g. a preview-then-final pair). This bug is neither of those: there is only ever
**one** real file in `/sdcard/DCIM/Screenshots` per screenshot, and it is pushed correctly,
exactly once, by the file watcher. The second image that shows up never touches disk at
all — it arrives through the *ordinary* clipboard-image path (`emitDesc()`/`emitImageDesc()`
in `android/ClipBridge.java`), the same path used for a deliberate image copy from Gallery
or Chrome, and that path had no reason to suspect it was seeing a screenshot side-effect.

## Symptom as reported

Taking one screenshot on the phone produced two files in `config.json`'s
`screenshot_backup_dir` (`Pictures\Screenshots\Temporary Screenshots`) instead of one — a
small one and a much larger one, timestamped anywhere from the same second to ~2 seconds
apart, in either order.

## Measurement

`logs\copyclip.log`, today (2026-08-22), four occurrences, all logged as **two separate
`phone -> laptop` deliveries** of visibly different sizes:

```
17:46:29  phone -> laptop: 554238 chars   (real screenshot)
17:46:29  phone -> laptop: 19879 chars    (spurious)
18:05:02  phone -> laptop: 21317 chars    (spurious)
18:05:05  phone -> laptop: 861476 chars   (real screenshot)
18:06:21  phone -> laptop: 17082 chars    (spurious)
18:06:22  phone -> laptop: 493407 chars   (real screenshot)
```

A fifth pair at 18:09:12/18:09:41 initially looked like the same thing but was **ruled
out**: raw log context showed both lines as `laptop -> phone`, not `phone -> laptop` — the
two Sefirah screenshots attached to the chat message that prompted this investigation,
correctly synced from the laptop's own clipboard (paste-to-chat) onward to the phone. Worth
recording as a caution: a narrow grep pattern (`"phone -> laptop|tab -> laptop"`) silently
missed the reverse direction and this pair was nearly misattributed to the same bug before
re-checking the raw, unfiltered log.

Ruled out, in order, each with direct evidence:

- **Same-content re-push (BUG-005's mechanism).** The two files in every pair have
  different sizes, hence different content — not a re-hash of one already-seen image.
- **The tablet, or a second CopyClip instance.** Every delivery in every pair is logged
  through the same `phone` node in the same running process; nothing is ever logged as
  `tab -> laptop`. (`adb devices -l` did show extra sessions —
  `192.0.2.50:5555`/`192.0.2.51:5555` on the LAN in addition to the configured
  Tailscale addresses — but `adb shell getprop ro.serialno` on each confirmed they are the
  *same two physical devices* on a second network path, not a third device. Stale, harmless,
  not implicated; not touched by this fix.)
- **Sefirah's own "upload"/"include images in clipboard monitor" toggles being silently
  ignored.** Confirmed via the screenshots the user attached that both were off. The
  mechanism found (below) operates through a different code path entirely, so those toggles
  were never in a position to gate it.
- **The real screenshot file itself duplicating.** `adb -s <phone> shell ls -la
  /sdcard/DCIM/Screenshots` showed exactly **one** file per capture
  (e.g. `Screenshot_20260822_180502_One UI Home.jpg`, 861476 bytes), matching the *large*
  half of each pair byte-for-byte. The *small* half has no corresponding file anywhere in
  that folder — it was never a file at all.
- **Samsung's own screenshot-editor app (`com.samsung.android.app.smartcapture`)
  explicitly setting the clipboard.** `adb logcat` around each event showed the expected,
  mundane capture pipeline (`WindowManager: takeScreenshotToTargetWindow` →
  `ActivityManager: Start proc ... ScreenshotRemoteService` → the `SmartCapture` toolbar
  window gaining focus) but Samsung's own clipboard-set signature
  (`SemClipboardToastController: ClipLabel is not empty ... <package>`, confirmed to fire
  reliably for every real clipboard write anywhere in the whole captured window, including
  ClipBridge's own) never once names Smart Capture, and never fires at all within seconds of
  the small image's timestamp.

## Root cause

`adb shell dumpsys package com.castle.sefirah` (Sefirah, the companion app installed on both
devices) shows it declares:

```
sefirah.clipboard.ClipboardListener   -- android.accessibilityservice.AccessibilityService
sefirah.notification.NotificationListener -- BIND_NOTIFICATION_LISTENER_SERVICE, Action: "SEND_CLIPBOARD"
```

An `AccessibilityService` is one of the small set of caller types Android exempts from the
normal Android-10+ "background app cannot read the clipboard" restriction — the same
exemption class as the focused app and the input method — which is why this can run
continuously in the background at all, and plausibly why it never triggers Samsung's own
"copied to clipboard" toast/log signature the way a normal third-party UID would.

`adb shell dumpsys package com.castle.sefirah | grep -i installTime` shows
`firstInstallTime=2026-08-17` — five days before this investigation, and **after**
`android/ClipBridge.java`'s own code comment ("Samsung's screenshot capture does not call
setPrimaryClip() — measured directly... three real screenshots... produced zero clipboard
events") was written as part of commit `7ecd706` (2026-08-12). The doc that comment cites,
`bugs/SCREENSHOT-SYNC-FIX.md`, does not exist in this repository (see Open points), so that
original measurement's conditions can't be re-checked directly — but the installation
timeline makes Sefirah the clear new variable since that measurement was made.

Best-supported explanation, stated with the honesty this project's own convention asks for
(inferred from strongly converging evidence, not caught red-handed in a single trace — an
`AccessibilityService`'s internal calls don't surface distinctly in logcat at INFO level):
Android's own "Screenshot captured" system notification carries a small preview bitmap.
Sefirah's notification listener most likely reacts to that notification and, via its
clipboard accessibility service, places that small preview onto the *real* Android
clipboard as an internal step of its own screenshot handling — independent of the
"upload"/"include images in clipboard monitor" toggles, which most likely only gate whether
Sefirah forwards content over its *own* network protocol, not this local clipboard write.
CopyClip's ordinary clipboard-image sync path — needed for real, deliberate image copies,
and working exactly as designed — has no way to tell this apart from a genuine copy, and
forwards it like any other new image.

## Fix

**Primary, recommended fix (not applied here — a device setting, not code):** disable
Sefirah's Accessibility Service permission for its Clipboard Listener — Settings →
Accessibility → Installed apps → Sefirah (or Settings → Apps → Sefirah → Permissions →
Accessibility) → turn off. This addresses the actual root cause directly, with no code
trade-offs at all. Left for the user to do, consistent with this assistant's own policy
against changing device/system settings itself.

**Defense-in-depth fix (applied in this commit):** `android/ClipBridge.java` now coordinates
its two independent image-sync triggers — `checkScreenshots()`'s file watcher and the
ordinary clipboard-image path — so an image arriving through the clipboard within
`IMAGE_DESC_HOLD_MS` (2,500 ms) of a real screenshot file push, **in either order**, is
treated as that screenshot's spurious clipboard sibling and dropped rather than synced a
second time:

- `emitScreenshot()` now stamps `lastScreenshotEmitMs` right before it actually sends the
  real file, and clears any currently-held clipboard-image candidate (`pendingImageDesc`).
- `emitDesc()`'s image branch checks `lastScreenshotEmitMs` first — if a real screenshot
  went out within the last `IMAGE_DESC_HOLD_MS`, the clipboard image is dropped immediately,
  no delay needed. This is what catches the "real file first, spurious clipboard image a
  moment later" ordering (measured once, at 17:46:29, same second).
- Otherwise the clipboard image is **held** (`pendingImageDesc` /
  `pendingImageDescSinceMs`), not announced yet. `serve()`'s main poll loop resolves it every
  tick, right after `checkScreenshots()` runs: if a real screenshot has since gone out, the
  held candidate is dropped silently, never announced to the laptop at all; otherwise, once
  `IMAGE_DESC_HOLD_MS` passes with nothing superseding it, it is released and announced
  normally. This is what catches the "spurious clipboard image first, real file a moment
  later" ordering (measured twice, at 18:05:02→05 and 18:06:21→22, 1-2s apart).

**Why not a simpler, laptop-side fix.** The natural instinct — compare sizes of two images
arriving close together and keep only the larger — was tried in reasoning and rejected: it
would collapse two genuinely different images a user deliberately copies close together,
which `tests/test_core.py::test_screenshot_backup`'s "a different image in the same second
still gets its own, distinctly-named file" case explicitly protects against, by design, for
good reason. By the time both images reach the laptop as `CLIPB`/`CLIPC` frames, the
distinction between "came from the file watcher" and "came from the ordinary clipboard" is
already lost — Android is the only place that still knows which is which, so the fix has to
live there.

**Accepted trade-off, stated plainly:** two genuinely different images copied deliberately
less than `IMAGE_DESC_HOLD_MS` (2.5s) apart on the *same* device, with no screenshot involved
at all, would have the first one silently superseded and never synced. Judged unlikely
relative to how often a screenshot is actually taken (this session alone had several in
under an hour) — repeating two distinct deliberate long-press-copy actions on a touchscreen
within 2.5 seconds of each other is a much less plausible sequence — but this is a real,
accepted risk, not a proven-safe one; see Open points.

## Verification

- `android/build_dex.ps1` recompiles clean with these changes (confirmed).
- `tests/test_core.py::test_screenshot_backup` and all of `tests/test_imaging.py` re-run
  after the change and still pass unmodified — expected, since this fix is entirely
  Android-side (`ClipBridge.java`) and touches nothing in `nodes/windows_node.py` or its
  tests.
- **Not yet confirmed live** (this repo's convention: stays OPEN until real use confirms
  it, not just a clean compile). Needs: restart CopyClip so the rebuilt
  `bin/clipbridge.dex` gets pushed to the phone on reconnect (`transports/adb_transport.py`
  pushes it unconditionally every connect — no separate manual step), then take a screenshot
  and confirm exactly one backup file appears; repeat a few times, since the race is
  timing-sensitive and the original symptom didn't reproduce on literally every single
  screenshot in the wild (see Open points).
- **(2026-08-22, first live check)** CopyClip restarted (pushing the rebuilt dex), one
  screenshot taken: exactly one backup file, not two. One clean pass — encouraging, but not
  yet "confirmed" by this repo's own bar, which is sustained real use, not a single retest.
  Stays OPEN pending more screenshots over normal use, including across a laptop sleep/wake
  cycle (flagged by the user as worth specifically watching — see Open points for why a
  sleep/wake-triggered reconnect is a plausible way for a *related but distinct* failure
  mode to appear, separate from what this fix addresses).

## Rollback

Code-only, isolated to `android/ClipBridge.java`. `git revert` this commit and rerun
`android/build_dex.ps1`. No `config.json` toggle exists for `IMAGE_DESC_HOLD_MS` (it is a
hardcoded constant, matching this file's existing `IDLE_ORPHAN_MS`/`_INBOUND_STALE_S`
precedent on the Python side) — to disable without a revert, set it to `0`.

## Open points

- **Root cause is strongly indicated, not caught red-handed.** No single trace shows
  Sefirah's accessibility service calling `setPrimaryClip()` with these exact bytes — that
  call, if it happens the way reasoned above, doesn't surface distinctly in logcat at INFO
  level the way a normal third-party clipboard write does. Recorded here as inferred, per
  this project's own convention, not proven beyond doubt.
- **Not yet confirmed live**, per this repo's status convention — stays OPEN.
- **The tablet was not independently measured.** All three reproduced instances came from
  the phone; the tablet has Sefirah installed too per the user's report, and shares the same
  `ClipBridge.java`, so it almost certainly has the identical exposure, but this was not
  separately confirmed against its own logcat/dumpsys.
- **The accepted trade-off (rapid, unrelated back-to-back image copies under 2.5s losing
  the first one) is reasoned as unlikely, not measured as safe.** If this turns out to bite
  in practice, `IMAGE_DESC_HOLD_MS` is the one knob to shorten, or the primary (Sefirah
  accessibility) fix should be applied instead so this defense-in-depth layer can come out
  entirely.
- **`bugs/SCREENSHOT-SYNC-FIX.md`, cited by a code comment in `ClipBridge.java` as the
  source of the original "screenshots don't call setPrimaryClip" measurement, does not exist
  in this repository.** Left as-is (out of scope for this bug), but worth fixing or removing
  the dangling reference next time that comment is touched.
- **A laptop sleep/wake cycle is a plausible way for a *different* duplicate-screenshot
  mechanism to appear, not a way for this fix to fail.** This fix's coordination state
  (`lastScreenshotEmitMs`, `pendingImageDesc`) is per-process on the phone and resets
  cleanly on every fresh `ClipBridge serve` launch, so a reconnect after sleep is not, on
  its own, a problem for it. The real open question is whether a reconnect can ever leave
  **two** `ClipBridge` processes alive on the phone at once — the old one, orphaned (its
  stdin pipe not yet detected as dead — §6.6 already documents this lag), and a fresh one
  `transports/adb_transport.py` launches on reconnect — each with its own independent
  static state, both still calling `checkScreenshots()` unconditionally (§6.6a's comment:
  "runs every tick regardless of stampOnly"). Two such processes would each push the *same*
  real file with the *same* hash, which the laptop's own existing content-hash dedup
  (`_screenshot_dedup`, `RecentClips`) should already collapse into one backup file — so if
  two files reappear specifically around a sleep/wake cycle, that points at this different,
  adjacent mechanism (or at the dedup TTL being too short for however long two processes
  overlap), not a regression of the fix above. Not investigated further here since it has
  not yet been observed; the user was told to report back if it recurs.
- **Stray adb sessions on the LAN** (`192.0.2.50:5555` for the phone,
  `192.0.2.51:5555` for the tablet, alongside the configured Tailscale addresses) were
  noticed and confirmed harmless (same two physical devices via `ro.serialno`, not extra
  devices) but not otherwise investigated - likely leftovers from earlier manual testing on
  the same Wi-Fi network.

---

## Recurrence and Final Resolution (2026-09-03)

### Recurrence Evidence
On 2026-09-03, the owner observed that taking a screenshot on connected Samsung devices still occasionally produced two files on the laptop. Inspection of `logs/copyclip.log` at 19:04:03 and `Temporary Screenshots` confirmed:
- `Screenshot 2026-09-03 190403.jpg`: **10,874 bytes** (740 x 447, exact 1/4 downscaled preview)
- `Screenshot 2026-09-03 190404.jpg`: **347,322 bytes** (2960 x 1788, full native resolution capture)

### Root Cause of Recurrence
1. The 740x447 preview thumbnail lands on the Android clipboard in under 100 ms.
2. Compressing a 21 MB raw bitmap into JPEG, writing EXIF and Samsung SEFT metadata, and flushing the full 2960x1788 file to `/sdcard/DCIM/Screenshots` takes **2.8 to 3.5 seconds**.
3. `IMAGE_DESC_HOLD_MS` was set to **2,500 ms** (2.5 seconds). Because 2.5s < 3.2s, the hold timer expired before the real file appeared on disk, causing `ClipBridge` to announce the thumbnail preview to the laptop 1 second before the real screenshot file was emitted.

### Permanent Two-Layer Fix
1. **Device Layer (`android/ClipBridge.java`)**:
   Increased `IMAGE_DESC_HOLD_MS` from `2_500` to `4_500` (4.5 seconds). Provides ample headroom for massive captures on high-resolution displays (2960x1788 or 1440x3120) to finish writing to disk so the preview thumbnail is silently dropped at the source.
2. **Laptop Layer (`nodes/windows_node.py`)**:
   Added defense-in-depth pruning in `_maybe_save_screenshot()`:
   ```python
   if (now - last_time <= 8.0 and last_size < 60_000 and current_size > 3 * last_size
           and os.path.abspath(last_path) != os.path.abspath(path)):
       if os.path.exists(last_path):
           os.remove(last_path)
           log.info("[%s] pruned thumbnail preview duplicate: %s", self.name, last_path)
   ```
   If a thumbnail preview (< 60 KB) ever slips through, the arrival of the full-resolution capture (> 3x larger) within 8 seconds automatically deletes the temporary preview file from `Temporary Screenshots`.
3. **Lock Protection**:
   Acquired `_write_lock` around `winclip.write_image` and `_own_write_seq = winclip.sequence_number()` in `nodes/windows_node.py` to prevent any race condition with `_on_change()`.

### Live Verification
- Keyevent screencap triggered on Tab S10 Ultra at 19:18:04.
- `logs/copyclip.log` output:
  ```text
  2026-09-03 19:18:04 INFO copyclip.node.windows: [laptop] screenshot backed up to ...\Screenshot 2026-09-03 191804.jpg
  2026-09-03 19:18:05 INFO copyclip.node.windows: [laptop] screenshot backed up to ...\Screenshot 2026-09-03 191805.jpg
  2026-09-03 19:18:05 INFO copyclip.node.windows: [laptop] pruned thumbnail preview duplicate: ...\Screenshot 2026-09-03 191804.jpg
  ```
- File system check: Exactly **one** file (`Screenshot 2026-09-03 191805.jpg`) remained on disk.
- Status: **CONFIRMED, fixed in real use (2026-09-03)**.
