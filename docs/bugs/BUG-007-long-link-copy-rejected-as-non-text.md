# BUG-007: copying a link (not plain-selected text) on the phone gets silently rejected as "non-text"

**Severity:** Medium-High (silent, everyday-use functional failure: any link copied via a
"Copy link"/Share-sheet style action never reaches the laptop; no data loss, nothing
corrupted, but no error is visible anywhere at the production `log_level` either)
**Status:** CONFIRMED, fixed in real use (2026-08-23)
**Affects:** phone (measured directly); tablet not independently measured, but shares the
identical `ClipBridge.java` code path (see Open points)

---

## Symptom as reported

Copying ordinary selected text on the phone syncs to the laptop every time. Copying a long
URL (e.g. an Amazon product link, or an xda-developers article link) does not: nothing
appears on the Windows clipboard, and nothing looks wrong anywhere in the UI on either end.

## Measurement

`logs\copyclip.log`, today (2026-08-23), during the user's own back-to-back testing of "short
text" vs. "long link":

```
13:03:26  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
13:04:19  phone -> laptop: 29 chars                                           (normal text, OK)
13:06:53  phone -> laptop: 96 chars                                           (normal text, OK)
13:07:58  skip clip from phone: non-text (text/x-moz-url); v1 syncs text only
13:08:14  phone -> laptop: 14 chars                                           (normal text, OK)
```

Both failures are real, logged, production events, not reproduced synthetically, and land
exactly where the user's own testing pattern predicts (a link attempt bracketed by working
plain-text copies). `config.json`'s `log_level` is `"INFO"` in production, and this exact
message logs at `log.info` (`core/hub.py`'s `on_clip()`), which is the only reason it was
visible at all; the size cap (`core/policy.py`'s `max_chars`, 800,000) and the denylist
(`denylist_regex: []`) were both checked and ruled out first: an 800,000-char cap is nowhere
near a ~230-character URL, and the denylist is empty.

A one-off, read-only re-query of the phone's *current* clipboard (same mechanism
`tools/clipboard_probe.py` uses on the Windows side, done here against Android directly:
`adb shell "CLASSPATH=/data/local/tmp/clipbridge.dex app_process / me.mohit.copyclip.ClipBridge desc"` /
`... get`) found the same Amazon URL now reporting as plain `text/plain`, 234 chars, decoding
byte-for-byte to the clean URL with no extra title text appended. This is **not** the same
clip instance that produced the `text/x-moz-url` rejection above: something (most likely the
Chrome/Samsung-Internet address-bar "clipboard suggestions" chip visible in a phone
screenshot taken during this investigation, which re-copies a history entry as plain text
when tapped) had already overwritten the original multi-mime clip by the time of this
follow-up read. It is corroborating evidence that `getText()` on this kind of clip returns
the clean URL with nothing appended, not proof that the *original* `text/x-moz-url` instance
did too (see Open points).

## Root cause

`android/ClipBridge.java` reads the Android `ClipDescription` for the current clip, which can
declare **more than one** mime type for the same item, e.g. `["text/x-moz-url",
"text/plain"]`, a common pattern for a "copy this link" action, where `text/x-moz-url` is a
Firefox-lineage URL+title compatibility format and `text/plain` is the plain fallback both
representations coerce from the same underlying text field.

The code that decided what mime label to put on the outgoing `DESC`/`CLIP` wire frame took
whichever mime came **first** in that array, unconditionally:

```java
private static String mimeOf(ClipDescription desc) {
    if (desc == null || desc.getMimeTypeCount() == 0) {
        return "unknown";
    }
    return desc.getMimeType(0);   // <-- whatever the source app listed first
}
```

Android does not guarantee "plain text first": the system Sharesheet-style "Copy" affordance
that's the natural way to copy a long URL (rather than manually selecting all of it) lists the
richer format first. Meanwhile `emitCurrentClip()` (the function that answers the laptop's
`GET` with the actual body) already treats this exact situation as legitimate text and sends
it:

```java
if (desc != null && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN)
        && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_HTML)) {
    emit("SKIP " + mime + " non-text");   // only trips when NEITHER is present
    return null;
}
...
CharSequence cs = clip.getItemAt(0).getText();   // reads fine regardless of mime order
```

So the phone correctly reads and transmits the URL's actual text, but labels it with
whatever `desc.getMimeType(0)` happened to be, which can be `text/x-moz-url`. The laptop side
(`core/models.py`) only recognises two literal strings as text:

```python
TEXT_MIMES = ("text/plain", "text/html")
```

and `core/policy.py.reject_reason()` rejects anything else outright:

```python
if not self.allow_non_text and payload.mime not in TEXT_MIMES:
    return f"non-text ({payload.mime}); v1 syncs text only"
```

`text/x-moz-url` is in neither set, so a clip whose content was read perfectly correctly gets
thrown away one step later, purely because of which mime string happened to be reported for
it. "Normal text" is unaffected because manually selecting and copying text produces a
`ClipData` that only ever declares `text/plain`; there is no second, richer format to be
listed first.

**Inferred, not measured this session:** *which* component actually attaches the
`text/x-moz-url` alternate representation (Android's own system Sharesheet "Copy" target vs.
the source app itself, e.g. the Amazon app's or Chrome's own share/copy implementation) was
not independently identified: the original clip was already gone (overwritten by the
suggestions-chip re-copy described above) by the time this was investigated, so it could not
be re-inspected directly. The fix below does not depend on knowing which component it is: it
handles the general pattern (a richer mime declared alongside a standard text fallback) rather
than special-casing this one string.

## Fix

`android/ClipBridge.java`'s `mimeOf()` now prefers the canonical text mime whenever the
`ClipDescription` declares one, checking with the same `hasMimeType()` calls
`emitCurrentClip()`'s own accept/reject gate already uses, so the label attached to a clip is
now consistent with the reason the code already decided to forward it:

```java
private static String mimeOf(ClipDescription desc) {
    if (desc == null || desc.getMimeTypeCount() == 0) {
        return "unknown";
    }
    if (desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN)) {
        return ClipDescription.MIMETYPE_TEXT_PLAIN;
    }
    if (desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_HTML)) {
        return ClipDescription.MIMETYPE_TEXT_HTML;
    }
    return desc.getMimeType(0);
}
```

`mimeOf()` feeds both `emitDesc()` (the metadata-only poll announcement) and
`emitCurrentClip()` (the actual body sent on `GET`), so both frames now agree, and both now
report `text/plain` for exactly the clips that were already being read and forwarded as text.
Nothing about *which* clips get forwarded changes: `emitCurrentClip()`'s existing
`hasMimeType` gate is untouched; only the label attached to the ones already crossing it
changes.

**Why this and not a Python-side allowlist widening.** Adding `"text/x-moz-url"` to
`core/models.py`'s `TEXT_MIMES` instead would fix this one string but leaves the actual bug
(reporting an arbitrary first-declared mime instead of the one the content is actually being
sent as) in place for the next app or Android version that lists some *other* custom mime
first alongside a `text/plain` fallback. Fixing `mimeOf()` closes the whole class of "richer
format declared first, plain-text fallback available" cases in one place, matching the
precedence the Android-side gate already established rather than inventing a new one.

**Why this doesn't touch images.** A real image clip (from Gallery, MediaStore, a screenshot
URI) does not also declare `text/plain`/`text/html` in practice, so `hasMimeType()` returns
false for both and `mimeOf()` falls through to the original `getMimeType(0)` behavior,
unchanged. `isImageMime(mime)`'s classification in `emitDesc()`/`emitCurrentClip()` is
unaffected.

