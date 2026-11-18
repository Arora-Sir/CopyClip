---
name: copyclip-feature
description: Add a new feature to CopyClip (Windows-to-Samsung clipboard sync). Fires on "add X feature", "implement X", "build X" said about this project. Carries the research-first, spike-before-build, never-regress-text-sync pipeline used for image clipboard sync, so it doesn't need to be re-explained per feature.
---

# Adding a feature to CopyClip

This is the pipeline actually used to build image clipboard sync (see
`docs/bugs/SPIKE-IMAGE-CLIPBOARD.md` and the corresponding plan file for that session). It
exists so a simple prompt like "add file sharing" or "add clipboard history" automatically
gets the same rigor, without the user having to restate it every time.

Read `CLAUDE.md` first — the non-negotiables there (never regress text sync, zero deps,
never root, measure don't assume) apply to every step below and are not repeated here.

## 1. Understand before proposing

- Read `ARCHITECTURE.md` in full, or at minimum the sections touching the layer the
  feature lands in (§3 anatomy of a copy, §4 moving parts, §6 reliability mechanisms).
  Read `README.md` for the user-facing shape of what already exists.
- Identify which existing layers the feature can reuse untouched. `core/hub.py`,
  `core/dedup.py`, and `domains/sync_domain.py` are content-agnostic by design — they
  operate on bytes + MIME + hashes, never on a specific payload type. Most features should
  not need to touch them beyond a narrow, well-justified addition (image sync's only
  `hub.py` change was one size-gate branch).

## 2. Sweep prior art before designing anything

- Search `otherCopyProjects/` for the same problem solved elsewhere (scrcpy, ClipCascade,
  KDE Connect, Shizuku, Shizuku-API, AdbClipboard, ClipboardHelper, clipper,
  awesome-shizuku). Report negative findings too — "nobody has done this" is a real,
  useful, load-bearing finding, not a failed search.
- Check `otherCopyProjects/NOTES.md` for what was already adopted or rejected and why, but
  verify any specific technical claim in it against the actual source in that folder
  before relying on it — it has been wrong once before (see its corrected FakeContext
  section), and it is meant to be evidence-checked, not taken on faith.
- Distinguish adoptable mechanisms from adoptable ideas. A prior-art project's *data model*
  or *framing shape* is often reusable even when its specific *implementation technique* is
  rejected (image sync adopted ClipCascade's typed-MIME idea while rejecting its
  READ_LOGS-scraping technique, and adopted scrcpy's `FakeContext` mechanism while keeping
  the existing reflective `IClipboard` binder for the parts that already worked).

## 3. Spike anything unproven, on a real device, before writing production code

- If the feature needs a mechanism nothing in this codebase or `otherCopyProjects/` has
  ever demonstrated working (image sync's case: writing an image to the Android clipboard
  from a non-app process), build a throwaway probe under `android/spike/`, compile it with
  its own `build_*_dex.ps1` (copy the pattern from `android/spike/build_spike_dex.ps1`),
  push it to a real device, and run it.
- Write the measured results to `docs/bugs/SPIKE-<FEATURE>.md`, following `docs/bugs/README.md`'s
  conventions: real command output, not recollection; say explicitly what was NOT verified
  (don't imply full coverage); note any bugs found in ported code along the way (image
  sync found two in its scrcpy `FakeContext` port that scrcpy itself never needed to fix).
- If a step needs the device unlocked, or risks landing on sensitive personal content
  (notes, messages, photos), ask the user first and stop immediately if it happens anyway
  — don't explore further to "make the most" of an unlocked device.
- Only after the spike passes (or its gaps are explicitly scoped and accepted) start
  writing production code.

## 4. Design for the existing guarantees, not around them

Before writing code, check whether the feature could weaken any of these (all documented
in `ARCHITECTURE.md` §6):
- ACK-confirmed writes and the `ack_timeout_s` dead-channel-detection guarantee — a large
  payload must not silently make this guarantee meaningless (image sync's chunked protocol
  ACKs periodically for exactly this reason, not just at transfer end).
- The single-representative-write rule for a Samsung sync domain (no redundant writes).
- Lock-state handling (a locked device silently discards writes; queue and replay on
  unlock, mirroring the exact pattern in `ClipBridge.java`'s `doSet`/`pendingSet`/
  `flushPending`).
- Echo-loop suppression (content-hash based, never label- or URI-based — Samsung rewrites
  clip labels on relay, and content URIs are re-minted per copy).
- The outbox's single-slot-per-key design and durability guarantee. If the feature's
  payloads can be large, consider whether inlining them in `outbox.json` is still
  appropriate or whether a sidecar-file pattern (as image sync uses) is warranted.

## 5. Build as a parallel path, not a rewrite of the proven one

- New wire verbs, new branches gated on payload type, new files — preferred over editing
  the logic that already works. Text's `SET`/`CLIP`/`GET`/`DESC` framing must stay
  byte-identical through any change; a new payload type gets its own verbs if a
  single-line/inline framing would not scale to it (image sync's `SETB`/`SETC`/`CLIPB`/
  `CLIPC` chunked framing exists exactly because a multi-megabyte payload as one line
  would starve `PING`/`LOCK` frames on the same pipe).
- Any new failure mode must fail closed and log clearly, never crash the daemon or the
  on-device poll loop. Wrap platform-specific I/O (`ContentResolver`, WIC/COM, MediaStore)
  in narrow try/except that disables just that feature, not the whole node.
- Verify hash/content identity, never trust a successful-looking write or a well-formed
  header alone — this project has already relearned "verify, don't assume success" three
  separate times (see `ARCHITECTURE.md` §6.1/§6.3/§6.4); don't make it a fourth.

## 6. Test at every layer you touched, live where it matters

- Extend `tests/test_core.py` (pure logic — policy, outbox, dedup, hub) first; it runs in
  under a second and catches the most mistakes per minute spent.
- If the feature touches `nodes/android_node.py`'s wire protocol, extend
  `tests/test_android_node.py` using its existing `FakeTransport` pattern.
  If it touches Windows-side codec/API work, add a `tests/test_<feature>.py` following
  `tests/test_imaging.py`'s shape (real system APIs where practical — e.g. Windows Imaging
  Component for image decode — not everything needs to be mocked).
- Then test live: build/push the real dex, run against real hardware. Confirm the specific
  regression risk directly — for any change near the clipboard, that means writing text
  before and after the change and confirming it round-trips identically, not just that the
  new feature's own tests pass.

## 7. Update docs and config as part of the same change

- `ARCHITECTURE.md`: extend the relevant §6 subsection (reliability mechanism), add wire
  verbs to §5's table, add any new tuning knob to §7's table, and move the feature out of
  §11's "future scope" list if it was listed there.
- `README.md`: add a short section or config row if the feature is user-facing.
- New config flags default to the **safe/off** state (`allow_images` defaults `false`) and
  get flipped on only after the manual verification checklist passes — this is a deliberate
  choice, not an oversight to fix later.
- If a spike found open questions that live verification couldn't close, record them as
  open in `docs/bugs/README.md`'s tracker, using the same "OPEN, awaiting confirmation"
  convention as BUG-001 through BUG-003 — don't quietly drop them or claim full coverage.
