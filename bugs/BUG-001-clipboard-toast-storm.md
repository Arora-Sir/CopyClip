# BUG-001: Toast storm on every copy, and Link to Windows reading the clipboard while "off"

**Severity:** Medium (noise, and a genuine privacy concern: an app is reading every clip)
**Status:** OPEN — fix applied, awaiting real-use confirmation
**Affects:** S24 Ultra primarily; Tab S10 Ultra since clipboard alerts were enabled there too

---

## Symptom as reported

- Copy on **tablet** -> tablet shows 1 toast ("Copied to your connected devices"); **phone
  shows 3**: "Link to Windows pasted from your clipboard", "Copied from your S10 Ultra",
  "Shell pasted from your clipboard".
- Copy on **S24** -> phone shows 2 ("Shell pasted…", "Link to Windows pasted…"); tablet
  shows 1 ("Copied from the S24_Ultra").
- Copy on **laptop** -> tablet shows "Copied from S24_Ultra"; phone shows "Link to Windows
  pasted…", "Copied to your connected devices", **and sometimes** the Shell toast.
- Link to Windows was switched **off** on both devices, yet its toasts persist, and
  "Can't connect to laptop/predator" still appears occasionally.

Initial theory was that content was being copied repeatedly between devices.

## Finding: it is not repeated copying

Almost every toast is a **read** notification. Android 12+ raises
"«App» pasted from your clipboard" whenever any app *reads* the clipboard. Three separate
readers, plus Samsung's own sync messages, fire for a single copy.

| Toast | Emitted by | CopyClip's doing? |
| :--- | :--- | :--- |
| "Shell pasted from your clipboard" | `com.android.shell` = our ClipBridge | **Yes** |
| "Link to Windows pasted from your clipboard" | `com.samsung.android.mdx` `ClipboardRedirectorService` | No |
| "Copied to your connected devices" | Samsung MCF, outgoing relay | No |
| "Copied from your S10 Ultra / S24_Ultra" | Samsung MCF, incoming relay | No |
| "Can't connect to laptop/predator" | `com.samsung.android.mdx.kit` `DeviceConnectionService` | No |

## Measured evidence

**1. The asymmetry between devices was one setting.**

```
phone : settings get secure clipboard_show_access_notifications -> 1
tab   : settings get secure clipboard_show_access_notifications -> 0   (at the time)
```

That alone explains why the tablet appeared quiet. the owner has since set both to `1`
deliberately, to keep visibility of what touches the clipboard.

**2. Nothing was actually disabled.**

```
dumpsys package com.samsung.android.mdx | grep 'User 0:'
  ... stopped=false ... enabled=0 ...
```

`enabled=0` is `COMPONENT_ENABLED_STATE_DEFAULT`, i.e. **enabled**. It does not mean
disabled. An earlier check that reported everything as DISABLED was a broken shell loop
(unexpanded `$p` made `grep -q ""` match every line); its output was wrong and was discarded.

**3. Why Link to Windows reads the clipboard while switched off.**

```
dumpsys activity services com.samsung.android.mdx

* ServiceRecord{3c5a39b u0
    com.samsung.android.mdx/.windowslink.interactor.clipboard.ClipboardRedirectorService
    c:com.microsoft.appmanager}
```

`c:` is the *client* that bound the service. **`com.microsoft.appmanager` is actively
binding MDX's clipboard redirector.** The in-app toggle ends the session; it does not unbind
the service or stop the process, so the redirector keeps a clipboard listener registered and
reads every clip.

`appops` corroborates it, showing a read minutes before the check:

```
com.samsung.android.mdx        READ_CLIPBOARD: allow; time=+6m37s ago
com.android.shell              READ_CLIPBOARD: allow; time=+6m38s ago
```

`com.samsung.android.mdx.kit/.discovery.impl.viewImpl.DeviceConnectionService` is likewise
still running, which produces "Can't connect to predator".

