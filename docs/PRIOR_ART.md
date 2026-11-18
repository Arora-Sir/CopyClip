# Reference projects: what each one actually contributed

Shallow clones made by `tools/clone_reference_projects.ps1` into the untracked `reference-projects/` folder. These are read-only references, not forks to commit to.
This file is the point of the folder: the source is just evidence.

Everything below was checked against the real repos, and in scrcpy's case against the
source in this folder, not from memory.

---

## The problem all of these are working around

Since Android 10, clipboard access is limited to the app with focus, the default IME, or a
privileged holder. Every project here is a different answer to that, and the answers are
not equally good.

**Measured on s24-ultra (Android 16, SDK 36):**
`appops get com.android.shell READ_CLIPBOARD` returns `allow`. The **shell UID is
whitelisted**. That is the entire basis of CopyClip's design, and it is why we need no
installed app while everything below does.

**Also measured, and not documented by any project here:** clipboard access is denied while
the device is *locked*. Screen state is irrelevant. `getPrimaryClip` returns null and
`setPrimaryClip` is accepted then silently discarded, raising nothing. CopyClip detects this
via `ITrustManager.isDeviceLocked` and queues writes until unlock.

---

## scrcpy (Genymobile) — the primary reference

`server/src/main/java/com/genymobile/scrcpy/wrappers/ClipboardManager.java`
`server/src/main/java/com/genymobile/scrcpy/FakeContext.java`

**Adopted:** the core mechanism. Run under shell UID via `app_process`, talk to the real
clipboard service, use `com.android.shell` as the calling package.

**Important finding: scrcpy has moved on.** Current scrcpy no longer reflects over
`IClipboard`. It builds a `FakeContext extends ContextWrapper` (PACKAGE_NAME =
`com.android.shell`) and calls `getSystemService(CLIPBOARD_SERVICE)`, letting the framework
absorb signature changes.

**Correction (2026-08-06, image-sync research):** the paragraph originally here claimed
`FakeContext` needs a patched hidden-API jar. **That is factually wrong** and was never
checked against the source in this folder before now. `scrcpy/server/build.gradle:5` is
`compileSdk = 36`, plain AGP, no hidden-API jar dependency.
`scrcpy/server/build_without_gradle.sh:27` compiles with
`$ANDROID_HOME/platforms/android-$PLATFORM/android.jar` -- **the exact same stock jar
`build_dex.ps1` already uses.** The only hidden type `FakeContext` needs that is absent from
that jar is `android.content.IContentProvider`, and scrcpy solves it with a hand-written
5-line marker interface compiled alongside the real sources
(`scrcpy/server/src/main/java/android/content/IContentProvider.java`, see
`build_without_gradle.sh:55-58`, `# Fake sources to expose hidden Android types to the
project`). At runtime the boot classloader supplies the real interface, so the anonymous
`ContentResolver` subclass in `FakeContext` binds by dex descriptor.
`AttributionSource`/`ContentResolver(Context)` need no stub at all -- both are stock public
API.

This was re-verified hands-on for image clipboard sync: `android/spike/ImageSpike.java`
(see `bugs/SPIKE-IMAGE-CLIPBOARD.md`) compiles `FakeContext` plus the same 5-line stub
against `platforms/android-36/android.jar` with the project's own `javac`/`d8` toolchain,
with zero changes to `build_dex.ps1`'s SDK setup, and runs successfully as shell UID on the
real S24 Ultra. Adopting `FakeContext` costs one stub file, not a toolchain change --
**this is why image clipboard sync uses it** (scoped narrowly to obtaining a
`ContentResolver`; the working `IClipboard`-over-binder text path below is left untouched).

The trade-off below is still real and still the reason text sync stays on the reflective
`IClipboard` path rather than migrating to `FakeContext.getSystemService(CLIPBOARD_SERVICE)`:

