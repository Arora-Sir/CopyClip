# CopyClip — Architecture & Operations Guide

This is the deep-dive companion to [README.md](README.md). README tells you how to run it
and configure it. This tells you *how it actually works end to end, and why it's built the
way it is* — including the reasoning behind decisions that aren't obvious from reading the
code cold, and the lessons learned from real bugs found in production.

If you just want to get it running, read README instead. Come here when you need to change
something and want to understand what you might break.

---

## 1. What this is, in plain language

CopyClip keeps one clipboard across three real, specific devices:

- **predator-helios-300** — your Windows 11 laptop
- **s24-ultra** — your Samsung Galaxy S24 Ultra phone
- **tab-s10-ultra** — your Samsung Galaxy Tab S10 Ultra tablet

Copy on any one of them, and the text shows up on the other two. No app is installed on
either Samsung device. No cloud service is involved — everything travels directly between
your own devices over Tailscale, your private mesh network. There's no account to sign into
and nothing to `pip install`.

The simplest way to picture it: **the laptop is the brain.** It runs a small Python program
(the "hub") that watches the Windows clipboard and talks to both Samsung devices over
Tailscale. The phone and tablet don't talk to each other through CopyClip at all — they
already mirror each other on their own, through Samsung's built-in "Continuity" feature
(the same thing that lets you drag files between a Samsung phone and tablet). CopyClip
treats "the phone and the tablet" as one Samsung pocket, writes to whichever one is
reachable, and lets Samsung carry it to the other for free.

---

## 2. The problem this solves, and why it's harder than it sounds

