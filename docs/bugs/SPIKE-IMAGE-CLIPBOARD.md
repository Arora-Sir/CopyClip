# SPIKE: image clipboard on the shell UID (Phase 0 of image sync)

Not a bug. A throwaway pre-implementation spike, following the `docs/bugs/` conventions
(measured evidence, never recollection; say when something is inferred rather than proven).
Source: `android/spike/ImageSpike.java` + `android/spike/android/content/IContentProvider.java`
+ `android/spike/build_spike_dex.ps1`. Not referenced by production code and safe to delete
once this file exists. Ran against **phone** (`100.64.0.11:5555`, SM-S928B, Android 16 /
SDK 36) on **2026-08-06**.

## Why this exists

No project in `otherCopyProjects/` (scrcpy, ClipCascade, KDE Connect, Shizuku, ...) has ever
written an image to the Android clipboard from a non-app process (`otherCopyProjects/NOTES.md`
research, see the image-sync implementation plan §1). This had to be measured before any
production code was written.

## Result: all 9 questions answered, mechanism works end to end

| # | Question | Result |
| :---: | :--- | :--- |
| 1 | Does `FakeContext.get().getContentResolver()` construct under `app_process`? | **YES**, after two fixes below |
| 2 | Does `MediaStore.Images.Media.insert(...)` return a real `content://` URI as shell? | **YES** |
| 3 | Does `openOutputStream(uri)` accept bytes and does the file land? | **YES**, confirmed on disk |
| 4 | Does `IClipboard.setPrimaryClip` accept a `ClipData` built from public constructors around that URI? | **YES** |
| 5 | Is it actually pasteable? | **Mechanism-level PASS; live UI paste not visually confirmed** (see below) |
| 6 | Does `FileUriExposedException` fire? | **N/A** — never used a `file://` URI, by design |
| 7 | Reverse direction: read an *arbitrary third-party app's* clip URI as shell | **Not tested** (scoped out, see below) |
| 8 | Does a locked device silently discard an image `setPrimaryClip`, same as text? | **YES, confirmed** |
| 9 | Heap headroom for a 24 MiB payload in `app_process`? | **YES**, no issue |

## Two real bugs found in the ported scrcpy code, not present in scrcpy itself

scrcpy's `FakeContext` never does file I/O through its `ContentResolver` (it only uses it
for the settings/camera provider), so these were latent and never triggered upstream.

**1. `Class.forName("android.app.ActivityThread").getDeclaredConstructor().newInstance()`
throws inside a `Handler` constructor.**

```
Caused by: java.lang.RuntimeException: Can't create handler inside thread Thread[main,5,main]
that has not called Looper.prepare()
	at android.os.Handler.<init>(Handler.java:253)
	at android.app.ActivityThread$H.<init>(ActivityThread.java:2848)
	at android.app.ActivityThread.<init>(ActivityThread.java:511)
```

`app_process`'s main thread has no prepared `Looper` by default. `ClipBridge.java` never
needed one -- it talks to service binders directly and never touches `ActivityThread`.
**Fix:** call `Looper.prepareMainLooper()` once, before anything constructs `FakeContext`,
if `Looper.myLooper() == null`.

**2. `openOutputStream`/`openInputStream` throw `FileNotFoundException: No content provider:
<uri>` even though `insert()` on the same resolver just succeeded.**

```
java.io.FileNotFoundException: No content provider: content://media/external/images/media/1001223577
	at android.content.ContentResolver.openAssetFileDescriptor(ContentResolver.java:1877)
```

scrcpy's `FakeContext.acquireUnstableProvider()` always returns `null` (verbatim: `protected
IContentProvider acquireUnstableProvider(Context c, String name) { return null; }`). Measured
on this device: `ContentResolver#openAssetFileDescriptor` calls `acquireUnstableProvider()`
**first** and throws immediately on a null return -- it does not fall back to the stable
`acquireProvider()` path the way `insert()`/`query()` apparently do. **Fix:** make
`acquireUnstableProvider` delegate to the same `getContentProviderExternal` call as
`acquireProvider`. Confirmed this is what makes `openOutputStream`/`openInputStream` work.

