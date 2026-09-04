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

**A fourth device, `pixel` (a 2016 Google Pixel), is supported but disabled by default**
(`config.json`'s `"enabled": false`) — standalone, no Samsung Continuity partner, would
behave exactly like the laptop if turned on. Off because it's used only 1-2 days a month and
has no non-root way to keep wireless adb alive across its own reboots (Android's persistent
Wireless Debugging pairing doesn't exist before Android 11; this device's final OS version is
Android 10) — see `config.json`'s `_pixel` note and [BUG-009](bugs/BUG-009-pixel-clipbridge-printstream-crash.md).

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
either Samsung device" — was true when this section was written.

**Update (2026-08-31):** an installed app (`android_app/`, package
`com.mohitarora.copyclip`) was built this session, and briefly ran on phone and tab too
(hosting this same `clipbridge.dex` from a Shizuku `UserService` instead of an `adb` shell
session, same shell-UID mechanism underneath, same dex, same protocol, just started by
Shizuku's own privileged API). It was reverted off phone/tab the same day: the reboot-
survival concern that motivated it turned out to already be solved by plain `adb` once
Wireless Debugging has been paired with the network once (confirmed by a real phone reboot,
see the Troubleshooting entry in README), so the app added a shared key, a port, and a
foreground service for zero actual capability gain on those two devices. Both are back to
`"transport": "adb"` in `config.json`, and the app is uninstalled from both, see the "App
walked back off phone/tab" entry in §8 for the full reasoning. The laptop side's own change
stays regardless of any of this: `windows_app/tray.py` replaces running `copyclip.py` as a
bare script, a `Shell_NotifyIconW` tray icon plus an optional tkinter dashboard wrapped
around the exact same `core/hub.py` daemon, `build_hub()` unchanged (see §4), and it applies
equally whether a given device is on plain adb or the app tier.

**Update (2026-09-01):** `pixel` is now `"enabled": true` on plain `"transport": "adb"` in
`config.json`, the exact same zero-app Tier 1 phone/tab use, confirmed live both directions.
Its Android version predates persistent Wireless Debugging pairing (Android 11+), so unlike
phone/tab it cannot get reboot survival from this alone, a real platform wall, not a config
choice, so it needs `adb -d tcpip 5555` re-armed over USB after every cold start (this
machine has a double-click convenience script for that at
`<software>\Phone\Pixel\rearm_wireless_adb.bat`, personal tooling, not part of this repo).
Until re-armed, this device's entry just sits unreachable and the daemon retries with its
normal backoff, harmless, same as any other device being briefly offline. **While
unreachable, this also explains what actually keeps working and what doesn't**, confirmed
live 2026-09-01: an S24-to-pixel copy still arrives, but not via this Tier 1 link at all,
it's really S24-to-laptop (CopyClip's own `phone -> laptop` path, unaffected by pixel's own
state) and then laptop-to-pixel as a *second*, independent hop that only Sefirah completes
while Tier 1 is down (CopyClip's own attempt at that second hop queues in the outbox
instead, `outbox[node:pixel] holding ... target unavailable`, and flushes automatically the
moment Tier 1 reconnects). The reverse direction has no such fallback: pixel-to-anyone has
no automatic path at all while unreachable, Sefirah's own pixel-to-laptop direction has
never worked (see `bugs/SPIKE-PIXEL-ACCESSIBILITY.md`), so Send Clip (§6.8) is the only way
out during that window, a manual tap, not automatic. Once re-armed, both hops become
CopyClip's own direct, ACK-confirmed path and Sefirah's role in this specific chain becomes
redundant again, not required, see §8's "pixel moved to Tier 1" entry for the full story.

The installed app (`android_app/`) stays installed on `pixel` regardless, now purely for
"Send Clip" (§6.8, now also image-capable) as the fallback for a moment it hasn't been
re-armed yet. Its automatic background bridge still **refuses to start**, and always will
on this specific device: its Shizuku only ever grants root (UID 0), never shell UID, and
`ClipBridgeService.kt`'s `requireShellUid()` guard checks the *actual* UID Shizuku handed
it rather than trusting that a successful `bindUserService()` call implies shell, confirmed
live: `ps -A` showed `root ... com.mohitarora.copyclip:service` after nothing but a normal
in-app "Grant Shizuku access", no explicit root request anywhere in this codebase. This
project's no-root rule has no carve-out for "Shizuku said it was fine": that specific tier
stays genuinely off on this device, permanently, unrelated to the Tier 1 path above. A
separate tool (Sefirah), if still installed, can also relay laptop-to-pixel independently
of any of this, though it's now redundant with Tier 1 for that specific direction. Full
setup for every path: [SETUP.md](SETUP.md).

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
| [transports/tcp_transport.py](transports/tcp_transport.py) | Tier 2: TCP to the installed app's Shizuku-hosted bridge (or Termux, same protocol either way), for when reboot survival matters. The hub is always the client here, it dials out, never listens. |
| [transports/push_listener.py](transports/push_listener.py) | The one direction that runs backwards: an inbound listener a device dials *into*, for the manual "Send Clip" fallback (§6.8), same mutual-HMAC handshake as the tcp tier, played from the opposite role. |
| [android/ClipBridge.java](android/ClipBridge.java) | The on-device helper. Source is tracked; the compiled `.dex` is gitignored and rebuilt with `android/build_dex.ps1`. Unchanged by the installed-app work above, it's still the exact same dex, just launched differently. |
| [android_app/](android_app/) | The installed Android app (package `com.mohitarora.copyclip`). `SyncForegroundService.kt` owns the Shizuku binding and republishes state to the UI; `shizuku/ClipBridgeService.kt` is the UserService that spawns `ClipBridge.dex` and enforces the shell-UID guard; `SendClipActivity.kt`/`shizuku/PushClient.kt` are the manual-fallback push path (§6.8). |
| [windows_app/tray.py](windows_app/tray.py) | The laptop side's tray icon + autostart registration, wrapping `copyclip.py`'s own `build_hub()`/`load_config()` unchanged, the daemon itself did not change. `windows_app/dashboard.py` is the optional tkinter status window. |

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

### 6.8 Send Clip: the manual fallback tier

For a device whose automatic background bridge can't run at all (root-mode Shizuku,
pre-Android-11 with no Wireless Debugging pairing, or any future case, §2's pixel update is
the concrete example), CopyClip needs a path that doesn't depend on that bridge in any way.
The one clipboard-read permission Android grants with **zero** special setup is being the
genuinely focused foreground app, the same exemption `bugs/SPIKE-PIXEL-ACCESSIBILITY.md`
measured is real, just narrower than hoped. "Send Clip" is built entirely on that exemption
and nothing else:

