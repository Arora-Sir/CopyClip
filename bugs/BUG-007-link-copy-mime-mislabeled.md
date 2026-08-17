# BUG-007: a "copy link" clip is reported under the wrong mime and rejected as non-text

**Severity:** High (real content silently never syncs, no error surfaced)
**Status:** OPEN — fix applied, awaiting real-use confirmation
**Affects:** phone, tablet (both run the same `ClipBridge.java`); symptom appears on the
laptop as "some copies just don't arrive"

---

## Symptom as reported

Some text copied on the phone — specifically links — didn't sync to the laptop, while
other text copied around the same time synced normally. Retrying later (~15 minutes)
appeared to work.

## Measurement

`logs/copyclip.log`, same boot as BUG-006 (2026-08-14):

```
03:11:39  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
03:13:35  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
03:34:52  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
03:36:39  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
```

Four rejections, all the same mime, all from a `core/policy.py`-level rejection inside
`core/hub.py`'s `on_clip()` — i.e. a full `CLIP` payload was received from the phone and
then rejected, not a delivery failure. **A policy rejection is never queued to the
outbox and never retried** (`core/hub.py::on_clip`, `reject_reason` branch returns
immediately) — so the "worked 15 minutes later" was almost certainly the same link copied
a *different* way the second time (e.g. long-press-select producing plain `text/plain`),
not the original rejected clip eventually getting through.

## Root cause

`android/ClipBridge.java`:

- `mimeOf(desc)` returns `desc.getMimeType(0)` — whichever mime Android lists **first** in
  the clip's declared type array.
- A "copy link" action (browser address-bar copy, a share-sheet's "copy link" button, and
  apparently others) commonly declares **multiple** mime types on one `ClipData` — e.g.
  `["text/x-moz-url", "text/plain"]` — with a genuine `text/plain` alternate present but
  not first.
- `emitCurrentClip()`'s skip gate checks `hasMimeType(TEXT_PLAIN) || hasMimeType(TEXT_HTML)`,
  which is **not** order-dependent, so it correctly recognized this as text and did not
  skip it on the Android side — it went on to extract real, syncable text via
  `clip.getItemAt(0).getText()`.
- But the actual `emit("CLIP " + mime + ...)` / `emit("DESC " + mime + ...)` calls used the
  *original* index-0 string (`text/x-moz-url`) as the reported label, not the type that
  had actually justified treating it as text.
- `core/policy.py`'s `reject_reason()` on the Windows side is working exactly as intended
  — `TEXT_MIMES = ("text/plain", "text/html")` correctly rejects anything not literally
  one of those two labels. **No Windows-side defect.** The clip that arrived was
  genuinely text; it was mislabeled at the source.

Not specific to one browser or to `text/x-moz-url` by name — any app that declares a
non-standard primary mime alongside a genuine `text/plain`/`text/html` alternate would hit
the same mismatch.

## Fix

`android/ClipBridge.java` only; no Windows-side change.

New helper `reportedTextMime(ClipDescription desc)`: returns `text/plain` if that type is
declared (via `hasMimeType`, independent of array position), else `text/html` if that is,
else falls back to the raw `mimeOf(desc)` (a genuinely non-text clip, unaffected). Used in
place of the raw `mime` variable at the two points that report a label for
already-confirmed-text content — `emitDesc()`'s `DESC` line and `emitCurrentClip()`'s
`CLIP` line — leaving the image-mime branch and the skip-if-neither-text-type gate
untouched.

Rebuilt via `android/build_dex.ps1`; `transports/adb_transport.py` pushes the updated dex
unconditionally on every connect, so no separate manual deploy step.

## Verification

- Build: `android/build_dex.ps1` completed clean, `bin/clipbridge.dex` (28.2 KB)
  regenerated.
- Live, still needed: reproduce the exact action that logged `text/x-moz-url` (a browser
  "copy link" action) and confirm `logs/copyclip.log` now shows `phone -> laptop: N chars`
  instead of `skip clip from phone: non-text (...)`; paste on the laptop and confirm the
  content is the correct URL; confirm plain-text copies are unaffected (regression check).

## Rollback

Code-only; `git revert`, then rerun `android/build_dex.ps1` to restore the prior dex.

## Open points

- **Not yet confirmed in real use**, per this repo's status convention.
- **What `clip.getItemAt(0).getText()` actually contains for a real `text/x-moz-url` clip
  was not measured before this fix** — commonly just the bare URL, but some apps encode a
  two-line `URL\nTitle` form for this mime. Either is a large improvement over not syncing
  at all; worth checking what actually lands on the laptop during live verification, and
  only worth adding parsing/cleanup if that measurement shows it's needed — none added
  speculatively here.