**Consequence for the implementation plan:** `android/ClipBridge.java`'s `FakeContext` port
(Phase 3) must include this second fix. Porting scrcpy's `FakeContext.java` verbatim would
silently break image file I/O while leaving everything else scrcpy uses working, which is
exactly the kind of bug that would not surface until real-world testing.

## Measured transcript (locked phone)

```
$ ImageSpike islocked
LOCKED true

$ ImageSpike insert
INSERT_OK content://media/external/images/media/1001223578
(file confirmed on disk: /sdcard/Pictures/CopyClip/copyclip_spike_....png, 70 bytes)

$ ImageSpike clipwrite content://media/external/images/media/1001223578
CLIPWRITE_OK uri=content://media/external/images/media/1001223578 readback=null(likely locked/discarded)
```

`setPrimaryClip` raised no exception (matches text's documented behavior), and the
description readback came back `null` -- exactly the same silent-discard-while-locked
behavior `NOTES.md`/`ARCHITECTURE.md` §6.1 already measured for text. **No new lock-handling
code is needed for images**; the existing `doSet`/`pendingSet`/keyguard-replay logic in
`ClipBridge.java` is content-type-agnostic and covers this by construction.

## Measured transcript (phone unlocked, user unlocked on request)

```
$ ImageSpike islocked
LOCKED false

$ ImageSpike full
LOCKED false
HEAP_OK 24MB allocated and touched
CONTEXT_OK resolver=me.mohit.copyclip.spike.ImageSpike$FakeContext$1
INSERT_OK content://media/external/images/media/1001223579
CLIPWRITE_OK uri=content://media/external/images/media/1001223579 readback=ClipDescription { image/png hasLabel(14) <08-06 23:47:26.852> }
CLIPREAD mime=image/png uri=content://media/external/images/media/1001223579 text=null
CLIPREAD_BYTES 70 sha256=3ec322a42990a3067cc6c73f3856a86e55bdd8baf19d2166954a8fb319329a72
FULL_DONE
```

Read back through `FakeContext`'s `ContentResolver.openInputStream`, in the same process
that wrote it: byte count and content are internally consistent (same code path a real
paste handler uses -- `ClipData.Item.getUri()` + `ContentResolver.openInputStream(uri)`).

## Q5/Q7: what was deliberately not verified, and why

The plan called for pasting into Samsung Notes and screenshotting the result. Launching
Notes (`monkey -p com.samsung.android.app.notes ...`) landed on a folder named "Passcode"
containing notes titled "Windows Outlook Pass...", "Gmail Akn", "Gmail Shared", "Gmail
Julie", "Sunshine" -- personal password-related notes. **The spike was aborted at that
point**: backed out to the home screen, deleted the screenshot from both the device and
this session's scratch directory, without opening or reading any note content. No further
UI automation against installed apps was attempted for the rest of this spike.

Consequence: **visual paste confirmation into a real third-party app, and reading a clip URI
minted by a different app's provider (not our own `media` authority), remain unverified.**
This is intentionally left **OPEN, awaiting confirmation in real use**, matching the project's
existing convention for BUG-001 through BUG-003. It does not block starting the build --
the mechanism (`ClipData` built from public constructors around a `content://` URI, read
back via `ContentResolver.openInputStream`) is exactly what `ClipData` documentation and
every consuming app's paste handler expects, and is not something CopyClip can special-case
per source app. What it does change: `ClipBridge.java`'s reverse-read path (a device's own
copy) must **not assume** every source URI is readable -- wrap `openInputStream` in a
try/catch, and on `SecurityException`/`FileNotFoundException`, emit
`SKIP <mime> unreadable-uri` and move on, rather than crashing the poll loop. This is
already consistent with the plan's existing size-gate-before-decode design; it is now also a
correctness requirement, not just a size safeguard.

## Cleanup performed

Test artifacts (`Pictures/CopyClip/copyclip_spike_*.png`, `/data/local/tmp/spike.dex`, the
MediaStore rows) were deleted from the phone after the spike. `android/spike/` is left in
the repo (git-ignored build output aside) as a reference for Phase 3; it is not wired into
`build_dex.ps1` or any production path.

## Verdict

**Proceed to Phase 1-6 of the implementation plan.** The two fixes above go into
`ClipBridge.java`'s `FakeContext` port directly (Phase 3), not as a follow-up. Q5/Q7 stay
open pending real-use confirmation, tracked the same way as the three existing open bugs.