- `SendClipActivity.kt` is a real (if minimal) visible screen, not a Shizuku call. It waits
  for `onWindowFocusChanged(true)`, the documented, reliable signal that this app is now
  the actual focused window, before touching `ClipboardManager.getPrimaryClip()`, which
  needs no permission at all once that's true.
- Once read, the content is pushed to the laptop over a **new, backwards** connection: this
  device dials *out* to the hub instead of the hub dialing in, the one direction nothing
  else in this codebase does (every other tier has the hub as the client). `PushClient.kt`
  plays the client role of the exact same mutual-HMAC handshake `transports/framing.py`
  already defines; `transports/push_listener.py` plays the server role on the hub, something
  that has never existed before this, the laptop had never needed to *accept* a connection
  for CopyClip until this feature.
- The pushed frame lands in `hub.on_clip()` exactly like any other device's clip, same
  dedup, same domain-awareness (a Samsung-domain device never gets double-written), same
  policy checks. No new routing logic exists for this path; it reuses the same one entry
  point every other source already goes through.
- **A genuinely new operational requirement:** since the laptop has never accepted an
  inbound connection for CopyClip before, Windows Firewall blocks the new push port
  (`push_port`, default 8387) by default, silently, as a connection timeout on the device
  side rather than a clear rejection. One-time fix, documented in
  [SETUP.md](SETUP.md#35-firewall-rule-for-send-clip).

**Images (2026-09-01):** `SendClipActivity.kt` checks `ClipDescription.getMimeType(0)`
first; for a recognized image mime (matching `core/models.py`'s `IMAGE_MIMES` exactly:
PNG/JPEG/GIF/BMP/WEBP) it reads the actual bytes via `ContentResolver.openInputStream()`
on the clip item's URI, instead of the text-coercion path. Deliberately sent as **one
unchunked frame**, not the `SETB`/`SETC` chunking the main sync path uses for large
transfers (§6.7a): a screenshot is a few MB, comfortably under the 24 MiB cap, and adding
chunking here would be real complexity this feature doesn't need to carry. `PushClient.kt`
and `transports/push_listener.py` both widened their socket timeout from 8s/10s to 20s/20s
to give a slower link enough room for that one larger frame. Confirmed end to end with a
real device screenshot: pushed through the actual wire protocol, landed on the laptop
clipboard, and verified byte-for-byte identical to the source file by SHA-256, including
triggering the laptop's own screenshot-backup feature automatically (proof it was recognized
as a genuine image, not text). An unrecognized image format (HEIC, etc.) is reported as a
plain failure rather than silently transcoded, out of scope for a manual one-shot push.

