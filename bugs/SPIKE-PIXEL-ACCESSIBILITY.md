# SPIKE: does an enabled AccessibilityService unlock background clipboard reads on Android 10?

Not a bug. A throwaway pre-implementation spike, following the `bugs/` conventions (measured
evidence, never recollection; say when something is inferred rather than proven). Ran against
**pixel** (`FA6A40311146`, sailfish, Android 10 / SDK 29) on **2026-08-31**.

## Why this exists

The app-rewrite plan's item E proposed an AccessibilityService-based fallback tier for the
pixel, since it cannot hold Shizuku without root. The causal claim underneath that design --
"an app with any enabled AccessibilityService is exempted from Android's Android-10+
background-clipboard-read restriction" -- came from this session's earlier web research on
Sefirah and XClipper, not from anything measured in this codebase. Per this project's own rule
("spike unproven platform behavior before writing production code"), this had to be checked on
a real device before building `AccessibilityBridgeServer.kt` and the rest of item E on top of it.

## Result: the claim does NOT hold on this device -- measured, not inferred

Built a minimal, real `ClipAccessibilityService` (near-empty, `onAccessibilityEvent` a no-op,
narrow config: `accessibilityEventTypes="typeWindowStateChanged"`,
`canRetrieveWindowContent="false"`) plus a plain `ClipboardManager.addPrimaryClipChangedListener()`
registered in `SyncForegroundService.onCreate()`. Confirmed via `adb shell settings get secure
enabled_accessibility_services` that the service was genuinely enabled (ground truth, not just
the Settings UI's own toggle rendering):

```
...:me.mohit.copyclip/.accessibility.ClipAccessibilityService:...
```

With the CopyClip app backgrounded (home screen), used the same `clipbridge.dex` already on
this device (shell UID, from earlier BUG-009 work) to change the clipboard externally:

```
$ adb shell "CLASSPATH=/data/local/tmp/clipbridge.dex app_process / me.mohit.copyclip.ClipBridge set <b64>"
ACK set
```

`OnPrimaryClipChangedListener` never fired (no `CopyClip` log tag output at all). The real
reason, from Android's own system log at the exact moment of the write:

```
E ClipboardService: Denying clipboard access to com.brave.browser, application is not in focus neither is a system service for user 0
E ClipboardService: Denying clipboard access to me.mohit.copyclip, application is not in focus neither is a system service for user 0
E ClipboardService: Denying clipboard access to com.google.android.as, application is not in focus neither is a system service for user 0
E ClipboardService: Denying clipboard access to com.internet.speed.meter.lite, application is not in focus neither is a system service for user 0
```

The same denial, with identical wording, fired for four unrelated apps simultaneously,
including a Google system app (`com.google.android.as`). The check's own log message names
exactly two exemptions -- "in focus" or "a system service" -- and mentions no third category.
**An active AccessibilityService is not one of the exemptions this specific Android build's
`ClipboardService` checks.**

## An important, unplanned side finding

This device already has an enabled accessibility service, `com.castle.sefirah/
sefirah.clipboard.ClipboardAccessibilityService` (confirmed in the same `settings get` output),
independently corroborating this session's earlier BUG-006-driven inference and the later
Sefirah-source research that it uses exactly this mechanism. **This measurement is consistent
with, and offers a real platform-level explanation for, something the user already reported
independently earlier this session**: Sefirah's pixel -> laptop direction has never worked,
while laptop -> pixel has. Writing to the clipboard was never subject to this restriction (only
reads are gated); if Sefirah's own accessibility-based read hits this same Android-10 wall, that
would produce exactly the one-directional behavior already observed in real use, via the same
root cause measured here, not a separate, unrelated Sefirah bug. This is a plausible,
evidence-consistent explanation, not confirmed directly against Sefirah's own attempt (it did
not appear in this session's logcat capture, so its own read attempts were not directly caught
mid-denial the way this spike's were).

## Follow-up check, same session: broadest plausible config, also measured false

Retested with `accessibilityEventTypes="typeAllMask"`, `canRetrieveWindowContent="true"`, and
`accessibilityFlags="flagRetrieveInteractiveWindows"` (the broadest realistic accessibility
config, well beyond this tier's intended narrow scope) -- to rule out "the narrow config was
the problem" before concluding the mechanism itself doesn't exist here. Uninstalled and
reinstalled for a genuinely fresh process (confirmed via a changed UID, `u0_a195` ->
`u0_a196`) and freshly re-enabled the service (`adb shell settings put secure
enabled_accessibility_services`) so nothing stale from the first run could explain a negative
result. Identical outcome: `"Denying clipboard access to me.mohit.copyclip, application is not
in focus neither is a system service for user 0"`, same wording, same co-occurring denials for
unrelated apps. **Ruled out, not just untested**: this is not a narrow-config artifact.

## What remains genuinely untested, not ruled out

- Whether this check differs on newer Android versions (where Sefirah/XClipper's own approach
  was presumably developed and tested) was not measured directly against phone/tab in this
  session -- plausible given Android's general history of loosening some restrictions and
  tightening others release to release, but not confirmed.

## Verdict

**Do not build `AccessibilityBridgeServer.kt` or the rest of plan item E's design on this
premise.** Measured twice -- a minimal narrow config, then the broadest realistic config on a
guaranteed-fresh install and grant -- with the identical result both times. This rules out a
configuration mistake; the mechanism itself does not exist on this device's Android 10. The
pixel's real options remain what they were before this session's app-rewrite work began: manual
re-arm, staying on Sefirah (now better understood: one-directional for the same reason measured
here, not a mystery), or further investigation into a different mechanism on this specific OS
version -- not yet identified. The accessibility grant made on the real device during this spike
was disabled afterward (`bugs/README.md` convention: clean up spike side effects that serve no
further purpose once a spike concludes negative).

## Follow-up spike (2026-09-01): does the plain clipboard-change listener even fire in the background, separate question from whether its *content read* succeeds

Asked directly: could "Send Clip" trigger itself automatically on every copy, instead of
needing a tap, by reacting to `ClipboardManager.addPrimaryClipChangedListener` firing in the
background and launching `SendClipActivity` (which already reads correctly once genuinely
focused, see ARCHITECTURE.md §6.8) with no user interaction anywhere in the chain? This is a
narrower, different claim than the one above: not "does an enabled AccessibilityService
unlock reads", but "does the listener callback even run at all while backgrounded, before
any read is attempted".

**Result: no, measured twice, not inferred.** The listener was already registered
unconditionally in `SyncForegroundService.onCreate()` (harmless, matching this file's own
main spike). Added a temporary `startActivity(SendClipActivity)` call as the first line of
the callback body, immediately after a log line (`"accessibility-path clip change
observed"`) that would need to print before anything else could happen. Backgrounded the
app, changed the clipboard externally the same way as the main spike (`ClipBridge ... set`
over an independent shell-UID process), twice, cleanly isolated with `logcat -c` between
runs. Neither the log line nor the `startActivity` attempt ever appeared, on either run;
the only related log line was `ClipboardService: Denying clipboard access to
com.mohitarora.copyclip, application is not in focus neither is a system service for user
0`, meaning the *notification* itself is gated by the same focus check the read is, not
just a subsequent read attempt once notified. There is no way, on this device, to even
learn that a background copy happened, let alone act on it.

**Verdict, updated:** no code changed the way the original spike's did (there is no
"different mechanism" left to keep this around for, and none is known on this Android
version). `registerAccessibilityClipListener()`, `ClipAccessibilityService.kt`, its
manifest declaration, and `accessibility_service_config.xml` were all removed outright
(2026-09-01), superseding this file's own earlier "stays in the codebase in case a
different mechanism is found later" decision, now backed by two independent negative
results (content-read denied; notification-callback never even fires) rather than the one
this file originally recorded. `SyncState`'s `accessibilityEnabled`/`accessibilityClipEvents`
fields, already unused by any UI (the card that displayed them was removed earlier the same
day), were removed with it. Confirmed clean afterward: a rebuild succeeded, and
`enabled_accessibility_services` no longer lists this app on the real device after
reinstalling. "Send Clip" (§6.8) remains a manual, tap-triggered action, permanently, not a
temporary state pending a fix, there is nothing left to fix on this platform version.

## Cleanup performed

`ClipAccessibilityService` was disabled on the pixel (`adb shell settings put secure
enabled_accessibility_services`, removing this app's entry) after the second measurement --
leaving an accessibility grant active with no working purpose is unnecessary exposure, not a
convenience worth keeping. `config.json`'s pixel entry was untouched by this spike itself
(a later, separate session change moved it to `"enabled": true` on plain Tier 1 adb, see
ARCHITECTURE.md §8, unrelated to accessibility). The app code this section originally left
in place "in case a different mechanism is found later" was fully removed in the follow-up
spike above, once two independent negative results made that unlikely enough not to be
worth the dead weight.
