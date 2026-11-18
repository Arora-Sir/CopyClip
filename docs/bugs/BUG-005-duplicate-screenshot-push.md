# BUG-005: a screenshot re-touched by a background process gets pushed again as if new

**Severity:** Medium-High (redundant syncs, large repeated writes, possible knock-on effect
on Windows Clipboard History — see Open points)
**Status:** CONFIRMED, fixed in real use (2026-09-03)
**Affects:** phone, tablet (both run the same `ClipBridge.java`); symptoms visible on the
laptop

---

## Context: the feature this bug lives in

This bug is in the screenshot-folder watcher (`checkScreenshots()`/`emitScreenshot()`/
`initScreenshotWatch()` in `android/ClipBridge.java`), added because Samsung's screenshot
capture does not call `setPrimaryClip()` — there is no clipboard event to catch, so this
watches `/sdcard/DCIM/Screenshots` directly on a poll tick instead. That feature was already
in the working tree, uncommitted, before this session started; this bug was found while
testing a related, session's-own feature (`nodes/windows_node.py`'s local screenshot backup
— see this repo's `ARCHITECTURE.md` §8 "Two entries in Windows Clipboard History" note and
its follow-up) which surfaced it by making repeated syncs of the same screenshot visible as
repeated backup files.

## Symptom as reported

Taking one screenshot on the phone or tablet resulted in it being synced to the laptop more
than once — not back-to-back, but recurring over several minutes. Separately (see Open
points), whenever a device screenshot synced in, Windows' own Clipboard History (Win+V)
appeared to lose every entry except the new image.

## Measurement

`logs/copyclip.log`, one ~3-minute window, six `phone -> laptop` deliveries all logged as
the same size:

```
02:03:17  phone -> laptop: 855355 chars
02:03:26  phone -> laptop: 855355 chars
02:03:34  phone -> laptop: 855355 chars
02:04:06  phone -> laptop: 855355 chars
02:05:03  phone -> laptop: 855355 chars
02:06:12  phone -> laptop: 855355 chars
```

Six identical byte counts looked at first like the same content re-delivered — the obvious
suspects were `core/dedup.py`'s hash dedup failing, or the laptop's own `write()`/
`_on_change()` echo race (a real, separate issue — see below) looping the content back out
to the phone/tablet domain and back again.