**Permanently manual, not a gap pending a fix (2026-09-01):** whether the notification's own
"Send Clip" action could trigger itself automatically on every copy, no tap, was directly
spiked, not just reasoned about: `bugs/SPIKE-PIXEL-ACCESSIBILITY.md`'s follow-up section
found the background clipboard-change listener never fires at all while backgrounded on
this Android version, the notification is denied before any of the app's own code runs.
There is no mechanism on this platform version to build an automatic trigger on top of, so
this stays a manual, tap-triggered action indefinitely.

**Notification visibility & expansion persistence (2026-09-01):** the sync notification's
channel moved from `IMPORTANCE_LOW` to `IMPORTANCE_DEFAULT` (`copyclip_sync_v2`, since Android
ignores importance changes to an existing channel id) so it ranks in the active alerting
section rather than the minimized silent section.

Live reverse-engineering of Sefirah vs CopyClip via dumpsys and bytecode disassembly revealed
the root cause for notification collapse on shade close/reopen:
1. **Notification Thrashing**: CopyClip was executing `updateNotification()` on every single
   poll tick (`POLL_MS = 2500ms`), pushing thousands of re-posts (`numUpdatedByApp > 1500`)
   to SystemUI even when the status text was identical. When SystemUI receives a notification
   update, it resets the user's manual expansion state.
2. **Missing BigTextStyle**: Without `NotificationCompat.BigTextStyle().bigText(...)`, SystemUI
   treats the notification as having no explicit large content view (`bigContentView`).
3. **Media Style vs Alerting Stack Ranking**: Sefirah holds a `MediaSession` (`mMediaNotificationKey`),
   which Android SystemUI specially pins to the top of the shade and keeps expanded by default.
4. **SystemUI Top-Slot Rule**: On Android (Stock/Pixel UI), SystemUI automatically expands
   only the top-most notification (Index 0) on fresh shade pull-downs; lower notifications
   (Index 1+) default to collapsed on fresh open, though multiple notifications can be
   manually expanded simultaneously while the shade remains open.

In CopyClip, deduplicating updates (`lastNotificationText` guard) and adding `BigTextStyle`
ensures that CopyClip ranks at the top of the active notifications and stays auto-expanded
with the "Send Clip" action button visible every time the notification shade is opened.
Confirmed live on Pixel: pressing Home and reopening the notification shade keeps CopyClip
expanded with the Send Clip action immediately accessible.