**Android doesn't let a background app read the clipboard.** Since Android 10, only the
app currently in focus, the keyboard, or a privileged system identity can see clipboard
content. That kills the obvious approach ("just write an Android app that syncs the
clipboard") unless that app is always the one on screen, which defeats the point.

The way around this: `adb` (Android's own developer bridge) connects as a special identity
called the **shell UID**, and Android specifically whitelists the shell UID for background
clipboard access — confirmed by running `appops get com.android.shell READ_CLIPBOARD`,
which returns `allow`. This is the same trick the popular screen-mirroring tool `scrcpy`
uses for its own clipboard sync feature. So CopyClip pushes a small compiled Java helper
(`ClipBridge.dex`, built from [android/ClipBridge.java](android/ClipBridge.java)) to each
device and runs it via `adb shell app_process ... ClipBridge serve` — which executes *as*
the shell UID, without installing anything as a normal app. This is why there's "no app on
either Samsung device."

**A second, non-obvious discovery: a locked phone silently discards clipboard writes.**
Not screen-off — the actual keyguard lock. With the phone locked, `setPrimaryClip` returns
successfully but the write is thrown away, and `getPrimaryClip` returns null. Nothing raises
an error. This was found by testing, not by documentation, and it shapes a real piece of the
design: every write has to check lock state and queue if the device is locked, replaying
once it unlocks, rather than trusting a successful-looking write (see §6).

**A third piece: Samsung Continuity is already syncing your phone and tablet, whether
CopyClip exists or not.** Measured directly: a clip written to the phone reappeared on the
tablet about 2.5 seconds later, tagged with a Samsung-internal label. If CopyClip ignored
this and wrote to both devices independently, every copy would arrive on each device twice
— once from CopyClip, once from Samsung — which is both wasteful and, because reading
clipboard content raises an Android notification ("Shell pasted from your clipboard" for
CopyClip's own reads), visibly noisy. So CopyClip treats phone+tablet as a **sync domain**:
it picks one of them (the "representative") to write to, and lets Samsung do the rest. See
§4 and §6.

---

## 3. Anatomy of a copy

### Laptop copies something

1. Windows raises a clipboard-changed event. `nodes/windows_node.py` reads the new text and
   hands it to the hub as a "clip observed on `laptop`."
2. The hub checks: is this exact content genuinely new, or a repeat? Either way (see §6.2
   for why "repeat" doesn't mean "skip"), it works out who still needs it.
3. Phone and tablet are one sync domain. The hub picks a **representative** — normally
   whichever one comes first in `config.json` and is currently reachable and unlocked
   (`domains/sync_domain.py`'s `representative()`) — and writes to it, and only it.
4. The write isn't just "fire and forget" — the hub waits for the phone to send back a real
   confirmation (`ACK set`) before considering it delivered (§6.3). If that never comes, the
   write is treated as failed and queued for retry, not silently assumed to have worked.
5. A couple of seconds later, Samsung Continuity mirrors that same content onto the *other*
   Samsung device on its own. CopyClip does not write to it a second time — it recognizes
   the mirrored copy (same content hash) and does nothing further, which is also how it
   confirms Continuity is still healthy (§6.5).

### Phone copies something (symmetric for the tablet)

1. `ClipBridge` on the phone polls the clipboard roughly 4 times a second (every 250ms) by
   checking only its *timestamp* — which is free and raises no notification — and only
   reads the actual content when the timestamp changes.
2. If the phone is the domain's current representative, it reads the content and reports it
   (`DESC`, then the laptop asks for the body with `GET` only if it doesn't already
   recognize the hash — this is what stops a single copy from crossing the wire twice when
   Samsung mirrors it).
3. If the phone is the *standby* member (tablet is representative right now), it only
   announces a bare timestamp (`STAMP`), never reading content — because Samsung will mirror
   whatever the representative already read, so there's nothing new to contribute, and
   reading here would just double the toasts for no benefit.
4. The hub applies it to the laptop's clipboard and, separately, does *not* write it back
   into the Samsung domain it came from — that would just be an unwanted echo.

### What happens when the phone is locked

The write is accepted by ClipBridge, detected as going to a locked device
(`ITrustManager.isDeviceLocked`), and parked rather than lost. It replays automatically the
moment the phone unlocks. Meanwhile, if the tablet is unlocked, the write goes there instead
and Samsung Continuity carries it to the phone once it wakes up — a locked phone doesn't
block the copy from happening at all, it's just not the one that gets written to directly.

### What happens when the phone is unreachable (offline, Tailscale off, etc.)

This is where today's session's bug fixes matter most — see §6 for the full detail. In
short: the hub now genuinely confirms a write reached the device before believing it
succeeded, so an unreachable phone gets correctly detected and queued for retry instead of
being falsely marked "delivered" and later either lost or delivered stale.

---

## 4. The moving parts

| Module | Responsibility |
| :--- | :--- |
| [copyclip.py](copyclip.py) | Entry point: loads config, wires nodes/domains/transports into a `Hub`, sets up logging, starts everything. |
| [core/hub.py](core/hub.py) | The router. The *only* place that decides where a clip goes. Knows nothing about ADB, ctypes, or any specific transport — only the abstract `ClipboardNode` interface. |
| [core/dedup.py](core/dedup.py) | `RecentClips`: tracks which nodes already hold which content hash, with a TTL. This is what stops Samsung's own mirroring from looping content back and forth forever. |
| [core/outbox.py](core/outbox.py) | Durable, single-slot-per-key retry queue for writes that couldn't be delivered yet. Persists to `state/outbox.json` so a daemon restart doesn't lose a pending write. |
| [core/policy.py](core/policy.py) | Size cap, denylist regex, and sensitive-clip filtering (skips anything a source app flagged with Android's `EXTRA_IS_SENSITIVE`, e.g. a password manager). |
| [core/models.py](core/models.py) | `ClipPayload`/`ClipDescriptor` — the shared data shapes; `WriteResult` — the outcome enum every node's `write()` returns. |
| [nodes/base.py](nodes/base.py) | `ClipboardNode`, the abstract contract the hub actually depends on. |
| [nodes/windows_node.py](nodes/windows_node.py) | The laptop as a node. Thin wrapper over [winclip.py](winclip.py) (raw ctypes, no pywin32) and [winimage.py](winimage.py) (image codec, §6.6a). Also owns the optional local screenshot-backup save (§8, `screenshot_backup_dir`). |
| [winimage.py](winimage.py) | PNG encode (hand-rolled, deterministic), WIC-backed decode (arbitrary formats), DIB↔PNG conversion. No third-party deps. |
| [nodes/android_node.py](nodes/android_node.py) | A phone or tablet as a node. Owns its own connect/reconnect supervisor and speaks the ClipBridge wire protocol (§5). |
| [domains/sync_domain.py](domains/sync_domain.py) | `SyncDomain` — the phone+tablet grouping. Elects a representative, tracks whether Samsung Continuity is actually propagating (healthy vs degraded), and decides who gets written to for one clip. |
| [transports/adb_transport.py](transports/adb_transport.py) | Tier 1: a persistent `adb shell` pipe. Works with zero phone-side setup; doesn't survive a phone reboot (wireless ADB isn't `persist`ed). |
| [transports/tcp_transport.py](transports/tcp_transport.py) | Tier 2: Termux + Shizuku (`rish`), for when reboot survival matters. |
| [android/ClipBridge.java](android/ClipBridge.java) | The on-device helper. Source is tracked; the compiled `.dex` is gitignored and rebuilt with `android/build_dex.ps1`. |

---

## 5. Wire protocol reference

One line per frame, `\n`-terminated, base64 for any payload. Defined authoritatively in
`ClipBridge.java`'s own top-of-file doc comment; handled on the Python side in
`AndroidNode._handle()`.

| Direction | Verb | Meaning |
| :--- | :--- | :--- |
| laptop → device | `SET <base64>` | Apply this content to the device's clipboard. |
| laptop → device | `GET` | Send the current clip's full content. |
| laptop → device | `DESC` | Send metadata only (mime, sensitive flag, timestamp, hash, length) — never the body. |
| laptop → device | `MODE stamp` / `MODE full` | Standby (announce timestamps only, never read content) vs representative (read content to produce hashes). |
| laptop → device | `PING` | Liveness probe; answered with `PONG`. |
| laptop → device | `QUIT` | Clean shutdown. |
| device → laptop | `READY <sdk>` | Greeting once the helper has a working binder handle. |
| device → laptop | `CLIP <mime> <sens> <b64>` | Full clip content, in response to `GET` or when in full mode and something genuinely new appeared. |
| device → laptop | `DESC <mime> <sens> <ts> <sha> <len>` | Metadata-only announcement. |
| device → laptop | `STAMP <ts>` | "Something changed" with no content — used in standby mode, and by the own-write-echo suppression. |
| device → laptop | `ACK <verb>` | Confirms a `SET` or `MODE` actually completed. |
| device → laptop | `DEFER <verb> <reason>` | The command was accepted but can't apply right now (device locked) — will replay itself. |
| device → laptop | `LOCK <0\|1>` | Lock-state change. |
| device → laptop | `SKIP <mime> <why>` | Something was deliberately not read/sent. |
| device → laptop | `PONG` | Answers `PING`. |
| device → laptop | `ERR <message>` | Something went wrong on the device side. |
| laptop → device | `SETB <id> <mime> <sha256> <bytes> <nchunks>` | Header for a chunked image write. |
| laptop → device | `SETC <id> <idx> <b64chunk>` | One chunk of an image write. |
| device → laptop | `CLIPB <id> <mime> <sens> <sha256> <bytes> <nchunks>` | Header for a chunked image announcement (in response to `GET`). |
| device → laptop | `CLIPC <id> <idx> <b64chunk>` | One chunk of an image body. |
| device → laptop | `ACK setc <id> <idx>` | Periodic liveness ping during a chunked write, distinct from the transfer's final `ACK set`. |
| device → laptop | `ERR chunk <id> <reason>` | The device aborted a chunked write it was receiving (hash mismatch, oversize, gap). |

Images use `SETB`/`SETC`/`CLIPB`/`CLIPC` instead of the single-line `SET`/`CLIP` verbs because
a multi-megabyte payload as one line would block the pump/reader thread and starve
`PING`/`LOCK` frames — see §6.7a. Text's `SET`/`CLIP`/`GET`/`DESC` framing above is completely
unmodified by any of this; `GET` is reused unchanged, and the device decides whether to
answer with `CLIP` (text) or `CLIPB`+`CLIPC`×N (image).

---

## 6. Reliability mechanisms, and why each one exists

### 6.1 Device-lock queuing

Covered in §2/§3. The point worth repeating here: this was found by *measuring* actual
device behavior, not by reading documentation — Android does not raise an exception when a
locked-device write is discarded. Trusting the absence of an error is exactly how a clip
gets silently lost. This principle — verify a write actually happened rather than assuming
success from the absence of an error — turned out to be the same lesson relearned twice more
this session (§6.3, §6.4).

### 6.2 Why a repeated content hash doesn't mean "skip it"

Originally, if the exact same content had been seen before (e.g. you copy the same word
twice), the hub treated the second occurrence as an "echo" of the first and did nothing.
That's correct when it really is an echo (Samsung mirroring a clip CopyClip already knows
about) — but it's wrong when it's a *deliberate* second copy, and a node genuinely still
lacks that content because the first delivery to it silently failed. The fix: the hub always
proceeds to routing, and lets the existing per-node delivery cache (`dedup.should_deliver`)
decide who still needs it — which was always correct and already tested, it just wasn't
being consulted early enough.

### 6.3 ACK-confirmed writes

`AndroidNode.write()` used to declare success the moment a local pipe write to the `adb
shell` process didn't raise an exception — which tells you the write reached your *own*
laptop's `adb` client, not that it reached the phone. If the phone had just gone
unreachable, that local write could still "succeed" while the actual bytes went nowhere,
and the hub would wrongly mark the phone as having received content it never got.

The fix: `write()` now sends `SET` and waits (up to `ack_timeout_s`, default 4 seconds — see
§7) for the device's real `ACK set` or `DEFER set ...` reply, which the protocol already
provided (§5) but nothing was listening for. No reply within the timeout means the channel
is genuinely dead: the write is reported as failed, and the transport is proactively marked
dead so the reconnect supervisor notices within about a second, instead of waiting on a much
slower passive 90-second idle-ping fallback.

### 6.4 "Already delivered" is not the same as "failed"

`_deliver_to_node()` used to return the same `False` for two different situations: a
genuine write failure, and "the target already has this exact content, there's nothing to
do." Callers couldn't tell them apart. In practice this meant: once a queued retry became
satisfied by some other path (say, a second identical copy succeeded directly while an
older queued retry for the same content was still pending), the retry could never register
that as success. It just kept re-queuing itself — with its own log line and a disk save —
every couple of seconds, for up to 90 seconds (until the underlying delivery record's TTL
expired), at which point a long-stale write could suddenly fire and land on the phone with
no corresponding fresh copy behind it — a "phantom" delivery, minutes after the fact.

The fix distinguishes the two outcomes properly, and specifically avoids re-arming the
Continuity health probe (§6.5) for the "already satisfied" case — doing so would otherwise
make a healthy domain look falsely degraded, since the probe would keep timing out waiting
for a re-confirmation nobody has any reason to send again.

### 6.5 Continuity health tracking

After a representative write, the hub arms a short-lived "probe": if the *other* domain
member reports the same content hash within `grace_s` (8 seconds — comfortably above
Samsung's measured ~2.5s relay time), that's proof Continuity is actually propagating right
now, and CopyClip keeps writing to just the one representative. If that confirmation is
missed `fail_threshold` (3) times in a row, the domain is marked **degraded**, and CopyClip
falls back to writing every reachable member directly — because at that point nothing is
relaying on its behalf. It recovers back to "healthy" the moment a confirmation succeeds
again.

### 6.6a Image clipboard: MediaStore under app_process, and why it needed FakeContext

Writing an image to the Android clipboard needs a real `content://` URI backing a
`ClipData.Item` — a `file://` URI throws `FileUriExposedException`, and minting a
`content://` URI needs a working `ContentResolver`. Under `app_process` (no installed app,
no manifest, no `Context` in the normal sense), that resolver does not exist by default.

The fix, ported from `otherCopyProjects/scrcpy`'s `FakeContext`/`Workarounds` classes and
scoped **narrowly** to this one purpose: a hand-built `ActivityThread` gives access to the
system `Context`, and a `ContextWrapper` subclass overrides `getContentResolver()` to route
provider access through `ActivityManager.getContentProviderExternal(...)` — the same binder
call Shizuku's own shell-UID service uses. Text's `setPrimaryClip`/`getPrimaryClip` are
**not** touched by any of this; they stay on the proven reflective `IClipboard` binder path
that was already working. If `FakeContext` fails to initialize for any reason, imaging is
disabled and text sync is completely unaffected — see `ClipBridge.java`'s `initImaging()`.

This was measured, not assumed, before being built: see
[bugs/SPIKE-IMAGE-CLIPBOARD.md](bugs/SPIKE-IMAGE-CLIPBOARD.md) for the device spike that
proved the mechanism on the real S24 Ultra, including two real bugs found in a naive port
of scrcpy's code (a missing `Looper.prepareMainLooper()`, and `acquireUnstableProvider`
needing to actually delegate rather than return `null`, which scrcpy itself never needed
because it never does file I/O through this resolver). Both fixes are in `ClipBridge.java`.

**Format handling:** PNG, JPEG, GIF, BMP and WEBP travel byte-for-byte, original encoding
preserved — no re-encode, no bloat on a photo. Only HEIC/HEIF/AVIF/unknown formats are
transcoded to PNG (via `BitmapFactory`/`Bitmap.compress` on the device, or WIC on the
laptop), because Windows cannot reliably decode those directly.

**Storage:** images write to `Pictures/CopyClip` via `MediaStore.Images.Media.insert`, with
history kept (no auto-delete). Any app with `READ_MEDIA_IMAGES` can paste the result —
maximum compatibility, at the cost of the album being visible in Gallery.

**Windows side:** `winimage.py` hand-rolls a deterministic PNG encoder (fixed zlib level,
filter type 0 per scanline — determinism is load-bearing for own-write echo suppression, not
a style choice) and decodes arbitrary formats via WIC (Windows Imaging Component) over raw
COM/ctypes, with every GUID and struct layout verified against the installed Windows SDK
headers rather than recalled from memory. `winclip.py` sets `CF_DIBV5` (alpha preserved, via
an explicit `BI_BITFIELDS` alpha mask — the ClipCascade prior-art bug of destroying alpha
with `.convert("RGB")` is deliberately not repeated) alongside the original bytes under a
registered format (`"PNG"`/`"JFIF"`/`"GIF"`) when one applies, in one clipboard-ownership
cycle, so both the pass-through-preferring apps and the DIB-only apps can paste it.

**What's still open:** reading a clip URI minted by a *different* app's content provider
(not CopyClip's own `MediaStore` row), and a live visual paste confirmation into arbitrary
third-party apps, were not verified during the spike — see
[bugs/SPIKE-IMAGE-CLIPBOARD.md](bugs/SPIKE-IMAGE-CLIPBOARD.md)'s Q5/Q7. `allow_images`
defaults to `false` in `config.json` pending real-use confirmation, matching this project's
existing convention for BUG-001 through BUG-003.

### 6.7a Chunked framing for images

A 24 MiB image is roughly 32 MiB of base64 — far too large for the single-line `SET`/`CLIP`
framing text uses, which would block the pump/reader thread for the whole transfer and
starve `PING`/`LOCK` frames. Images use `SETB`+`SETC`×N (laptop → device) and
`CLIPB`+`CLIPC`×N (device → laptop) instead (§5), with the device ACKing every 8 chunks
(`ACK setc <id> <idx>`) so `ack_timeout_s`'s "no answer in N seconds means the channel is
dead" guarantee (§6.3) stays meaningful at any payload size — without this, a channel that
died halfway through a multi-megabyte transfer would only be noticed after the whole
(pointless) send finished, not within the normal detection window.

Chunking operates on the **base64 string**, not the raw bytes — ASCII by construction, so
it is structurally immune to the mid-codepoint UTF-8 corruption bug found in ClipCascade's
own chunker during prior-art research (fixed-offset byte slicing decoded with
`errors="ignore"`, silently dropping partial multi-byte characters). The reassembled bytes
are verified against the header's `sha256` before anything downstream ever sees the payload
— a gap, a dropped chunk, or corruption fails closed rather than applying a truncated image.

The outbox (§6.4) cannot inline a queued image the way it does text: a queued 24 MiB image
base64'd into JSON would mean a ~32 MiB `fsync` on every retry tick. Image payloads instead
write a single-file-per-key sidecar (`state/outbox/<key>.bin`, overwritten in place) with
only `{mime, sha256, sidecar: true}` in `outbox.json`; on load, a missing or
hash-mismatched sidecar drops that entry rather than serving stale or corrupt bytes. Text
payloads are completely unaffected — still fully inline, exactly as before.

### 6.6 ClipBridge idle-orphan safeguard

A subtler gap, found by testing: when Tailscale drops on the phone, `ClipBridge` doesn't
know its connection to the laptop is dead until the OS actually closes its stdin pipe —
which lags behind the network actually going down. During that gap, the orphaned process
keeps polling and, if it was in full-read mode, keeps reading content on any clipboard
change (including one Samsung Continuity delivers locally, with nobody able to receive the
result), raising a real but pointless "Shell pasted from your clipboard" toast.

The fix: `ClipBridge` now tracks the last time it heard anything from the laptop, and once
more than `IDLE_ORPHAN_MS` (100 seconds — see §7 for why) has passed with no contact, it
treats itself as orphaned and suppresses content reads exactly like standby mode, until
contact resumes. **Known, accepted limit:** this only helps a phone that's been disconnected
for minutes. It does *not* prevent the toast from a quick few-second Tailscale toggle,
because a safe threshold (long enough not to false-trigger during completely normal quiet
periods between copies) is necessarily longer than that gap. Closing that narrower window
would need the OS to notice the network loss faster, which isn't something this process
controls.

### 6.6b Screenshot-vs-clipboard-image race suppression

A companion app installed alongside CopyClip (Sefirah) turned out to place a small preview
image on the real Android clipboard around the moment a screenshot is taken — independent of
any of its own "sync to PC" toggles, most likely a side effect of its notification listener
reacting to Android's own "Screenshot captured" system notification via its clipboard
accessibility service (see `bugs/BUG-006` for the full investigation). `checkScreenshots()`
(§6.6a's sibling, the file-based watcher) correctly pushes the real screenshot file; the
ordinary clipboard-image path (`emitDesc()`/`emitImageDesc()`) — needed for real, deliberate
image copies and otherwise working exactly as designed — has no way to tell this preview
apart from a genuine copy, and forwarded it too, producing two device-synced images (and,
via `screenshot_backup_dir`, two backup files) for one real capture.

The two never share a content hash, so neither the existing per-node dedup nor a
laptop-side size comparison can safely tell them apart — the latter was considered and
rejected because it would also collapse two genuinely different images a user deliberately
copies close together, which `tests/test_core.py::test_screenshot_backup` explicitly
protects against by design. Android is the only place that still knows which code path an
image came from, so the fix lives entirely in `ClipBridge.java`: `emitScreenshot()` stamps
`lastScreenshotEmitMs` right before actually sending the real file; the ordinary
clipboard-image path checks that stamp first (catching the "real file, then spurious
preview" ordering with no delay) and otherwise holds the candidate briefly
(`pendingImageDesc`/`pendingImageDescSinceMs`, resolved every poll tick) so a real screenshot
that follows within `IMAGE_DESC_HOLD_MS` can still supersede it (catching the "spurious
preview, then real file" ordering — the more commonly observed one). **Known, accepted
trade-off:** two genuinely different images copied deliberately less than
`IMAGE_DESC_HOLD_MS` apart on the same device, with no screenshot involved at all, would have
the first one silently superseded. This is a defense-in-depth layer, not the real fix — the
actual root cause is Sefirah's clipboard accessibility service, and disabling that
permission (Settings → Accessibility → Sefirah) removes the problem at the source with no
trade-off at all.

### 6.7 Reconnect backoff

Each device's `AndroidNode` supervises its own connection independently (one device
rebooting or dropping never affects the other). On failure it retries with **exponential
backoff** — 1s, 2s, 4s, 8s, 16s, then capped at 30s forever — rather than hammering a dead
connection every second. This is deliberate and pre-dates today's session: cheap retries are
fine, but pointless when the target has been gone for a while. The practical consequence: if
you toggle Tailscale back on, you may wait anywhere up to the current backoff interval plus
however long Tailscale itself takes to re-establish its tunnel — commonly under a minute,
sometimes a bit over. This is a real, known, and currently accepted tradeoff, not a bug.

---

## 7. Tuning knobs

All in [config.json](config.json) unless noted. Every value here was chosen for a specific
reason — changing one is a real tradeoff, not a free lunch.

| Key | Default | Controls | Tradeoff of changing it |
| :--- | :---: | :--- | :--- |
| `ack_timeout_s` | 4.0 | How long `write()` waits for a real `ACK`/`DEFER` before declaring a write failed (§6.3) | Lower = faster failure detection but more false positives on a briefly slow network; higher = more tolerant but slower to notice a genuinely dead phone |
| `reconnect_min_s` | 1.0 | First retry delay after a connection drops | Rarely worth changing |
| `reconnect_max_s` | 30.0 | Ceiling on the exponential reconnect backoff (§6.7) | Lower = faster reconnect after the device comes back, at the cost of more frequent (cheap) failed attempts during a real outage |
| `connect_timeout` | 15.0 (hardcoded in `transports/adb_transport.py`, not in config.json) | How long a single `adb connect`/push attempt is allowed before failing | Rarely worth changing |
| `idle_ping_s` | 60.0 | How long a channel can be silent before the hub actively pings it to check it's alive | This is also, indirectly, why `IDLE_ORPHAN_MS` is set to 100s — it must exceed this plus its ~30s grace period |
| `dedup_ttl_s` | 90.0 | How long a delivered-content record is remembered per node | Longer than Samsung's ~2.5s relay by a wide margin; short enough that deliberately re-copying the same text later still syncs |
| `tick_s` | 2.0 (hardcoded in `core/hub.py`) | How often the hub retries the outbox and re-checks domain health | Rarely worth changing |
| `IDLE_ORPHAN_MS` | 100,000 (hardcoded in `android/ClipBridge.java`) | How long `ClipBridge` can go without hearing from the laptop before suppressing content reads (§6.6) | Must stay comfortably above `idle_ping_s` + its grace window or a healthy-but-quiet connection will falsely trip it |
| `IMAGE_DESC_HOLD_MS` | 2,500 (hardcoded in `android/ClipBridge.java`) | How long an ordinary clipboard-sourced image is held before being announced, so a real screenshot file push (in either order) can supersede it (§6.6b, `bugs/BUG-006`) | Lower = less delay on a genuine standalone image copy, but less time for a slow screenshot-folder poll tick to catch up; higher = safer against the race, more delay, and a bigger window where two genuinely distinct rapid image copies could collapse into one |
| `grace_s` (per domain) | 8.0 | How long a Continuity confirmation probe is given before counting as a miss (§6.5) | Above Samsung's measured ~2.5s relay by a wide margin |
| `fail_threshold` (per domain) | 3 | Consecutive missed confirmations before a domain is marked degraded (§6.5) | Lower = faster fallback to direct writes, more sensitive to one-off blips |
| `allow_images` | `false` | Whether image clips sync at all (§6.6a) | Defaults off pending real-use confirmation of bugs/SPIKE-IMAGE-CLIPBOARD.md's open Q5/Q7; flip once confirmed |
| `max_image_bytes` | 25,165,824 (24 MiB) | Oversize images are **skipped and logged**, never truncated — enforced independently on the laptop (`core/policy.py`) and on each device (`ClipBridge.java`'s own `MAX_IMAGE_BYTES`) | Higher = larger screenshots sync, more memory pressure per transfer on both ends |
| `image_chunk_bytes` | 49,152 (48 KiB) | Raw bytes per chunk in the `SETB`/`SETC` framing (§6.7a) | Smaller = more `ACK setc` round trips, faster stall detection on a shaky link; larger = fewer round trips, coarser stall detection |
| `screenshot_backup_dir` | `""` (off) | Local folder every image clip also gets saved into as a plain file (§8, "Two entries in Windows Clipboard History") | Empty disables it entirely; a stopgap for a Windows bug, meant to be removed once Microsoft fixes it |
| `screenshot_backup_include_devices` | `true` | Whether `screenshot_backup_dir` also covers images synced in *from* the phone/tablet, not just laptop-native ones | `false` limits the backup to laptop screenshots only |
| `screenshot_exclude_from_history` | `true` | Whether a device-synced image is written with `CanIncludeInClipboardHistory=0`, excluding it from Windows Clipboard History/Win+V (§8, "Two entries in Windows Clipboard History") | `false` lets synced screenshots appear in Win+V again, at the cost of the eviction-of-other-entries issue that motivated this flag in the first place (`bugs/BUG-005`) |

---

## 8. Lessons learned this session

Condensed postmortems, each as symptom → root cause → fix. Earlier incidents
(toast storm from Link-to-Windows/Phone Link, `EmptyClipboard err=1418` data loss, duplicate
daemon instances) are documented in full in [bugs/](bugs/README.md) and not repeated here.

**Repeat copies silently dropped.** *Symptom:* copying the exact same text twice never
reached a device that missed the first delivery. *Cause:* the hub bailed out of routing
entirely whenever a content hash had been seen before, without checking whether any
individual node actually still lacked it. *Fix:* §6.2.

**Fire-and-forget writes.** *Symptom:* a phone disconnected mid-copy could get marked as
having received content it never got, permanently blocking any retry to it for that
content. *Cause:* `write()` trusted a successful local pipe write as proof of delivery.
*Fix:* §6.3.

**"Already delivered" treated as "failed."** *Symptom:* the phone occasionally showed a
"copied" toast minutes after the fact, with no real corresponding laptop copy; separately,
phone→laptop sync sometimes felt delayed. *Cause:* found by reading real production logs
(`outbox[domain:samsung] holding <hash> (target unavailable)` repeating every ~2 seconds for
5+ minutes) — a satisfied outbox entry could never register as done, so it kept re-queuing
until its dedup record's TTL expired and a stale write finally fired. *Fix:* §6.4, plus a
narrowed outbox lock (per-entry instead of for the whole retry loop) so one stuck entry
can't delay a genuine concurrent copy, and `Outbox.put()` now skips the disk write and log
line entirely when re-arming identical, unchanged content — closing the performance/log-noise
angle of the same bug.

**ClipBridge orphan window.** *Symptom:* an extra "Shell pasted from your clipboard" toast
right after disabling Tailscale on the phone. *Cause & fix:* §6.6.

**Two entries in Windows Clipboard History (Win+V) per Win+Shift+S screenshot — ruled out
as a CopyClip cause, not fixed here.** *Symptom:* a single Snipping Tool screenshot
("copy to clipboard" enabled) appears twice in Win+V history. *Investigated because:*
`core/hub.py`'s `_route()` (§ "Anatomy of a copy") never writes a clip back to the node it
originated from, so CopyClip has no code path that could write a laptop-originated
screenshot back onto the laptop's own clipboard a second time — but per this project's
measure-before-concluding rule, that was confirmed rather than assumed. *Measured:*
reproduced with CopyClip running under `--log-level DEBUG` (only one `laptop -> phone`
delivery logged for the screenshot's content hash, no duplicate), then reproduced again
with CopyClip **completely stopped** — Win+V still showed two entries with no CopyClip
process running at all. *Confirmed root cause:* a known Windows 11 25H2 regression (this
machine is on build 26200, matching community reports exactly) — with Snipping Tool's
**"Automatically save original screenshots" and "Automatically copy changes to clipboard"
both on**, each independently triggers its own clipboard write for the same capture. Not a
CopyClip issue, not fixed in this repo. *Windows-side fix (confirmed working on this
machine):* Snipping Tool's own Settings → turn off "Automatically save original
screenshots", leave "Automatically copy changes to clipboard" on (CopyClip needs that one).
No known way to keep both Snipping Tool settings on without the duplicate; worth checking
for a Snipping Tool Store update periodically in case Microsoft patches it. *Follow-up:*
turning off Windows' own auto-save loses the file it used to drop in
`Pictures\Screenshots\Temporary Screenshots`, so `config.json`'s `screenshot_backup_dir`
(§7) now has CopyClip replicate that save itself — for laptop-native screenshots and, via
`screenshot_backup_include_devices`, ones synced in from the phone/tablet too. Remove that
config once Microsoft fixes the underlying bug and Windows' own setting is safe to re-enable.
*Second follow-up:* the local backup feature itself surfaced a real Android-side bug (a
screenshot re-touched in place by a background process got re-pushed as if new — full
writeup in `bugs/BUG-005-duplicate-screenshot-push.md`) plus a related Windows Clipboard
History symptom, large device-synced images evicting the user's other Win+V entries.
Neither is a CopyClip-caused clearing bug (confirmed: no code in this repo calls any
clipboard-history API), but `config.json`'s `screenshot_exclude_from_history` (§7) now
excludes device-synced images from History entirely as the only available fix, trading
away that image's own Win+V visibility for not disturbing the rest of the user's history —
see `bugs/BUG-005` for the full investigation, including why no way to have both exists.

**(2026-08-22) A companion app's clipboard accessibility service caused a second,
spurious backup file per phone screenshot.** *Symptom:* one phone screenshot produced two
files in `screenshot_backup_dir` — a small one and the real, much larger one, 0-2s apart in
either order. *Investigated because:* the user noticed it during real use and asked for a
live, device-measured investigation rather than a guess, per this project's own rule.
*Measured:* `adb shell ls` on `/sdcard/DCIM/Screenshots` showed only one real file per
capture, matching the *large* half of each pair byte-for-byte; the small half had no
on-disk file at all, so it never went through `checkScreenshots()`. `adb logcat` ruled out
Samsung's own screenshot-editor app explicitly setting the clipboard (its clipboard-set
signature never named it, and never fired near the small image's timestamp), then
`adb shell dumpsys package com.castle.sefirah` found the actual mechanism: Sefirah (a
companion app installed five days earlier, after this file's own "screenshots don't touch
setPrimaryClip" measurement was written) declares its clipboard listener as an
`AccessibilityService` — one of the caller types Android exempts from the normal
background-clipboard restriction — independent of its own "sync to PC" toggles, which the
user had already turned off. *Root cause:* strongly indicated, not caught red-handed (an
accessibility service's internal calls don't surface distinctly in logcat) — most likely
Sefirah's notification listener reacting to Android's own "Screenshot captured" system
notification (which carries a small preview bitmap) and placing that preview on the real
clipboard via its accessibility service. *Fix:* §6.6b — a defense-in-depth timing
coordination in `ClipBridge.java` between the file watcher and the ordinary clipboard-image
path, since the real fix (disabling Sefirah's Accessibility Service permission) is a device
setting, not something this codebase can apply. Full writeup:
`bugs/BUG-006-screenshot-clipboard-preview-duplicate.md`.

**(2026-08-23) Copying a link (not plain-selected text) on the phone never reached the
laptop.** *Symptom:* ordinary copied text always synced; a long URL copied via a "Copy
link"-style action never appeared on the Windows clipboard, with nothing visibly wrong on
either end. *Investigated because:* the user asked for a measured explanation rather than a
guess, per this project's own rule. *Measured:* `logs/copyclip.log` at the production
`INFO` level already contained the answer: `skip clip from phone: non-text
(text/x-moz-url); v1 syncs text only`, twice, bracketing the user's own successful
plain-text tests. *Root cause:* `ClipBridge.java`'s `mimeOf()` (§5) reported whichever mime
type `ClipDescription.getMimeType(0)` happened to list first, and a link copy can declare a
richer format (`text/x-moz-url`) ahead of the `text/plain` fallback it's also transmitted
under. `emitCurrentClip()` already recognized and forwarded the content correctly via
`getText()`, but labeled it with that first-listed mime, which `core/models.py`'s
`TEXT_MIMES` (only the literal strings `"text/plain"`/`"text/html"`) doesn't recognize, so
`core/policy.py` rejected perfectly good text purely over its label. *Fix:* `mimeOf()` now
prefers `text/plain`/`text/html` whenever the source declares one, using the same
`hasMimeType()` precedence `emitCurrentClip()`'s own accept/reject gate already relies on,
one function, no wire protocol or config change. Full writeup, including what remains
unconfirmed: `bugs/BUG-007-long-link-copy-rejected-as-non-text.md`.

**(2026-08-23) A whole unmerged branch, `feature/lan-fallback-tls`, turned up during
cleanup of leftover files from the same investigation.** *Context:* while reviewing loose,
untracked TLS-spike files sitting in the working tree, `git branch -a` surfaced a real,
substantial, already-tested branch from 2026-08-18 that had never been merged: a same-WiFi
TLS fallback feature (see this file's own Future Scope section) bundled with two unrelated
bug fixes from the same session. One of those, a link-copy mime mislabeling fix, was already
independently rediscovered and fixed on `main` the same day as this cleanup (BUG-007 above);
the other, a High-severity daemon-restart data-loss bug, was not. *Action:* the loose,
superseded spike files were deleted (both of the branch's own spike writeups already say
their sources are safe to delete once the writeup exists, and it does); the redundant mime
fix was left as-is, since `main`'s version was already tested and confirmed; the unrelated,
still-relevant restart fix was extracted and landed on `main` on its own, renumbered
BUG-008 to avoid colliding with numbers `main` had already assigned elsewhere in the
meantime. The TLS feature itself stays on its branch, untouched, its own commit message
lists real open reliability gaps that make it not yet ready regardless.

**(2026-08-23) An AHK-triggered daemon restart was silently dropping the next clip as an
unsynced baseline, unfixed on `main` until brought over from the branch above.** *Symptom:*
phone/tablet -> laptop sync occasionally "just doesn't work" right after a restart, no
error anywhere. *Root cause:* `core/hub.py`'s baseline-adoption tracking was purely
in-memory, so any fresh process, including a spurious restart of an already-healthy daemon,
had no way to tell "first ever connection" apart from "just restarted, and the device's
clip genuinely changed while it was down" and silently treated both the same way: adopt,
don't apply. Originally measured across three real daemon restarts in one boot
(2026-08-14). Independently reproduced during this session's own restarts while deploying
the BUG-007 fix above (`baseline from phone (37064 chars), not applied`), harmless that
time only because the dropped content happened to be an echo of what the laptop had itself
just written. *Fix:* a small durable `core/baseline_store.py` (mirrors `core/outbox.py`'s
atomic-write pattern) persists the last-seen hash per device across process restarts, so a
genuine content change during downtime is applied instead of dropped; a companion AHK-side
guard (outside this repo, confirmed already present) skips the restart entirely when a
single healthy instance is already running. Full writeup:
`bugs/BUG-008-restart-drops-baseline.md`.

---

## 9. Security & trust model

Written up after a direct question: since this carries sensitive information daily, can a
third party on the network read or inject clipboard content in transit? Short answer: not
without either being a member of your own Tailscale tailnet or already holding an
authorized adb key for one of your Samsung devices — but that protection comes entirely
from Tailscale and adb, not from anything CopyClip's own wire protocol implements.

**What actually protects the data:**

- The laptop only ever dials *out*. Neither transport (`transports/adb_transport.py`,
  `transports/tcp_transport.py`) ever binds or listens on a socket — there is no code path
  by which an unsolicited connection reaches the laptop process at all.
- Devices are addressed by fixed Tailscale IP (`config.json`'s `100.x.x.x` CGNAT-range
  addresses), and all traffic to those addresses is carried inside Tailscale's WireGuard
  tunnel. A device that isn't a member of your tailnet normally can't route packets to that
  address space at all, so it sees nothing — not even encrypted packets to sniff.
- `adb`'s own RSA-key trust model gates who can open a shell on either Samsung device in the
  first place (a connecting host's key must already be authorized, or the user must tap
  "Allow" on the unlocked device screen). This is what actually stops an arbitrary tailnet
  peer from running `ClipBridge` even if it could route to the device.
- `core/hub.py`'s domain/representative-election logic is entirely config-driven from the
  two hardcoded `devices[]` entries in `config.json` — there's no runtime path for an
  unlisted device to be treated as `phone` or `tab`.

**What CopyClip does *not* add on top of that:**

- The transport actually configured today, `transports/adb_transport.py` (Tier 1), sends
  plain UTF-8 `SET`/`GET`/`DESC`/... lines with **no encryption and no authentication of its
  own** — `android/ClipBridge.java`'s `serve()` executes any well-formed verb from stdin with
  no credential check. It fully trusts the adb pipe it's handed. Payloads are base64-encoded
  for safe transport, not for confidentiality — base64 is trivially reversible.
- A second, more hardened transport already exists in the repo —
  `transports/tcp_transport.py` + `transports/framing.py` (Tier 2), built for exactly this
  concern — with real HMAC-SHA256 mutual authentication and replay protection. Its own
  docstring is explicit about scope: it authorizes *within* the tailnet (stops another
  tailnet peer from impersonating your phone/tablet); it is not encryption, since Tailscale
  already provides that. It is not currently active — `config.json` has both devices on
  `"transport": "adb"`.
- Practical consequence: anyone who is already inside your Tailscale tailnet, or who already
  holds an authorized adb key for either Samsung device, has full read/write access to
  whatever CopyClip is syncing. CopyClip does not add its own second lock on that door.

**A caveat worth knowing, not a CopyClip issue:** `adb tcpip 5555` makes each phone's `adbd`
listen on TCP 5555 on *all* network interfaces, not just the Tailscale one — technically
reachable from any device on the same Wi-Fi, bypassing Tailscale's routing restriction
entirely. adb's key-authorization step still gates any actual shell access from there, but
this is an Android/adb-level exposure CopyClip depends on and doesn't verify or harden.

**Bottom line:** on a home network, a device that is neither a tailnet member nor already
adb-authorized to your phone/tablet cannot read or inject your clips — but if you ever add a
device to the tailnet, or authorize a new adb key, that device inherits full clipboard
access with zero further gate from CopyClip itself.

---

## 10. Logging & debugging

- Logs live in `logs/copyclip.log` (rotated at 2 MB, 3 backups kept — bounded disk use), set
  up in `copyclip.py`'s `setup_logging()`. Both `logs/` and `state/` are gitignored — nothing
  from either is ever committed.
- Level is controlled by `config.json`'s `log_level` (default `INFO` — meant for the
  production instance) or overridden per run with `--log-level DEBUG`.
- **The production instance is launched by an external AutoHotkey setup, not by anything in
  this repo.** `<software>\Programming\AutoHotKey\AllScripts\BasicTasks.ahk`'s auto-execute
  section unconditionally force-restarts `copyclip.py` (via its `RestartNamedPythonServer()`
  helper: kill any existing instance — matched by name if it's a previous run of this same
  helper, or by command line for a leftover generic `pythonw.exe` — then relaunch hidden
  under a per-project renamed copy of `pythonw.exe`, e.g. `CopyClip_Python.exe`, so Task
  Manager's Name column identifies which process is which instead of showing two
  indistinguishable `pythonw.exe` rows) every time that script loads. `BasicTasks.ahk` is one
  of six scripts tracked by the loader `StartupScript.ahk`, which binds the *generic*
  Win+Ctrl+Alt+R "reload all scripts" hotkey (`#^!R::gosub ReloadAll`) — reloading closes and
  relaunches all six, CopyClip's restart is just the one with visible side effects (console
  activity), which is why it can feel like the hotkey is CopyClip-specific when it isn't.
  `core/single_instance.py`'s mutex is what makes this safe against overlapping with the
  Task Scheduler autostart (`install_autostart.ps1`) firing around the same time.
- **`Start CopyClip.bat`** — normal, quiet run (`INFO`).
- **`Start CopyClip (Debug).bat`** — same script, `--log-level DEBUG`, for a testing
  session. One codebase, no duplication; DEBUG statements use lazy `%`-formatting so they
  cost nothing when disabled.
- Useful patterns when reading a log: grep for `"holding.*target unavailable"` to spot stuck
  outbox retries (§6.4), `"no ACK/DEFER"` for a channel that timed out (§6.3), and
  `"unexpected connect error"` for genuine ADB-level connectivity trouble.

---

## 11. File map

See README's [Layout](README.md#layout) table for the quick version. The one addition worth
calling out here: **`tests/`** — `test_core.py` is pure routing/dedup/outbox logic, no
devices, runs in under a second; `test_android_node.py` covers the ACK-confirmation logic
with a fake transport, also fast; `test_e2e.py`/`test_three_device.py` need real, unlocked
hardware.

---

## 12. Future scope: designed for, not built

This section exists so a future session doesn't have to rediscover the plan from scratch —
it was already researched when the wire protocol and payload model were designed, even
though only text sync got built for v1.0.0.

### Images — built, `allow_images` currently off pending confirmation

No longer future scope. Image clipboard sync (Windows ↔ Samsung, both directions) is
implemented — see §6.6a (Android/MediaStore mechanics) and §6.7a (chunked framing, outbox
sidecar). The pre-implementation device spike and its findings are in
[bugs/SPIKE-IMAGE-CLIPBOARD.md](bugs/SPIKE-IMAGE-CLIPBOARD.md). `IMAGE_MIMES` in
`core/models.py`, `winimage.py` (new), and the `SETB`/`SETC`/`CLIPB`/`CLIPC` wire verbs
(§5) are the pieces that landed; text's existing code paths are untouched throughout.

`allow_images` defaults to `false` in `config.json` because two sub-questions from the
spike remain genuinely unverified in real use (reading a clip URI minted by a different
app's provider; live visual paste confirmation across arbitrary third-party apps) — flip it
once confirmed, matching this project's convention for BUG-001 through BUG-003.

### Same-WiFi TLS fallback for when Tailscale itself is down

Built and proven correct on real hardware, not merged: lives entirely on the
`feature/lan-fallback-tls` branch (commits `1ebb7a6`/`f15ea36`, 2026-08-18), not on `main`.

Motivated by a real incident: phone -> laptop sync stopped for hours at a time, traced to
Android's Doze mode silently killing the phone's Tailscale connection during idle, not
anything on the Windows side (a well-reasoned alternate hypothesis, the clipboard listener
going stale after sleep, was spiked and disproved first; see
[bugs/SPIKE-WINDOWS-CLIPBOARD-LISTENER.md](bugs/SPIKE-WINDOWS-CLIPBOARD-LISTENER.md) on that
branch). `transports/framing.py`'s existing HMAC-SHA256 scheme is authorization/anti-replay
only and assumes Tailscale's WireGuard tunnel supplies confidentiality; a same-WiFi fallback
has no such tunnel, so the branch wraps the Tier 2 (Termux) socket in TLS 1.2+ (stdlib `ssl`,
cert-pinned, no new dependency) on top of the unchanged HMAC layer, with `TcpTransport` trying
a device's normal address first, then an optional `lan_host`, and treating a handshake/cert
failure as a hard error rather than a silent plaintext fallback. Confirmed end-to-end on real
devices, both directions, both network states, with three real `rish`-invocation bugs found
and fixed along the way; see
[bugs/SPIKE-LAN-TLS-FALLBACK.md](bugs/SPIKE-LAN-TLS-FALLBACK.md) on that branch for the full
measured record.

**Why it's not on `main` yet, by its own commit message:** intermittent `READY` timeouts not
yet root-caused, Termux itself can get killed in the background (no Termux:API installed on
the test phone), and images/tablet/a live packet capture/unauthorized-device rejection were
never exercised over this path. Off by default even on its own branch
(`config.json` keeps both devices on `"transport": "adb"`); opt-in per device once the gaps
above are addressed. That branch's own unrelated daemon-restart-baseline-drop fix was
extracted and landed on `main` separately as [BUG-008](bugs/BUG-008-restart-drops-baseline.md);
its link-copy mime fix was superseded by the equivalent, already-shipped fix in
[BUG-007](bugs/BUG-007-long-link-copy-rejected-as-non-text.md), both discovered when this
branch turned up during a 2026-08-23 documentation pass.

### Files, via Taildrop handoff

Not researched to the same depth as images — just the shape of the idea: since Tailscale is
already the transport, its own Taildrop file-sharing feature is a plausible handoff
mechanism, rather than inventing a new file-transfer protocol inside CopyClip itself.

### Clipboard history with search

Not researched. Most likely shape: a new, optional consumer of clips already flowing through
`core/hub.py`, logging/indexing them for search — additive, not architectural, since every
clip already passes through one central place.

### More devices

No design work needed — already fully supported by the current architecture. Add an entry
to `config.json`'s `devices[]` (and to a `domains[]` group if it should mirror an existing
device via Samsung Continuity, standalone otherwise). `core/hub.py` does not change either
way — this is exactly what the hub/node abstraction in §4 was built to make trivial.

---

## 13. Where to go for more

- [README.md](README.md) — setup, config reference, troubleshooting, quick architecture
  diagram.
- [bugs/](bugs/README.md) — detailed, dated writeups of specific past incidents, with
  measured evidence, root cause, fix, and rollback instructions for each.
- [otherCopyProjects/NOTES.md](otherCopyProjects/NOTES.md) — what was adopted or rejected
  from prior art (scrcpy, ClipCascade, and others) and why.