Both were ruled out by **measuring the actual saved files, not just the log**: hashing the
six backup files this produced (`nodes/windows_node.py`'s new local-backup feature) gave

| File | Size | SHA-256 (first 12 hex) |
| :--- | :--- | :--- |
| `Screenshot 2026-08-08 020317.jpg` | 855355 | `d3a36b861853` |
| `Screenshot 2026-08-08 020326.jpg` | 855355 | `98faf7d80456` |
| `Screenshot 2026-08-08 020334.jpg` | 855355 | `287aa4a6c567` |
| `Screenshot 2026-08-08 020406.jpg` | 855355 | `2b5be64007ab` |
| `Screenshot 2026-08-08 020503.jpg` | 855355 | `04c896253522` |
| `Screenshot 2026-08-08 020612.jpg` | 855355 | `beaf14805a7` |

**Identical length, six different hashes.** This rules out a dedup-logic bug (the hub
correctly treated six genuinely-different-content clips as six genuinely-different clips)
and rules out the echo-race loop too: a full search of the log for any `laptop -> phone`/
`laptop -> tab` delivery of this content, anywhere in the file, found none — every
occurrence is one-directional (`phone/tab -> laptop`), so there is no reverse leg for a
ping-pong to close.

## Root cause

`checkScreenshots()`'s "has anything changed" check used **both** filename and
`lastModified()`:

```java
if (lastScreenshotName != null && newest.getName().equals(lastScreenshotName)
        && newest.lastModified() == lastScreenshotModified) {
    return; // no change
}
```

Something on the phone/tablet (Samsung's media scanner, gallery thumbnail generation, a
cloud-backup client — not identified further, and not necessary to identify to fix this)
rewrites the current newest screenshot file *in place* without changing what it looks
like — same length, slightly different bytes, new `lastModified()`. That's exactly what the
hash table above shows. The existing check treated any `lastModified()` change as a brand
new screenshot and pushed the whole file again, in full, through `emitScreenshot()`.

`suppressHash` (checked immediately after, inside `emitScreenshot()`) does not help here: it
exists to suppress the device re-announcing its own most recent *clipboard write*, is a
single slot trivially clobbered by any other `doSet`/`doSetImage` call in between, and
wouldn't have matched regardless — the rewritten bytes genuinely hash differently every
time, so content-hash dedup provides no signal to collapse against here (unlike BUG-004's
sibling problem on the laptop side, where the duplicate really was byte-identical).

## Fix

`android/ClipBridge.java`, `checkScreenshots()`: drop `lastModified()` from the novelty
check. A screenshot is "new" when its **filename** differs from the last one sent, full
stop — Android's own timestamp-based screenshot naming already guarantees a genuinely new
capture gets a genuinely new name, so filename identity is sufficient and is what actually
answers "is this the same screenshot." `lastScreenshotModified` (the field, and its
assignment in `initScreenshotWatch()`) is removed as unused.

## Verification

- Rebuilt `bin/clipbridge.dex` via `android/build_dex.ps1`; restarted CopyClip, both phone
  and tablet reconnected cleanly and received the updated dex (`transports/adb_transport.py`
  pushes it unconditionally on every connect — no separate manual step).
- Live: take one screenshot on the phone, wait several minutes (comfortably longer than the
  ~3-minute window the original repeats spanned) — confirm exactly one `phone -> laptop`
  delivery and one backup file. Repeat for the tablet.
- `tests/test_android_node.py` (the wire-protocol/Python-side test suite touched this
  session for unrelated reasons) is unaffected by this Java-only change; rerun as a cheap
  regression check.

## Retest results, and a second, defense-in-depth fix

Live retest after the filename-identity fix above: duplicate phone/tablet screenshots went
from "mostly every time" to **rare** — a large, real improvement, not a failed fix. But not
fully eliminated. Filename-identity only catches the *measured* mechanism (same file
rewritten in place, same name, new mtime); it structurally cannot catch a case where
Samsung's screenshot flow legitimately produces **two different on-disk files** for one
real capture (e.g. a scrolling/long-screenshot preview-then-final pair, or an "edit" save
under a new name) — different filenames, so the fix above sees two genuinely "new"
screenshots.

**Update (BUG-014, 2026-09-17):** measured directly for the scrolling/long-screenshot case
specifically, and it does not use a different filename. Samsung's scroll capture overwrites
the SAME filename with the final stitched image, seconds after the initial single-page shot.
The different-filename theory above may still hold for the "edit" save case, which was not
separately measured, but it is not what scroll capture does. See
`docs/bugs/BUG-014-samsung-long-screenshot-scroll-capture-partial-sync.md` for the actual fix,
which is a bounded re-arm window on the SAME filename, not a different-filename dedup.

**Second fix, layered on top, not a replacement:** a small content-hash dedup in
`emitScreenshot()`, the Java-side equivalent of what `nodes/windows_node.py`'s
`_maybe_save_screenshot()` already does on the laptop (`core/dedup.py`'s `RecentClips`,
BUG-004's sibling fix). `ClipBridge.java` has no `RecentClips` to reuse (Python-only), so a
small purpose-built equivalent: a 32-entry-bounded `LinkedHashMap<String, Long>` (hash →
last-pushed timestamp), checked right after the existing `suppressHash` check in
`emitScreenshot()`:

```java
if (recentlyPushedScreenshot(hash)) {
    return; // same visual content pushed moments ago under a different filename
}
```

20-second window, `synchronized` for cheap defensiveness (this poll loop is currently
single-threaded per this session's own investigation, so this guards against a future
change rather than an active race — matches this project's general caution here, per
BUG-002's whole premise).

## Windows Clipboard History (Win+V) losing entries — follow-up investigation and fix

The two duplicate-push fixes above (filename-identity, then content-hash hardening) made
the "history shrinks" symptom much rarer, but a further retest showed it is **not fully
explained by duplicate pushes alone**. New data point: the shrink correlates specifically
with **larger images from the tablet** (~1.3 MB, vs. the phone's ~855 KB — see Measurement
above), and when it happens, **several** old entries disappear at once, not just the single
oldest one — while the new screenshot itself correctly appears in Win+V. That pattern isn't
explained by a simple 25-item FIFO cap; it looks like a size-driven eviction, evicting
enough old entries to make room for one large new one.

Researched directly (WebSearch) rather than assumed:

- **No documented total/aggregate size budget exists** for Clipboard History — Microsoft's
  own docs state only the 25-item and 4 MB/item caps (already cited below). The size-driven
  eviction theory is plausible and matches what was observed, but is genuinely undocumented
  — Microsoft has published nothing confirming or denying it.
- **No registry key or Group Policy adjusts item count, per-item size, or total size** —
  checked directly against this machine's registry
  (`HKCU\Software\Microsoft\Clipboard`: only an on/off `EnableClipboardHistory` flag exists,
  no size/count value), cross-checked against
  `HKLM\SOFTWARE\Policies\Microsoft\Windows\System\AllowClipboardHistory` (same, on/off
  only), and confirmed by a Microsoft-affiliated response on
  [Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/4225705/how-to-adjust-maximum-counts-of-clipboard-history)
  stating explicitly that no such adjustment exists.
- **The eviction algorithm itself (simple FIFO vs. size-aware) is undocumented either way**
  — nothing in Microsoft's docs confirms or denies that a large item evicts more than one
  old entry. This investigation's own observation is the best evidence available on that
  specific question.
- **The one real, documented lever:** `CanIncludeInClipboardHistory` — a registered
  clipboard format (Win32, [Clipboard Formats](https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-formats)),
  DWORD `0` set alongside an item's other formats tells Clipboard History to skip that item
  entirely. All-or-nothing per write (can't selectively hide just the large-image format
  while still listing something else from the same write), and whether excluding a write
  from History also stops it from **evicting** other entries was, going in, a reasonable
  but unverified inference rather than a documented guarantee.

**Decision, made with the user:** no way exists to keep synced screenshots visible in Win+V
**and** stop them from evicting other entries — those two things are not both achievable via
any documented or discoverable mechanism. Chose to exclude device-synced images from History
entirely, trading away their own visibility in the Win+V list (Ctrl+V still pastes them
correctly) for not disturbing the rest of the user's history.

### Fix

`winclip.py`'s `write_image()` now sets `CanIncludeInClipboardHistory=0` alongside the
image data, by default:

```python
CF_CAN_INCLUDE_IN_HISTORY = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
...
if exclude_from_history:
    items.append((CF_CAN_INCLUDE_IN_HISTORY, (0).to_bytes(4, "little")))
```

Threaded through as a real config toggle, not hardcoded — same "one-line revert" philosophy
as `screenshot_backup_dir` above, in case the trade-off ever needs to be undone (e.g. once
Microsoft documents or changes the underlying eviction behavior):

- `config.json`: `screenshot_exclude_from_history` (default `true`).
- `copyclip.py`'s `DEFAULT_CONFIG` → `WindowsNode(exclude_synced_images_from_history=...)`
  → `nodes/windows_node.py`'s `WindowsNode.__init__` stores it → `write()` passes it into
  `winclip.write_image(..., exclude_from_history=...)`.

Scoped correctly with no extra conditional needed: `winclip.write_image()` is called from
exactly one place in this codebase, `WindowsNode.write()` — the device → laptop path. A
laptop-native screenshot is never written back to the laptop's own clipboard (`core/hub.py`
never routes a clip back to its origin node), so this can never affect the user's own local
captures, only ones synced in from a device.

**This remains partially a spike**, per this project's own "measure before concluding, spike
unproven platform behavior" rule (`docs/bugs/SPIKE-IMAGE-CLIPBOARD.md` precedent), the two things
certain from documentation are that the excluded item won't appear in Win+V's list and that
Ctrl+V still pastes it correctly; whether exclusion also stops it from evicting *other*
entries needed live confirmation, not just code review.

### Verification

- `tests/test_core.py::test_write_image_excludes_from_clipboard_history` — confirms the
  format is actually set (available, value `0`) when `exclude_from_history=True` (the
  default), and confirms it is **not** set at all when `False` — proves the toggle actually
  gates the behavior, not just that the feature exists.
- Live: sync a large tablet screenshot with the fix active — confirm (a) it still pastes
  correctly via Ctrl+V, (b) it does not appear as a new Win+V list entry (expected
  trade-off), and (c) existing Win+V entries from before the sync survive. (c) is the part
  that was genuinely unverified going in.

## Rollback

Code-only; `git revert` the commit(s) that include these fixes. Note these fixes land
inside a larger, still-uncommitted diff (the screenshot-sync feature itself) — see Open
points. To revert *only* the History-exclusion trade-off without touching anything else,
set `screenshot_exclude_from_history: false` in `config.json` — no code change needed.

## Open points

- **Not yet confirmed in real use** — the filename-identity fix is retested and confirmed to
  drastically reduce the duplicate-push symptom; the content-hash hardening on top of it has
  not yet had a chance to be exercised by a real occurrence (by nature, rare and not
  reproducible on demand). The `CanIncludeInClipboardHistory` fix's live retest (does (c)
  above actually hold?) was in progress as of this writing. Stays OPEN per this repo's
  status convention (confirmed real use, not just a fix applied) until all three are
  confirmed.
- **If live retest shows existing Win+V entries still get evicted even with the exclusion
  fix active**, that means the "unverified inference" in the investigation above turned out
  false — Windows would be evicting history space for an item even while declining to list
  it. That would mean there is genuinely no fix available from CopyClip's side for the
  eviction itself (only for the item's own visibility, which this fix already achieves
  regardless) — record that finding here rather than continuing to search for a fix
  Microsoft hasn't documented.
- **The `_own_write_seq` race in `nodes/windows_node.py`'s `write()`/`_on_change()`** (stamps
  the own-write guard *after* the clipboard write completes, so the watcher thread can
  occasionally see the change first and misread an incoming device image as a fresh local
  capture) is real and separately confirmed this session, but is **not** the cause of this
  bug (see Measurement: no reverse deliveries in the log). Left unfixed, same as this
  session's earlier decision — a genuine, separate issue for a future pass.
- **403 `[tab] no ACK/DEFER for SET within 4.0s` warnings were found in the log** during this
  investigation, spanning 2026-08-06 01:47 to 2026-08-07 12:20 (~34 hours) — but confirmed
  historical and unrelated: entirely before this session's testing, and the current daemon
  shows only normal, isolated reconnect cycles around the times these bugs were actually
  reproduced. Worth a separate look at some point (that many ACK timeouts to the tablet
  specifically, over that long a stretch, is a real reliability signal) but out of scope
  here.
- **The screenshot-sync feature this bug lives in is itself still uncommitted work.** These
  fixes and that feature should land in the same commit once confirmed working in real use,
  rather than committing the fixes alone against an uncommitted base.