**Multi-OEM Screenshot Directory Support (2026-09-01):** Android OEMs store screenshots in
differing filesystem directories:
- Samsung OneUI (Galaxy S24 Ultra, Tab S10 Ultra): `/sdcard/DCIM/Screenshots/`
- Stock Android / Google Pixel (Pixel 1) / AOSP / Motorola: `/sdcard/Pictures/Screenshots/`

`ClipBridge.java`'s `SCREENSHOT_DIRS` array scans both directories on every poll tick, identifying
the newest screenshot across all valid paths via `findNewestScreenshot()`. Confirmed live on Pixel:
taking a screenshot on the Pixel (`/sdcard/Pictures/Screenshots/`) is detected immediately,
streamed across wireless ADB, placed on the Windows clipboard, and saved in
`Temporary Screenshots/` automatically.

### 6.6c Samsung SEF Metadata, Backward Codec Scanning & Mesh Liveness (2026-09-03)

**1. The Samsung SEF Metadata Trailer Problem:**
Samsung One UI's Smart Capture engine appends variable-length Samsung Extended Format (SEF) metadata
trailing *after* the standard JPEG End of Image (`0xFF 0xD9`) marker. On Galaxy S24 Ultra and Tab S10 Ultra,
binary file inspection revealed a 197-byte block terminating with the ASCII magic word `SEFT`
(`0x53 0x45 0x46 0x54`). Prior to 2026-09-03, `ClipBridge.java`'s completion check required
`data[len-2] == 0xFF && data[len-1] == 0xD9`. This caused `isCompleteImage()` to return `false` on
every Samsung screenshot, triggering an 8-second thread-blocking sleep loop that starved clipboard polling
and degraded screenshot sync latency from < 1s to 60-90+ seconds (see `bugs/BUG-010-samsung-sef-screenshot-delay.md`).

**2. The Backward Marker Scanning Algorithm:**
To support arbitrary OEM metadata blocks while maintaining strict protection against partial image reads:
- Standard JPEGs ending in `0xFF 0xD9` validate immediately in O(1).
- Files terminating in ASCII `SEFT` validate immediately in O(1).
- For non-standard trailers, `ClipBridge.java` performs a backward scan up to 64 KB from EOF searching
  for the `0xFF 0xD9` sequence.
- For PNGs, it scans up to 4 KB backwards for the `IEND` chunk marker (`0x49 0x45 0x4E 0x44`).
- All checks execute entirely in-memory in under 1 ms.

**3. Thread Starvation Guard & Timestamp Caching:**
- If `readCompleteScreenshotFile()` exhausts its 8-second retry window without finding a valid marker,
  `lastScreenshotName` is advanced to the unreadable file and a `SKIP` event is logged. This prevents
  an unreadable or corrupt file from locking the main polling thread into permanent retry loops.
- `findNewestScreenshot()` caches `newest.lastModified()` in a primitive `long`, halving filesystem stat
  syscalls across directory traversals.

**4. Wire Transport Streaming Liveness:**
During large base64 image transfers, chunks are streamed in 64 KB slices with an ACK window of 8 chunks
(~524 KB). Over Tailscale Wi-Fi, transmission bursts can occasionally exceed default single-clip timeouts.
`nodes/android_node.py` enforces `chunk_timeout = max(self.ack_timeout_s, 12.0)`, preventing false
transport drops during active screenshot streaming.

**5. Empirical Multi-Device Cartesian Latency Profile (120 Live Runs):**
Across all 12 directed device pairs between Laptop, S24 Ultra, Tab S10 Ultra, and Pixel 1:
- Text sync: **0.2 ms** (listener callback to laptop) up to **745 ms** (multi-hop hub relay).
- S24 Ultra Screenshot sync: **842 ms average** (down from 60-90+ seconds, a **75x speedup**).
- Tab S10 Ultra Screenshot sync: **1,128 ms average** (~1.1s).
- Google Pixel Screenshot sync: **1,018 ms average** (~1.0s).
- Zero transport stalls or frame drops across all 120 live benchmark iterations.