Also worth knowing: **Samsung Keyboard (`com.samsung.android.honeyboard`) reads the
clipboard as well**, so it can contribute a toast independently of anything here.

**4. The two clipboard paths are different components.** This is what made the fix safe:

| Path | Component | Reads clipboard via appops? |
| :--- | :--- | :--- |
| phone <-> **PC** | `mdx/ClipboardRedirectorService` bound by `appmanager` | **Yes, actively** |
| phone <-> **tablet** | `mcfds/McfDeviceSyncService`, `mcfserver/McfService` (bound by `android` and `com.android.bluetooth`) | **No — "No operations"** |

Samsung's phone-to-tablet relay (the `mcf_continuity` clip label) never reads through the
appops-tracked path, so blocking MDX should not disturb Continuity, which CopyClip's
three-device design depends on.

**5. One of the toasts really was ours.** `emitDesc()` in `ClipBridge.java` called
`getClip()` (a content read, which raises the toast) **before** comparing against
`suppressHash`. So a laptop-originated write made the device read back its own clip and
toast about it. That is the "sometimes the Shell toast appears on a laptop copy" case: not
random, just previously unexplained.

## Root cause

Three independent contributors, only the third of which is CopyClip's:

1. `clipboard_show_access_notifications = 1` surfaces every read by any app.
2. `com.microsoft.appmanager` keeps `ClipboardRedirectorService` bound, so Link to Windows
   reads every clip regardless of its visible on/off state.
3. CopyClip read clip content on **both** devices on every change, and additionally read
   back its own writes.

## Fix

**Device side** (applied to phone and tablet; reversible):

```
adb shell appops set com.samsung.android.mdx READ_CLIPBOARD ignore
adb shell appops set com.microsoft.appmanager READ_CLIPBOARD ignore
```

`ignore` makes the read silently return nothing instead of raising a SecurityException, so
the app does not crash or retry-loop. Both apps stay installed and running.

Deliberately **not** changed: `clipboard_show_access_notifications` stays `1` on both
devices at the owner's request, so he keeps visibility of clipboard access.

**CopyClip side:**

1. Never read back our own write: the poll records the `ClipDescription` timestamp set by
   our own `setPrimaryClip` and skips the content read when it matches.
2. The standby device stops reading content entirely (`MODE stamp`), emitting only a
   timestamp, which does not raise a read notification. A copy made on the standby still
   arrives, because Samsung relays it to the representative where it is read once.
3. If Continuity is detected as degraded, the standby is promoted back to `MODE full`, since
   nothing is relaying for us in that state.

Net effect: **one "Shell pasted" toast per genuine copy, on one device**, down from two per
copy plus one per laptop write.

## Verification

- Blocking gate: after applying appops on the phone, copy on the phone and confirm the
  tablet's clipboard matches within ~8 s. Continuity must still work.
- Manual: with alerts still on, copy once from each device and count toasts. Expected: no
  "Link to Windows pasted" anywhere; at most one "Shell pasted"; Samsung MCF toasts unchanged.
- Automated: `tests/test_three_device.py` asserts one content fetch per copy and zero for a
  laptop-originated write.

## Rollback

```
adb shell appops set com.samsung.android.mdx READ_CLIPBOARD allow
adb shell appops set com.microsoft.appmanager READ_CLIPBOARD allow
```

Takes effect immediately, no reboot.

## Open points

- `appops` may be reset by a Samsung update or a factory-style reset. Re-check with
  `python tools\device_audit.py` and compare against `DEVICE-STATE.md`.
- **"Can't connect to predator" is not fixed by this.** It comes from
  `mdx.kit/DeviceConnectionService`, which appops does not touch. Stopping it needs
  `pm disable-user --user 0 com.microsoft.appmanager` (or unpairing the PC in Phone Link).
  That is a larger hammer and remains the owner's call.
- Samsung Keyboard also reads the clipboard and can toast independently of all of this.