## Verification

- `android/build_dex.ps1` recompiles clean with this change (confirmed: `bin\clipbridge.dex`,
  28.4 KB).
- CopyClip restarted (`transports/adb_transport.py` pushes the dex unconditionally on every
  connect); log confirms a clean reconnect to both devices with the new dex active:
  `ClipBridge up on 100.64.0.11:5555 (READY 36)` / same for `100.64.0.12:5555`.
- **(2026-08-23, confirmed live)** User re-copied the Amazon link on the phone; `logs\copyclip.log`
  shows `13:25:02 phone -> laptop: 234 chars` and, on a repeat test, `13:31:30 phone -> laptop:
  234 chars` (234 matches the URL's measured length exactly, no `skip ... non-text` lines for
  either). Normal text (`13:31:33: phone -> laptop: 96 chars`) and an unrelated organic
  screenshot sync (`13:32:02`, `759714 chars`, backed up normally) both also went through
  clean in the same window, confirming the fix regresses neither the proven text path nor
  image sync.

## Rollback

Code-only, isolated to `android/ClipBridge.java`'s `mimeOf()`. `git revert` this commit (or
restore the single-`return desc.getMimeType(0)` body) and rerun `android/build_dex.ps1`. No
`config.json` toggle exists or is needed: this is a bug fix in existing behavior, not a new
feature with a knob.

## Open points

- **The exact source of the `text/x-moz-url` representation was not identified**, only its
  effect. If a future clip is rejected under some *other* unrecognised mime string that does
  **not** also declare `text/plain`/`text/html` (a genuinely non-text share, e.g. a file),
  this fix correctly leaves it rejected; that's the intended, unchanged behavior, not a gap.
- **The tablet was not independently measured.** Both logged failures were from `phone`; the
  tablet runs the identical `ClipBridge.java`/dex, so the same mechanism almost certainly
  applies, but no `tab -> laptop` rejection was observed to confirm it directly.
- **The original `text/x-moz-url` clip's exact `getText()` content was not directly
  re-inspected**: by the time of the follow-up device read, the clip had already been
  overwritten (to a plain `text/plain` copy of the same URL) by what looks like the browser's
  own clipboard-suggestions chip. The follow-up read confirms `getText()` returns a clean URL
  for *that* representation of the same content, not definitively for the original
  multi-mime instance; treated here as strong corroborating evidence, not direct proof, per
  this project's own "say when a root cause is inferred rather than proven" rule.