### 6.6d Directory Inode mtime Short-Circuit, Adaptive Keyguard Backoff & Zero-Cost Polling (2026-09-04)

**1. Directory Inode Modification Timestamp Caching:**
On devices with large media libraries (e.g. 728 existing captures in `/sdcard/DCIM/Screenshots` on Galaxy S24 Ultra), calling `File.listFiles()` on every 250ms polling tick resulted in 2,912 `java.io.File` heap allocations and kernel `stat()` syscalls per second. Linux ext4 and f2fs filesystems update the parent directory's inode `mtime` whenever an entry is created, renamed, or unlinked. `findNewestScreenshot()` inspects `SCREENSHOT_DIRS[i].lastModified()` first against `lastDirMtimes[i]`. If unchanged, it returns the cached `File` reference in O(1) (< 0.05 ms), completely eliminating directory traversals during idle operation and dropping idle CPU utilization to strictly **0.0%**.

**2. Adaptive Keyguard Poll Backoff:**
Android 10+ enforces background clipboard isolation behind the keyguard (`getPrimaryClip()` returns `null` when locked). `ClipBridge.java` implements `POLL_MS_LOCKED = 1500L`: when `isLocked()` is true, the polling loop slows to 1.5 seconds, reducing CPU wakeups and Binder IPC to `ITrustManager` by 83%. When `isLocked()` transitions from locked to unlocked, `lastStamp` is reset to `-1L` and `flushPending()` executes immediately, resuming normal 250ms polling with zero latency.

**3. Multi-Profile Samsung Secure Folder State Isolation:**
On Samsung devices with Secure Folder enabled (profile id=150), `dumpsys trust` outputs `deviceLocked=1` for the Secure Folder profile even while the primary user (`id=0`, Owner) is completely unlocked (`deviceLocked=0`, `mDreamingLockscreen=false`). Diagnostic tools and verification scripts must specifically filter for `id=0` or inspect `mDreamingLockscreen` to prevent false "device locked" reports on Samsung hardware.

**4. Remote DEX Cache Verification on Wire Transport:**
Before executing `adb push`, `AdbTransport._push_dex()` issues `adb shell stat -c %s /data/local/tmp/clipbridge.dex`. If the remote byte count matches the local DEX file (31,452 bytes), the upload is skipped, saving 300 to 500 ms during connection re-establishment and eliminating flash wear.

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
| `IMAGE_DESC_HOLD_MS` | 4,500 (hardcoded in `android/ClipBridge.java`) | How long an ordinary clipboard-sourced image is held before being announced, so a real screenshot file push (in either order) can supersede it (§6.6b, `bugs/BUG-006`) | Lower = less delay on a genuine standalone image copy, but less time for a slow screenshot-folder poll tick to catch up; higher = safer against the race, more delay, and a bigger window where two genuinely distinct rapid image copies could collapse into one |
| `POLL_MS` | 250 (hardcoded in `android/ClipBridge.java`) | Polling period while device is active and unlocked (§6.6) | 4 ticks/sec ensures sub-300ms clipboard detection with minimal battery footprint |
| `POLL_MS_LOCKED` | 1,500 (hardcoded in `android/ClipBridge.java`) | Polling period while keyguard is active (§6.6d, `bugs/BUG-012`) | 83% reduction in wakeups and Binder IPC while device is locked; resets immediately to 250ms on unlock |
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

