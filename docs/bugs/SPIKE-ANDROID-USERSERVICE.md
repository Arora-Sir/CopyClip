# SPIKE: Shizuku UserService/AIDL as a replacement for `adb shell app_process`

Not a bug. A throwaway pre-implementation spike, following the `docs/bugs/` conventions (measured
evidence, never recollection; say when something is inferred rather than proven). Source:
`android_app/` (a full Gradle/Android Studio project, kept as the real starting point for the
production app, not deleted like `android/spike/`'s throwaway probes -- see Verdict). Ran
against **phone** (`100.64.0.11:5555`, SM-S928B / e3q, Android, Shizuku already active via
ADB pairing) on **2026-08-31**.

## Why this exists

The plan at `C:\Users\you\.claude\plans\when-reboot-is-done-validated-crane.md` (a full
Windows-tray-app + Android-APK rewrite of CopyClip) depends on replacing today's
`adb shell app_process ... ClipBridge serve` launch mechanism, and the abandoned Termux/`rish`
text-shell tier, with Shizuku's official `UserService`/AIDL API. Live web research (Shizuku's
own docs/source) confirmed the mechanism's *shape* -- a `UserService` bound via
`Shizuku.bindUserService` runs as UID 2000 (shell) when Shizuku itself was started over ADB --
but explicitly could not confirm whether the shell-UID `appops READ_CLIPBOARD` grant this
project already relies on (`ClipBridge.java`'s exact reflective `IClipboard` binder mechanism,
calling package `com.android.shell`) actually carries over to a call made from *inside* a
UserService process, as opposed to a process spawned directly by `adb shell app_process`. That
gap had to be measured before any production networking/sync code was built on top of it.

## Result: the core hypothesis is confirmed, measured on a real device

| # | Question | Result |
| :---: | :--- | :--- |
| 1 | Does a fresh Gradle/Kotlin/Compose Android Studio project (AGP 8.12.0, Kotlin 2.2.21, Shizuku 13.1.5) build clean against this project's target devices? | **YES** |
| 2 | Does the app detect Shizuku's binder and reflect its live/dead state via `addBinderReceivedListenerSticky`/`addBinderDeadListener`? | **YES** |
| 3 | Does Shizuku's real permission dialog appear and grant correctly via `Shizuku.requestPermission()`? | **YES** |
| 4 | Does `Shizuku.bindUserService(...)` succeed and does the resulting process report `uid=2000` (shell)? | **YES** |
| 5 | Does the exact reflective mechanism `ClipBridge.java` already uses (`ServiceManager.getService("clipboard")` -> `IClipboard$Stub.asInterface` -> `getPrimaryClipDescription()` with `callingPackage="com.android.shell"`) work unchanged inside that UserService? | **YES** |
| 6 | Does the shell-UID `appops READ_CLIPBOARD` grant carry over to this new hosting method? | **YES, confirmed** -- no `SecurityException`, a real `ClipDescription` came back |

## Measured transcript (real device, real Shizuku permission grant, real probe call)

Screenshots taken at each step (not just logcat) to avoid the exact mistake `docs/bugs/README.md`
warns about (misreading state without looking at the real output):

```
Shizuku binder alive: true, permission granted: false      <- app launch, before grant
                                                              (adb logcat: "requestPermission:
                                                              pingBinder=true hasPermission=false"
                                                              then "Shizuku.requestPermission()
                                                              called")

[Shizuku system dialog] "Allow CopyClip to access Shizuku?" [Allow all the time] [Deny]
                                                              <- real dialog, confirmed via
                                                              screenshot, from
                                                              moe.shizuku.privileged.api's own
                                                              RequestPermissionActivity

Shizuku binder alive: true, permission granted: true        <- after tapping Allow all the time

probe() result:
uid=2000
service(clipboard) OK
getPrimaryClipDescription() OK: ClipDescription { text/plain hasLabel(40) <08-31 17:33:40.133> }
```

`uid=2000` confirms the UserService is genuinely running as shell UID, matching `Shizuku.java`'s
own doc comment for an ADB-mode-started Shizuku. The `ClipDescription` came back with real,
current clipboard metadata (a text clip, present on the device at the time) -- not `null`, not
a `SecurityException`. This is the specific, previously-unproven claim the whole spike existed
to check.

## One real bug found and fixed during the spike (not a platform limitation)

`IUserService.aidl` initially declared an explicit transaction id only on `destroy()` (required
by Shizuku's own contract) and left `probe()` un-numbered:

```
aidl: You must either assign id's to all methods or to none of them.
```

**Fix:** assign every method an explicit id (`probe() = 1`), matching the pattern Shizuku's own
official demo (`Shizuku-API/demo`) uses throughout. Not device-specific, a straightforward AIDL
compiler rule -- included here only because it would otherwise surprise the next person to add
a method to this interface without numbering it.

## What was deliberately not tested yet, and why

- **Only `getPrimaryClipDescription()` was probed, not `setPrimaryClip`/full `getPrimaryClip`.**
  The spike's job was narrowly "does the privileged binder call reach the clipboard service and
  get authorized," not "port the full sync protocol." `ClipBridge.java`'s `call()`/`buildArgs()`
  reflection helpers were mirrored exactly (same calling package, same overload-resolution
  logic), so there is no reason to expect `setPrimaryClip`/`getPrimaryClip` to behave
  differently, but this is **not yet directly measured** and should be confirmed before the
  full sync logic is considered proven, not just the read-metadata path.
- **Boot-triggered auto-start (the actual point of moving off ADB-shell launches) is not built
  yet.** This spike's `MainActivity` binds the UserService on a manual button tap, from a
  foreground Activity. The `RECEIVE_BOOT_COMPLETED` + foreground-service path -- and with it,
  android-mastery's documented "boot startup race" polling pattern (not yet needed here since
  nothing auto-starts yet) -- is separate, later work.