Trade-off, stated plainly: scrcpy delegates version drift to the framework; CopyClip absorbs
it in `ClipBridge.call()`, which enumerates overloads and fills parameters positionally by
type. If a future Android breaks that, adopting `FakeContext`'s `getSystemService` route for
the clipboard itself (not just imaging) is the fallback -- note it also requires porting the
Samsung-specific `mContext` field rewrite for `CLIPBOARD_SERVICE` and `"semclipboard"`
(`FakeContext.java:103-126`, scrcpy issues #6224/#6523), which the current binder-direct
approach never needed.

**Two failures worth stealing:**

- Issue **#4285**: `NoSuchMethodException` on `addPrimaryClipChangedListener` on Android 14.
  This is why CopyClip polls `ClipDescription.getTimestamp()` instead of registering a
  listener. The listener also needs a hand-authored hidden `IOnPrimaryClipChangedListener`
  AIDL stub, which is a standing maintenance liability.
- Issue **#1095**: `SecurityException: Calling uid 0 does not own package com.android.shell`.
  Root breaks this, shell does not. CopyClip runs as shell and never as root, which also
  satisfies the no-root rule in the global directives.

One small divergence: scrcpy uses `ClipData.newPlainText(null, text)`; CopyClip passes a
`"CopyClip"` label so synced clips are identifiable in Samsung's clipboard UI.

---

## ClipCascade (Sathvik-Rao) — closest analogue, rejected technique

Full-stack clipboard sync: Spring Boot server, Python desktop clients, native Android app,
text + images + files, end-to-end encryption.

**Rejected: its Android clipboard technique.** It requires
`pm grant com.clipcascade android.permission.READ_LOGS` plus
`appops set com.clipcascade SYSTEM_ALERT_WINDOW allow`, then *watches logcat* for a
clipboard access error and pops a transient overlay window to steal focus long enough to
read the clipboard. That is fragile (breaks if log formats change), needs an installed APK,
and flickers an overlay.

**Adopted: its data model.** Typed MIME payloads carrying text, images and files over one
protocol. CopyClip's frame header carries a `mime` field from day one for exactly this
reason, so image support later is a config flag rather than a protocol rewrite.

**Worth revisiting** when image sync is built: its Python desktop clipboard handling and its
encryption layer.

---

## Shizuku / Shizuku-API (RikkaApps) + awesome-shizuku — Tier 2 basis

Shizuku runs a daemon under the shell UID and proxies binder calls for authorised apps.
Confirmed running on this phone: `shizuku_server` as UID `shell`, plus a live
`com.arlosoft.macrodroid:shizuku` helper.

`rish` is the terminal front end. It supports `rish -c 'cmd'` and stdin piping, which is
exactly the shape CopyClip's transport needs.

**Why this matters here:** measured on this device, `service.adb.tcp.port=5555` is set but
`persist.adb.tcp.port` is **not**, so wireless ADB dies on every reboot and making it
persistent needs root. Shizuku survives reboots, so Tier 2 (Termux + `rish`) is the
reboot-proof transport. Same dex, same protocol, different pipe.

---

## clipper (majido), AdbClipboard (PRosenb), ClipboardHelper (Potato-DiGua)

Three variations on the same idea: install an APK that exposes the clipboard over ADB
broadcast intents, e.g. `am broadcast -a clipper.set -e text "..."`.

**Rejected:** all require installing an app, which is the thing CopyClip avoids, and all
inherit the same focus restriction the app has to work around anyway.

**Kept as Tier 4 fallback design.** If both ADB and Shizuku were ever unavailable, the
broadcast-intent pattern is the documented escape hatch.

---

## kdeconnect-android (KDE) — why the app you already have never fixed this

KDE Connect is installed on this phone. Its clipboard autoshare from Android to desktop has
been effectively broken since Android 10 for exactly the reason above: a background service
cannot read the clipboard. Manual "send clipboard" still works because the user action gives
it focus.

Useful as evidence that a large, mature, well-resourced project concluded there is no clean
in-app workaround. That is the strongest argument for the shell-UID route.

---

## Summary

| Project | Mechanism | Needs installed app | Verdict |
| :--- | :--- | :--- | :--- |
| scrcpy | shell UID + app_process | No | **Core mechanism adopted** |
| ClipCascade | READ_LOGS + overlay focus steal | Yes | Technique rejected, data model adopted |
| Shizuku | shell-UID binder proxy | Yes (Shizuku itself) | **Tier 2 transport** |
| clipper family | broadcast intents | Yes | Tier 4 fallback |
| KDE Connect | in-app service | Yes | Confirms the constraint is real |

Nobody solves this cleanly without an installed app *or* shell UID. This setup already has
shell UID two independent ways, which is the advantage CopyClip is built on.