**(2026-08-31) The installed app was walked back off phone/tab the same day it was
extended to them, over-engineering caught by the user asking a direct question, not by any
measurement of mine.** *Symptom:* none, functionally, phone and tab synced correctly on the
app/Shizuku tier the whole time. *What actually happened:* asked directly whether the app
was even necessary given "Tailscale was already working perfectly" before it, versus reverting
to the plain-adb method with no shared key, port, or hub address. *Investigated because that
question deserved a real answer, not a defense of already-built work:* the reboot-survival
concern that had originally motivated moving phone/tab onto the Shizuku-hosted tier was
checked against this session's own already-recorded evidence, and it didn't hold up. The
Troubleshooting entry in README, itself written and confirmed earlier the same day, already
established that plain `adb` survives a real reboot once Wireless Debugging has been paired
with the network once. That was true for phone/tab specifically before any of the app work
started. *Root cause:* the app/Shizuku tier was extended to phone/tab based on a reboot-
survival justification that a fact already sitting in this same file's own session history
would have ruled out, if it had been checked against phone/tab specifically rather than
applied uniformly across all devices including the one (pixel) it was actually necessary
for. *Fix:* `config.json` reverted phone and tab to `"transport": "adb"`; the app was
uninstalled from both (`adb uninstall com.mohitarora.copyclip`); both directions verified
live afterward (raw `ClipBridge get`/`set` round trip, real content, real device). Pixel is
unaffected, it stays on the app tier because it's the one device that genuinely fails the
plain-adb reboot-survival check (Android 10, predates the OS feature that provides it).
*Generalizable lesson:* "this device is having a problem" is not evidence for "every device
should get the same fix." A justification for added complexity needs to be checked against
each specific device it's being applied to, not assumed to generalize from the one device
that actually needed it, and a working system, before extending it further, is a better
moment to ask "does this scale down to less than what's already built" than after.

**(2026-09-01) A real, clean cold-boot test of `pixel` confirmed `BootReceiver` genuinely
works with zero manual action, and surfaced a real, unexplained anomaly worth tracking.**
*Test:* the device was rebooted and deliberately left untouched (unlocked only, no app
opened), specifically to get an uncontaminated read after an earlier attempt got muddied by
manual app-opening. *Confirmed, not inferred:* `adb logcat` showed Android's own
`ActivityManager` starting `com.mohitarora.copyclip` specifically `for broadcast
{.../.service.BootReceiver}`, unprompted, the service came up correctly, bound Shizuku,
attempted the bridge, and correctly refused on root, exactly as designed, all without
opening the app. Timing has real variance though, a `ps -A` check at 111s uptime found
nothing running yet; the same check at 166s found it alive, logs place its actual start
somewhere in that window, matching the same rough delay range the (contaminated) first
reboot test also suggested.

