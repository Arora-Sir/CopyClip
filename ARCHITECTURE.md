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
| [nodes/windows_node.py](nodes/windows_node.py) | The laptop as a node. Thin wrapper over [winclip.py](winclip.py) (raw ctypes, no pywin32). |
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
| `grace_s` (per domain) | 8.0 | How long a Continuity confirmation probe is given before counting as a miss (§6.5) | Above Samsung's measured ~2.5s relay by a wide margin |
| `fail_threshold` (per domain) | 3 | Consecutive missed confirmations before a domain is marked degraded (§6.5) | Lower = faster fallback to direct writes, more sensitive to one-off blips |

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

---

## 9. Logging & debugging

- Logs live in `logs/copyclip.log` (rotated at 2 MB, 3 backups kept — bounded disk use), set
  up in `copyclip.py`'s `setup_logging()`. Both `logs/` and `state/` are gitignored — nothing
  from either is ever committed.
- Level is controlled by `config.json`'s `log_level` (default `INFO` — meant for the
  AutoHotKey-launched production instance) or overridden per run with `--log-level DEBUG`.
- **`Start CopyClip.bat`** — normal, quiet run (`INFO`).
- **`Start CopyClip (Debug).bat`** — same script, `--log-level DEBUG`, for a testing
  session. One codebase, no duplication; DEBUG statements use lazy `%`-formatting so they
  cost nothing when disabled.
- Useful patterns when reading a log: grep for `"holding.*target unavailable"` to spot stuck
  outbox retries (§6.4), `"no ACK/DEFER"` for a channel that timed out (§6.3), and
  `"unexpected connect error"` for genuine ADB-level connectivity trouble.

---

## 10. File map

See README's [Layout](README.md#layout) table for the quick version. The one addition worth
calling out here: **`tests/`** — `test_core.py` is pure routing/dedup/outbox logic, no
devices, runs in under a second; `test_android_node.py` covers the ACK-confirmation logic
with a fake transport, also fast; `test_e2e.py`/`test_three_device.py` need real, unlocked
hardware.

---

## 11. Future scope: designed for, not built

This section exists so a future session doesn't have to rediscover the plan from scratch —
it was already researched when the wire protocol and payload model were designed, even
though only text sync got built for v1.0.0.

### Images

**What's already in place for this, right now, with zero further work:** `ClipPayload`
(`core/models.py`) already stores an arbitrary `mime: str` plus `data: bytes` — text is
just `mime="text/plain"` under the hood, not a hardcoded assumption. `Policy.reject_reason()`
(`core/policy.py`) already has an `allow_non_text` flag (`config.json`'s `allow_non_text`,
currently `false`) gating exactly this — flipping it is the *policy* half of turning images
on. The wire protocol (§5) already carries a MIME field on every `CLIP`/`DESC` frame.
`core/hub.py`, `core/dedup.py`, and `core/outbox.py` would not need to change at all — they
already operate on bytes + mime, never on text specifically.

**What's genuinely missing, and the two real traps found during the original research:**
- **Windows side** (`nodes/windows_node.py` / `winclip.py`): needs to read the clipboard's
  `CF_DIB` bitmap format and convert it to a sensible encoding; `winclip.py` today only
  handles `CF_UNICODETEXT`.
- **Android side** (`android/ClipBridge.java`) — **the trap that catches most naive
  implementations:** `ClipData.newUri()` cannot be given a `file://` URI — Android throws
  `FileUriExposedException`. The correct path is a `MediaStore` **insert** first, to obtain
  a real `content://` URI, and build the `ClipData` from that. This was confirmed by
  research, not assumed.
- `TEXT_MIMES` in `core/models.py` would need an image MIME added (e.g. `image/png`)
  wherever "is this text" branches matter — `ClipPayload.is_text`/`.text`/`.char_len` all
  assume a text/plain-or-html world today and would need an image case that skips the
  character-decode path entirely.

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

## 12. Where to go for more

- [README.md](README.md) — setup, config reference, troubleshooting, quick architecture
  diagram.
- [bugs/](bugs/README.md) — detailed, dated writeups of specific past incidents, with
  measured evidence, root cause, fix, and rollback instructions for each.
- [otherCopyProjects/NOTES.md](otherCopyProjects/NOTES.md) — what was adopted or rejected
  from prior art (scrcpy, ClipCascade, and others) and why.
