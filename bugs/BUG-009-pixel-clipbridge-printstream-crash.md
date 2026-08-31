# BUG-009: ClipBridge crashes instantly on Android 10 (Pixel) -- PrintStream constructor missing from core-oj.jar

**Severity:** High (deterministic, 100% failure -- not intermittent. Every single launch
attempt on the affected device crashed before printing `READY`, so the device could never
connect at all, in either direction, text or image)
**Status:** CONFIRMED, crash root cause fixed and verified via real device reconnect
(2026-08-31). A separate question about read-direction (write-then-local-copy) freezing
surfaced afterward; a direct, clean retest of that exact sequence on 2026-09-01 (see
Follow-up session below) did not reproduce it, downgraded to **not currently reproducing,
not confirmed fixed** pending real extended-session use now that this device is back in
regular rotation, see Follow-up section for what would still confirm-or-reopen it.
**Affects:** `pixel` (a 2016 Pixel, codename `sailfish`, Android 10 / SDK 29 -- its final
official OS version) only. `phone` and `tab` run much newer Android and never hit this.

---

## Symptom as reported

When `pixel` was added as CopyClip's fourth, standalone device, sync from it behaved
inconsistently: some directions appeared to work (`phone -> pixel`, `laptop -> pixel`),
others didn't (`tab -> pixel`, anything `pixel ->` any device, all image sync). Chasing this
down first ruled out two real confounds present at the same time -- Sefirah's clipboard
accessibility service still active on the device (independently syncing some content and
making CopyClip's own success/failure hard to attribute), and wireless adb's TCP listener
not actually being armed (`adb connect` failing with either a full timeout or "actively
refused" depending on the underlying cause). Once both were addressed and `pixel` still could
not connect at all, `adb logcat` was checked directly per this project's own "measure
real device output" rule rather than continuing to guess.

## Measurement

With `pixel` reachable over Tailscale and wireless adb finally armed
(`adb -d tcpip 5555`), `logs\copyclip.log` showed a new, different failure on every single
connection attempt:

```
WARNING copyclip.node.android: [pixel] connect failed: unexpected greeting from ClipBridge: 'Killed '
INFO    copyclip.node.android: [pixel] retrying in 30s
```

repeating identically, forever. `'Killed '` is what a shell prints when a foreground job is
sent `SIGKILL` -- not a protocol error, a real process death. `adb -s <pixel serial> logcat -d`
confirmed it directly:

```
---------- beginning of crash
E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: PID: 20547
E AndroidRuntime: java.lang.NoSuchMethodError: No direct method <init>(Ljava/io/OutputStream;ZLjava/nio/charset/Charset;)V in class Ljava/io/PrintStream; or its super classes (declaration of 'java.io.PrintStream' appears in /apex/com.android.runtime/javalib/core-oj.jar)
E AndroidRuntime: 	at me.mohit.copyclip.ClipBridge.main(ClipBridge.java:241)
E AndroidRuntime: 	at com.android.internal.os.RuntimeInit.nativeFinishInit(Native Method)
E AndroidRuntime: 	at com.android.internal.os.RuntimeInit.main(RuntimeInit.java:338)
I Process : Sending signal. PID: 20547 SIG: 9
```

`adb shell getprop ro.build.version.release` / `ro.build.version.sdk` on this device: `10` /
`29`, confirmed directly, not assumed from the model name.

## Root cause

`ClipBridge.java`'s `main()` constructed its output stream using the `Charset`-typed
`PrintStream` constructor:

```java
out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true,
        StandardCharsets.UTF_8);
```

`PrintStream(OutputStream, boolean, Charset)` was added in **Java 10** (2018). Android's
`core-oj.jar` on SDK 29 (Android 10) does not have it -- only the older
`PrintStream(OutputStream, boolean, String encoding)` overload, present since Android API 1.
`phone` and `tab` run far newer Android (with a newer, Mainline-updated `core-oj.jar`) and
have always had the newer overload, which is exactly why this was never seen there: the same
source file, same dex, same reflective `IClipboard`-binder mechanism -- one specific
constructor call is the only thing that differs by SDK level here, and it crashes in
`main()` itself, before ClipBridge does anything else at all -- before `connectClipboard()`,
before `initImaging()`, before the `READY` greeting. This is why the failure looked total and
undifferentiated (nothing worked, in any direction, for any content type) rather than
resembling a narrower, feature-specific gap.

## Fix

Swapped to the older `String`-encoding-name overload, identical behavior, universally
available:

```java
public static void main(String[] args) throws java.io.UnsupportedEncodingException {
    // PrintStream(OutputStream, boolean, Charset) is Java 10+ only -- NoSuchMethodError
    // on Android 10 (SDK 29)'s core-oj.jar, measured on a real Pixel (sailfish). The
    // String-encoding-name overload below has existed since Android API 1 and behaves
    // identically; "UTF-8" can never actually trigger this checked exception, every
    // JVM/ART implementation is required to support it.
    out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true,
            "UTF-8");
```

`main()` now declares `throws UnsupportedEncodingException` rather than adding a catch block
for an exception that can provably never fire for `"UTF-8"` specifically (every Java/ART
implementation is required to support it per the platform spec) -- matching this project's
own preference for not handling scenarios that cannot happen. `StandardCharsets` stays
imported and used elsewhere in the file (lines constructing `String`/`getBytes`/
`InputStreamReader` with a `Charset` argument -- those overloads have existed since Android
API 1 and are unaffected); this fix touches only this one `PrintStream` call site.

## Verification

- `android/build_dex.ps1` recompiled clean: `bin\clipbridge.dex` (28.5 KB).
- Before the fix: every single connection attempt to `pixel` crashed (`'Killed '` greeting),
  100% reproduction, no exceptions.
- After the fix, with the exact same config and no other change: `logs\copyclip.log` shows
  `[pixel] connected` repeatedly across multiple reconnects, `ClipBridge up on
  100.64.0.13:5555 (READY 29)` each time, and a real read succeeded (`baseline from pixel
  (138 chars), not applied`) -- confirming the crash is gone and the reflective `IClipboard`
  read path works correctly on SDK 29.
- `phone`/`tab` were not touched by this change beyond a routine dex rebuild+redeploy; both
  continued connecting and syncing normally throughout (confirmed in the same log window).

## Rollback

Code-only, isolated to `android/ClipBridge.java`'s `main()`. Revert to the single
`Charset`-argument constructor call and drop the `throws` clause, then rerun
`android/build_dex.ps1`. No `config.json` toggle exists or is needed for this fix
specifically.

## Follow-up session (still 2026-08-31): writes into pixel confirmed working; a second, real, unresolved bug found in the other direction

After Sefirah's accessibility permission was revoked (see BUG-006's mechanism -- the same
permission, still granted on `pixel` independent of the app's own in-app toggle, this whole
time) and the daemon restarted clean, `laptop -> pixel` and `phone -> pixel` both worked
repeatedly in real use, for text of several sizes and for at least one real image (a
screenshot, ~660 KB decoded, delivered via the chunked `SETB`/`SETC` path). This retroactively
supports the accessibility-service-slowdown theory floated below for the earlier ACK-timeout
symptom, though it still was not isolated as the *specific* cause with a controlled A/B test
-- both the crash-era queued content and Sefirah both changed in the same window.

**A second, different, real bug was found in the read direction.** Across the entire session,
exactly one `pixel -> laptop`/`pixel -> phone` event ever fired (a 7-character text clip, the
very first thing copied on `pixel` after it first connected) -- and it never fired again,
including after later, deliberate copies on `pixel`, even though the device stayed connected
and kept receiving writes from the other devices throughout. The one-and-only success happened
*before* any write had landed on `pixel`; the silence began immediately after the laptop wrote
the image referenced above.

`android/ClipBridge.java`'s poll loop (lines ~667-690) is the suspect:

```java
ClipDescription desc = getDescription();
long stamp = desc == null ? 0L : desc.getTimestamp();
if (stamp != lastStamp) {
    lastStamp = stamp;
    ...
}
```

If `getDescription()` (a reflective `getPrimaryClipDescription()` binder call) starts
returning `null` on every subsequent poll -- for whatever reason, not yet identified --
`stamp` computes as `0L` forever. Once `lastStamp` also happens to already equal `0L`, `stamp
!= lastStamp` is permanently false and the loop goes silent: no `STAMP`, no `DESC`, no `ERR`,
nothing -- which matches exactly what was observed, including the complete absence of any
`ERR poll: ...` line that a thrown exception would have produced. This is a plausible,
evidence-consistent hypothesis, **not a confirmed root cause** -- it was not verified against
a live `adb logcat` capture at the moment of a fresh post-image copy, and no image was ever
successfully sent *from* `pixel` in this session to check whether that direction has the same
gap or a different one (no attempt shows in the log at all -- suggesting either it was never
tried, or it silently hit this exact same frozen-poll-loop wall before ever announcing
anything).

**Separately tested and ruled out this session:** whether `pixel`'s wireless adb listener
could be re-armed from *inside* the device (via Shizuku or any other shell-UID-level command,
e.g. from MacroDroid), avoiding the USB step entirely. Confirmed not possible: `adb shell
setprop ctl.restart adbd` returns `setprop: failed to set property 'ctl.restart' to 'adbd'`
-- permission denied to the shell UID. Restarting adbd to open the TCP listener is done by
the ADB client/server's own protocol channel, not a generic system property any shell-level
process (Shizuku included, since it grants that same UID level, nothing more) can trigger.
This is a hard platform limit, not a gap in this project's code.

**Why the daemon appeared to hang for several minutes with no log output**, between the
Sefirah accessibility change and the restart, was not investigated -- the process was
confirmed alive (still running under Task Manager) but produced no further log lines well
past any of `ack_timeout_s`/`idle_ping_s`'s bounds. A restart cleared it. Root cause unknown.

**`pixel` was `"enabled": false` in `config.json`** for an unrelated operational reason: it is
used only 1-2 days a month, and every cold start needs the manual `adb -d tcpip 5555` USB
re-arm (no non-root path to a persistent pairing exists on this device, see main writeup
above), which was judged a worse trade than letting Sefirah handle this device's sync
instead at the time. The crash fix and the read-direction bug above both stayed in the
codebase regardless of that setting.

## Follow-up session (2026-09-01): re-enabled, and the exact freeze scenario retested clean

`pixel` was flipped back to `"enabled": true` for an unrelated reason (see
ARCHITECTURE.md §8's "pixel moved to Tier 1" entry, this device now uses the same plain-adb
path as phone/tab). That made it possible to directly retest the specific sequence this
bug's Open points describe, rather than continue speculating.

**Test:** with the daemon's own connection to `pixel` confirmed continuously up throughout
(no reconnect in the log window, checked by line-count marker before and after each step,
not just eyeballed): (1) laptop wrote a distinct string to `pixel`, confirmed delivered;
(2) immediately after, a **separate**, independent `adb shell` invocation of `ClipBridge
... set` simulated a local copy directly on `pixel`'s clipboard (the poll loop cannot tell
this apart from a genuine user copy, both are just a `ClipDescription` timestamp change);
(3) checked whether that second change was announced.

**Result:** it was, immediately and correctly, content matched exactly (`winclip.read_text()`
returned the precise string set in step 2), no freeze, no silence, no reconnect needed to
"unstick" it. This is exactly the sequence ("a write lands on `pixel`, then a subsequent
local copy") the original symptom described as permanently breaking.

**What this does and doesn't prove:** this is real, direct, positive evidence against the
`getDescription()`-returns-null-forever hypothesis specifically for a single write-then-copy
cycle, on the current dex build, over the current plain-adb connection. It is **not** an
extended-session stress test (the original report describes the freeze surviving "later,
deliberate copies", plural, over a longer session) and the original root cause was only ever
a hypothesis, never confirmed via a live `logcat` capture at the moment of freeze, so there
is nothing here to conclusively call "fixed" against. Status moved from OPEN to **CONFIRMED
not currently reproducing** rather than CONFIRMED FIXED, pending real, extended daily use
now that `pixel` is back in regular rotation rather than shelved right after being found.
Reopen this file if the original symptom (announcements stop after some point in a session
and never resume without a reconnect) recurs during that real use, this time with a
`logcat -d` capture taken at the moment it's noticed, to actually catch `getDescription()`
in the act instead of inferring it.