**A genuine, unexplained anomaly: wireless adb's TCP listener came back up on this boot
with no `adb -d tcpip 5555` run by anyone.** This directly contradicts what this file and
`config.json`'s `_pixel` note have said all session (no persistent pairing survives reboot
on this device, Android 11+ only). `getprop service.adb.tcp.port` read `5555` unprompted;
`adb logcat` showed Android's own `AdbDebuggingManager` processing a remembered, already-
authorized key from the laptop (`state = 4, alwaysAllow = true`) starting at boot+27s,
**before** Shizuku's own boot receiver fired at boot+35s, so Shizuku restarting itself is
not obviously the cause. **MacroDroid ruled out, confirmed directly with the user**: no
macro was ever set up for this, and the app isn't even installed on `pixel` (it's present
on phone/tab, which is likely what made it a plausible guess in the first place, but that
doesn't carry over to this device). Remaining candidates, still none confirmed: Shizuku's
root-mode server doing this anyway despite the timing (its *process* started later, but
something it left behind from a prior run is not ruled out); a Magisk boot script or module
(the device is rooted via Magisk; this would need root-shell inspection of
`/data/adb/service.d/` or similar to check, not yet done, and not attempted casually given
this project's own no-root rule, even for pure inspection); something else entirely. USB
was physically connected during this test (left from earlier work), which cannot be ruled
out as a factor without a repeat test with it genuinely unplugged. **Do not treat this as
"pixel no longer needs the manual re-arm"** until the mechanism is identified and
reproduced deliberately, not just observed once, this note
exists so the observation isn't lost, not to claim the limitation is gone.

**(2026-09-01) `pixel` moved from the app/Send-Clip-only tier to plain Tier 1 adb, the same
day it was asked "does opening Sefirah count as the automatic bridge being done."**
*Symptom:* none, this was caught by a direct question, not a failure. *What actually
happened:* the user asked whether Sefirah relaying laptop-to-pixel meant "automatic bridge
is already done," which surfaced a real architectural gap: pixel had never been tried on
plain adb Tier 1 at all this session, only on the Shizuku-hosted tier (which is root-refused
there) and Send Clip (manual, outgoing-only). *Root cause:* Tier 1 needs nothing pixel
doesn't already have, no root, no Shizuku, just shell-UID adb access, the same mechanism
phone/tab already prove reliable, its only real cost is the manual re-arm this device
already needs after every reboot regardless of which tier is used, since that limitation is
Android-version-specific (no persistent Wireless Debugging pairing before Android 11), not
tier-specific. *Fix:* `config.json`'s pixel entry flipped to `"transport": "adb", "enabled":
true`; verified live both directions after a cold-connection retry settled (`laptop ->
pixel` and `pixel -> laptop`, exact content, confirmed by direct base64 decode, not log
inference alone). *Side effect, also real:* this incidentally explained a previously-
unexplained intermittent symptom (S24-to-pixel copies sometimes not arriving): that path
was always S24-to-laptop (CopyClip, reliable) then laptop-to-pixel (Sefirah, a third-party
app with no logs or guarantees available to this codebase). Only fixed while Tier 1 is
actually up, though, not permanently: whenever pixel is unreachable (the common case right
after a reboot, until re-armed, see the `_pixel` config note), that second hop falls back
to depending on Sefirah again exactly as before, confirmed live 2026-09-01, `phone ->
laptop` kept firing normally in the log the whole time pixel sat unreachable, with
CopyClip's own attempt at the second hop queued in the outbox instead of delivered. See §2's
own update note for the fuller breakdown of what does and doesn't keep working during that
window. *Generalizable lesson,
same shape as the previous day's:* a device's constraint (pixel needs Shizuku, therefore
root-refused, therefore needs a fallback) had been treated as ruling out its most basic tier
too, without that being separately checked. The manual-rearm cost of Tier 1 was already
being paid for other reasons (this session also produced a re-arm convenience script,
`<software>\Phone\Pixel\rearm_wireless_adb.bat`, personal tooling outside this repo); it
just hadn't been connected to "therefore Tier 1 works here too."

**(2026-08-31) `Theme.NoDisplay` does not qualify for Android's clipboard-focus exemption,
even though the Activity genuinely starts.** *Symptom:* `SendClipActivity`, first built as
an invisible `Theme.NoDisplay` trampoline meant to read the clipboard and finish
immediately, always read an empty clipboard. *Investigated because:* this project's own
measure-before-concluding rule, rather than trusting that "the Activity started" was
equivalent to "the Activity is focused." *Measured:* `adb logcat` around a real tap showed
`ActivityTaskManager: START ... SendClipActivity` followed immediately by `ClipboardService:
Denying clipboard access to com.mohitarora.copyclip, application is not in focus neither is
a system service`, the exact restriction message from `bugs/SPIKE-PIXEL-ACCESSIBILITY.md`,
on an Activity that had, by every other measure, just started as the topmost task. *Root
cause:* `Theme.NoDisplay` is defined to never actually create/show a window (that's its
entire purpose), and window focus, not merely being the started/topmost activity, is what
the platform's clipboard check apparently keys on. *Fix:* `SendClipActivity` now shows real
(if minimal) visible content and gates the clipboard read on `onWindowFocusChanged(true)`,
confirmed working end-to-end on a real device afterward (§6.8). *Generalizable lesson:* an
Activity being "started" and an Activity being "focused" are not the same fact on this
platform, and only the second one is what several of Android's background-access exemptions
actually check.

**(2026-08-31) A newly-installed system-wide JDK broke Android builds with no code
change.** *Symptom:* `gradlew.bat assembleDebug` failed immediately with
`IllegalArgumentException: 25.0.2` out of the Kotlin compiler, on a build that had worked
earlier the same session. *Root cause:* a JDK 25 install had been added to this machine
since, with nothing pinning `JAVA_HOME`, so it shadowed `java` on `PATH` ahead of the JDK
this toolchain actually expects; this project's Kotlin compiler version cannot parse a JDK
25 version string at all. *Fix:* `android_app/gradle.properties` now pins
`org.gradle.java.home` to Android Studio's own bundled JDK (21), project-local only, does
not touch the system `JAVA_HOME`. *Why this matters beyond one machine:* any future JDK
install on this or another dev machine can silently break the build the same way; if
`gradlew.bat` ever fails with a bare version-number "what went wrong" message, check
`java -version` against what's pinned in `gradle.properties` before assuming a code problem.

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

**Confirmed for real (2026-08-31):** `pixel`, a Google Pixel phone, was added as a fourth
device with a single `config.json` entry and zero code changes anywhere (`core/hub.py`,
`domains/sync_domain.py`, `android/ClipBridge.java` all untouched). It is standalone, not a
`domains[]` member, since a Pixel has no Samsung Continuity relationship with the phone/tab
pair — `core/hub.py`'s existing `_standalone_nodes()` (already exercised daily for the
laptop) picked it up with no new logic. `android/ClipBridge.java` needed no changes either:
grepping it for Samsung-specific identifiers (`samsung`, `sec.android`, `com.samsung`)
returns nothing — it only ever talks to AOSP's `IClipboard` binder via version-adaptive
reflection (already branching on `SDK_INT`), so a stock-Android device is, if anything, a
more direct fit than the Samsung devices it was proven on first. The one real prerequisite
is device-side, not code-side: wireless adb must be armed once with the device on USB
(`adb -d tcpip 5555`, same as README's existing per-device Troubleshooting step), since
`persist.adb.tcp.port` does not survive a reboot on any of these devices.

**Follow-up, same day:** a real, separate crash was then found and fixed on this device --
[BUG-009](bugs/BUG-009-pixel-clipbridge-printstream-crash.md), a `PrintStream` constructor
missing from Android 10's core library, invisible on `phone`/`tab` since they run much newer
Android. Once fixed, `pixel` connected and read cleanly, but a second, unresolved question
(a write-path timeout, never conclusively explained) surfaced before it could be fully
exercised. Separately, `pixel` turned out to have no non-root path to the kind of persistent
wireless-adb pairing `phone`/`tab` already have (Android's Wireless Debugging pairing doesn't
exist before Android 11; this device is permanently on Android 10) -- combined with genuinely
infrequent use (1-2 days a month), that made the manual USB re-arm needed after every cold
start not worth it, so `pixel` is `"enabled": false` in `config.json` today, with Sefirah
(already installed on it) covering its clipboard sync instead. The fix and the standalone
support both stay in the codebase regardless -- flipping it back on is still just the one
config line this section always promised.

---

## 13. Where to go for more

- [README.md](README.md) — setup, config reference, troubleshooting, quick architecture
  diagram.
- [bugs/](bugs/README.md) — detailed, dated writeups of specific past incidents, with
  measured evidence, root cause, fix, and rollback instructions for each.
- [otherCopyProjects/NOTES.md](otherCopyProjects/NOTES.md) — what was adopted or rejected
  from prior art (scrcpy, ClipCascade, and others) and why.