- **Only tested on the S24 Ultra**, per the plan's own rollout sequencing (test the clean
  Shizuku-AIDL tier on a device with working non-root Shizuku first, not the pixel, which
  structurally cannot use this tier at all -- see the plan's Pixel Tier 2 history section).
  Not yet run on the Tab S10 Ultra or the pixel.
- **Shizuku's official demo's other pattern (`ShizukuBinderWrapper`/`transactRemote` for
  *existing* system AIDL interfaces, called from the app's own normal process) was not
  exercised.** Only the `UserService`/`bindUserService` path was tested, since that is what the
  plan actually calls for.

## Cleanup performed

Diagnostic screenshots and `uiautomator` dumps (`/sdcard/screen*.png`, `/sdcard/window_dump*.xml`)
were pulled to this session's scratch directory for verification and are not part of the repo.
No cleanup of the phone itself was strictly needed -- nothing was written to its storage beyond
those temporary dump files, which `adb shell rm` cleared. The Shizuku permission grant for
`me.mohit.copyclip` was left in place (the user explicitly approved granting it, and revoking it
would just mean re-granting on the next real test).

## Verdict

**Proceed to building the real UserService implementation on top of this confirmed mechanism.**
`android_app/` is kept (not deleted) as the actual starting point for the production app, unlike
`android/spike/`'s throwaway-and-delete convention, because the Gradle project scaffold itself
(not just the probe code) is real, reusable infrastructure. Next steps per the plan: port
`ClipBridge.java`'s full `getClip`/`setClip`/poll-loop/networking logic into the UserService
(replacing the narrow `probe()` method), then the boot-triggered foreground service, then the
AccessibilityService fallback tier for the pixel. The "not yet tested" items above stay open
until that work directly exercises them.

## Follow-up (same day, 2026-08-31): full implementation built and confirmed on two real devices

Rather than porting `ClipBridge.java`'s logic into Kotlin (re-earning every historical fix a
second time, blind), `shizuku/BridgeServer.kt` mirrors `termux/copyclip_node.py` instead: the
UserService extracts the exact, unmodified `clipbridge.dex` (bundled as an APK asset) to
`/data/local/tmp/`, spawns it via `ProcessBuilder("app_process", "/",
"me.mohit.copyclip.ClipBridge", "serve")`, and pipes its stdio over a TCP `ServerSocket` using
`shizuku/Framing.kt`, a Kotlin port of `transports/framing.py` byte-for-byte. No clipboard-
touching code was rewritten at all -- only how ClipBridge is launched and reached.

**One real bug found and fixed**, worth recording since it silently produces `AUTH-FAIL` with
no other symptom: `SettingsStore.keyBytesOrNull()` initially hex-decoded the pasted key string
into 32 raw binary bytes. The laptop side (`copyclip.py`'s `make_transport_factory` /
`copyclip_node.py`) never does this -- `open(key_file, "rb").read().strip()` uses the key
file's raw bytes as-is, which happen to *look* like hex text but are used as 64 ASCII-character
bytes, not decoded. Two different keys, two different HMACs, indistinguishable from a genuine
wrong-key mismatch in the error message. Fixed by returning `text.toByteArray(Charsets.
US_ASCII)` directly. Confirmed fixed live: after the fix, `[phone] connected` and real content
moved automatically in both directions.

**Confirmed working, live, both directions, both devices actually used daily:**
- S24 Ultra (100.64.0.11): `laptop -> phone: 48 chars` (a real clip, not a test payload).
- Tab S10 Ultra (100.64.0.12): `tab -> laptop: 119997 chars`, including the existing
  baseline/dedup/domain logic correctly recognizing the same content already mirrored to phone
  via Samsung Continuity (`baseline from phone (119997 chars), not applied`).
- Both devices' own in-app status card updated live and correctly (`Connected`, `client
  <laptop's tailscale ip>`), confirmed via real screenshots, not just log lines.

**Still not independently re-verified through this specific new path** (distinct from "unproven" --
this is the same unmodified `clipbridge.dex` already proven for these in production for months):
image sync specifically, and a real cold-boot survival test (the boot receiver + foreground
service are wired per android-mastery's documented patterns but have not yet been exercised by
an actual device reboot).

**Not attempted in this pass, a real remaining gap, not a rounding error:** the pixel's
AccessibilityService fallback tier. This tier is architecturally distinct (a different Android
permission model entirely, no Shizuku involved) and was not started -- the pixel is unaffected
by any of this and remains on Sefirah, exactly as before.
