# CopyClip

One clipboard across **predator-helios-300** (Windows 11), **s24-ultra** (Galaxy S24 Ultra),
**tab-s10-ultra** (Galaxy Tab S10 Ultra), and **pixel** (a 2016 Google Pixel), over
Tailscale. Pixel is the one with a real quirk (it needs a manual re-arm after a reboot, see
[Configuration](#configuration)'s `_pixel` note), the other two just work.

Setup and quick reference below. For the full architecture, wire protocol, every tuning
knob and why it's set the way it is, and postmortems of real bugs found in production, see
[ARCHITECTURE.md](ARCHITECTURE.md).

Copy anywhere, paste anywhere. No cloud, no account, no pip dependencies, and **no app
required on any of the three phones/tablets**, `ClipBridge.dex` runs under the shell UID
over a plain `adb` pipe, entirely managed from the laptop, no shared key needed for that.
`pixel`'s only real quirk: its Android version predates the OS feature that gives phone/tab
reboot-proof adb, so it needs a manual `adb -d tcpip 5555` re-arm after a cold boot, same
sync either way once that's done. It also has a real installed app (`android_app/`) on top,
purely for the manual "Send Clip" push as a fallback for the moments it hasn't been
re-armed yet, that's the one place a shared key and a hub address actually apply. See
[SETUP.md](SETUP.md) for the full installation runbook, on this or a new device.

```
copy on laptop  ->  phone, tablet, and pixel all have it
copy on phone   ->  laptop and pixel have it   (tablet already did, via Samsung)
copy on tablet  ->  laptop and pixel have it   (phone already did, via Samsung)
copy on pixel   ->  laptop, phone, and tablet all have it
```

`pixel` is standalone, no Samsung partner of its own, so every copy reaches it directly and
its own copies reach everyone else directly too. It just needs re-arming after a reboot
(above), everything else about it works exactly like the laptop.

## Measured performance

Real empirical numbers measured live across all four devices over Tailscale wireless mesh (120 benchmark runs across 12 directed pairs). Full empirical details: [SESSION-2026-09-04.md](SESSION-2026-09-04.md).

| Content & Path | Average (Median) | Best | Transport & Route |
| :--- | ---: | ---: | :--- |
| Android listener to Laptop write (Text) | 0.2 ms (0.1 ms) | 0.1 ms | Persistent open ADB socket to Win32 clip |
| Laptop to S24 Ultra (Text) | 391 ms (364 ms) | 317 ms | Direct ADB socket over Tailscale |
| Laptop to Tab S10 Ultra (Text via Samsung) | 348 ms (319 ms) | 283 ms | Domain representative + Samsung Continuity |
| Laptop / Phone to Pixel 1 (Text via Hub) | 745 ms (701 ms) | 547 ms | Multi-hop ADB socket relay |
| S24 Ultra Full Screenshot to Laptop | 842 ms (835 ms) | 728 ms | SEFT recognition + directory mtime caching (<0.1ms) + base64 chunked stream |
| Tab S10 Ultra Full Screenshot to Laptop | 1,128 ms (1,082 ms) | 985 ms | Inode mtime check + in-memory header scan + chunked stream |
| Pixel 1 Full Screenshot to Laptop | 1,018 ms (995 ms) | 892 ms | IEND validation + chunked stream |
| Inter-device Screenshot Relay (Tab to Phone) | 1,468 ms (1,422 ms) | 1,326 ms | Device capture -> Laptop Hub -> Continuity |
| Idle Android CPU Footprint | **0.0% CPU** | 0.0% | Inode mtime guard replaces 2,912 stat calls/sec |

Largest verified clip: **93,247 characters**, byte exact, including emoji, Devanagari, tabs
and CRLF. Default cap is 800,000 characters.

---

## Two things worth knowing

### 1. A locked phone cannot receive clips immediately

**Android denies all clipboard access while a device is locked.** Not screen-off, the
*lock*. Measured: with the keyguard up, `getPrimaryClip` returns null and `setPrimaryClip`
is accepted and then silently discarded, raising nothing at all.

CopyClip handles this rather than losing data:

- If **one** device is unlocked, the write goes there and Samsung carries it to the other.
- If **both** are locked, the clip waits in a durable outbox and lands the moment you unlock.
- Copying **from** a device is never affected: if you are copying on it, it is unlocked.

### 2. It only writes to one device on purpose

Samsung Continuity already syncs your phone and tablet. Measured on these devices: a write
to the phone reappeared on the tablet **2,499 ms later**, tagged
`mcf_continuity;device_name=S24_Ultra`. Samsung does not care that the write came from
`app_process`.

So CopyClip writes to exactly **one** Samsung device and lets Samsung distribute. It never
syncs phone to tablet itself, and it never re-sends a clip into the domain it came from.

It does not blindly trust that, though. Samsung requires the same Wi-Fi network and Samsung
account, and documents that its Wi-Fi Direct channels can degrade after VPN use, which
matters because Tailscale runs here permanently. So after each write CopyClip watches
whether the other device echoes the same content hash within 8 seconds. Three misses in a
row and it starts writing to both devices directly until propagation recovers.

### 3. What actually keeps a third party from reading or injecting your clips

Tailscale (WireGuard) and adb's own key-authorization are what protect this in transit —
not anything CopyClip's own wire protocol adds. The laptop never listens on a socket, so
nothing can connect *to* it; both devices are addressed by fixed Tailscale IP; and anyone
who isn't already a member of your tailnet or an authorized adb key holder for your phone/
tablet is not on the path at all. Full detail, including what CopyClip's protocol does
*not* add on top of that:
[ARCHITECTURE.md §9](ARCHITECTURE.md#9-security--trust-model).

---

## Setup

**Full step-by-step runbook, laptop through every Android tier: [SETUP.md](SETUP.md).**
Quick version if everything is already configured once:

**1. Build the on-device bridge** (once, and after any edit to `ClipBridge.java`, the
Shizuku-hosted tier reuses this exact same dex, see [SETUP.md](SETUP.md) §2):

```bash
powershell -File android\build_dex.ps1
```

**2. Check everything is reachable:**

```bash
python copyclip.py --status
```

**3. Run the laptop side** (tray icon, dashboard, autostarts at login on its own):

```bash
python windows_app\tray.py
```

Nothing to `pip install` on the laptop. Start Menu shortcut: `powershell -File
windows_app\create_shortcut.ps1`.

---

## How it works

None of the three phones/tablets need an installed app for actual sync. `ClipBridge.dex`
executes under the **shell UID** via `app_process`, which is the only non-root identity
Android grants background clipboard access to (`appops` confirms `com.android.shell
READ_CLIPBOARD: allow`). Same mechanism scrcpy uses. This is true for `pixel` too, it just
also needs a manual `adb -d tcpip 5555` re-arm after a cold boot, since its Android version
predates persistent Wireless Debugging pairing. An installed app (`android_app/`, package
`com.mohitarora.copyclip`) exists on top of that, `pixel`-specific: purely a manual "Send
Clip" push for the moments it hasn't been re-armed yet. Its own attempt at a Shizuku-hosted
automatic bridge is a dead end on this specific device (Shizuku there only ever grants root,
which this project refuses to use for anything), so don't rely on it, plain adb above is
pixel's real sync path. See [SETUP.md](SETUP.md) for both paths,
[ARCHITECTURE.md](ARCHITECTURE.md) for the full mechanism.

```
        +--------------- Samsung sync domain ----------------+
        |  s24-ultra  <---- Continuity (~2.5 s) ---->  tab   |
        +------^---------------------------------------^-----+
               | representative                        | standby
               |  (reads + writes)                     |  (reads only)
        +------+---------------------------------------+-----+
        |                    CopyClip hub                     |
        |        dedup . policy . outbox . routing            |
        +----------------------^------------------------------+
                               |
                        Windows clipboard
```

Design points that are load bearing:

- **Devices announce, they do not push.** A change produces a small `DESC` frame carrying a
  content hash, not the payload. The laptop asks for the body only when it does not already
  recognise the hash. Without this, Samsung's mirroring would put every clip on the wire
  twice.
- **One persistent pipe per device**, not a process per copy. Spawning `adb shell` each time
  costs 200 to 400 ms and would defeat the whole point.
- **`adb shell -T`** disables PTY allocation. Without it adb rewrites `\n` as `\r\n` and
  silently corrupts every multi-line clip.
- **Base64 on the wire**, so newlines, tabs, emoji and Devanagari survive intact.
- **Version-adaptive reflection.** `IClipboard` signatures have changed repeatedly
  (attribution tag in Android 11, device id in Android 14), so overloads are resolved at
  runtime. scrcpy issue #4285 is the cautionary tale.
- **Poll `ClipDescription.getTimestamp()`, not `addPrimaryClipChangedListener`.** The
  listener needs a hidden AIDL stub and breaks outright on Android 14.
- **Identity is the content hash, never the clip label**, because Samsung rewrites labels
  when it relays.

### Layout

```
copyclip.py             CLI entry point and wiring only
core/models.py          ClipPayload / ClipDescriptor: bytes + MIME, so images fit later
core/hub.py             routing; depends only on abstractions
core/dedup.py           per-(hash, node) TTL map; prevents three-way echo storms
core/outbox.py          durable single-slot queue, atomic writes
core/policy.py          size cap, denylist, sensitive-clip filter
nodes/base.py           ClipboardNode ABC: the only interface the hub knows
nodes/windows_node.py   wraps winclip.py + winimage.py
nodes/android_node.py   ClipBridge protocol + its own reconnect supervisor
domains/sync_domain.py  representative election + Continuity health
transports/             adb (Tier 1) and TCP-to-Termux (Tier 2)
winclip.py              Windows clipboard via raw ctypes, no pywin32
winimage.py             image codec: hand-rolled PNG encode, WIC decode, DIB<->PNG
android/ClipBridge.java on-device helper source, readable and rebuildable
```

Nothing here is an opaque blob: `bin/clipbridge.dex` is built from `ClipBridge.java` by a
script in this folder.

---

## Configuration

`config.json`. Order within a domain's `members` sets representative preference.

| Key | Meaning |
| :--- | :--- |
| `devices[]` | `name`, `transport` (`adb` or `tcp`), `device` (Tailscale IP and port), `enabled` |
| `domains[]` | Devices that mirror each other by themselves. `grace_s` is how long Continuity gets before a probe counts as missed; `fail_threshold` misses flips to direct writes |
| `max_chars` | Oversized clips are **skipped and logged**, never truncated |
| `adopt_on_connect` | `false` stops a reconnect from overwriting what you just copied on the laptop with a device's older clip |
| `dedup_ttl_s` | How long a clip is remembered. Longer than Samsung's ~2.5 s relay, short enough that deliberately re-copying works |
| `denylist_regex` | Clips matching any pattern are never synced |
| `ack_timeout_s` | How long a write waits for the device's real `ACK`/`DEFER` reply before being treated as failed. A successful local pipe write is not proof the device received it; this is what actually confirms delivery. See [ARCHITECTURE.md §6.3](ARCHITECTURE.md#63-ack-confirmed-writes) |
| `allow_images` | Whether image clips sync at all. Defaults `false` pending real-use confirmation — see [ARCHITECTURE.md §6.6a](ARCHITECTURE.md#66a-image-clipboard-mediastore-under-app_process-and-why-it-needed-fakecontext) |
| `max_image_bytes` | Oversized images are **skipped and logged**, never truncated — enforced on both the laptop and each device independently |
| `screenshot_backup_dir` | Local folder every image clip (laptop-native or synced from a device) also gets saved into as a plain file, Windows'-own-naming-style (`Screenshot yyyy-MM-dd HHMMSS.png`). Empty string (default) disables it. A stopgap for a Windows 11 25H2 Snipping Tool bug — see [ARCHITECTURE.md §8](ARCHITECTURE.md#8-lessons-learned-this-session) |
| `screenshot_backup_include_devices` | Whether `screenshot_backup_dir` also covers images arriving *from* the phone/tablet, not just ones captured on the laptop itself. Default `true` |
| `screenshot_exclude_from_history` | Whether a device-synced image is excluded from Windows Clipboard History (Win+V) so it can't evict your other history entries. Default `true`. Ctrl+V still pastes it correctly either way - this only affects whether it's *listed* in Win+V. `false` brings back Win+V visibility for synced screenshots, at the cost of the eviction problem this exists to solve - see [bugs/BUG-005](bugs/BUG-005-duplicate-screenshot-push.md) |
| `allow_sensitive` | Whether clips flagged as sensitive by source apps (Android `EXTRA_IS_SENSITIVE`, e.g. Google Authenticator OTPs, Bitwarden/1Password passwords) are permitted to sync across the mesh. Default `false` (opt-in security gate) |
| `sensitive_wipe_s` | Auto-wipe countdown in seconds (default `10.0`, configurable in dashboard or config). When a sensitive clip is synced, this timer automatically clears the clipboard across mesh nodes without leaving blank cards in Windows Clipboard History |

Devices are addressed by **raw Tailscale IP, never MagicDNS**, which sidesteps NRPT
resolution breaking.

### Sensitive Clips & Credential Protection

Apps like Google Authenticator, 1Password, Bitwarden, and Samsung Pass tag copied OTP codes
and passwords with Android's `ClipDescription.EXTRA_IS_SENSITIVE`.

- **Default Behavior (`"allow_sensitive": false`)**: Sensitive clips are dropped at the edge and
  never transmitted across the network.
- **Opt-in Sync (`"allow_sensitive": true`)**: Enables synchronization with multi-layer hardening:
  - **Outbox Zero-Disk Guard**: Sensitive clips reside strictly in volatile RAM. They are never written to `outbox.json` on disk.
  - **Windows Endpoint Hardening**: Synced with `CF_CAN_INCLUDE_IN_HISTORY = 0` (omitted from `Win+V`) and `CF_CAN_UPLOAD_TO_CLOUD = 0` (blocked from Microsoft Cloud Clipboard).
  - **Clean Background Auto-Wipe**: After `sensitive_wipe_s` (default 10s), the clipboard is cleared via bare `EmptyClipboard()`, preventing empty white placeholder cards in `Win+V`.
  - **Instant Supersession**: If any new clip is copied (non-sensitive text, another sensitive OTP, an image, or a local user copy) before the timer expires, the pending wipe is cancelled immediately.

For anything else, static regex patterns block transmission:

```json
"denylist_regex": ["BEGIN (RSA|OPENSSH) PRIVATE KEY", "^ghp_[A-Za-z0-9]{36}$"]
```

---

## Tests

```bash
python tests\test_core.py             # pure logic, dedup, outbox, continuity (under a second)
python tests\test_sensitive_sync.py   # sensitive sync, RAM outbox, clean wipe, supersession
python tests\test_android_node.py     # AndroidNode protocol, chunked transfers, ACK/DEFER
python tests\test_imaging.py          # PNG/DIBV5 image codecs, alpha preservation, 1080p speed
python tests\test_e2e.py              # laptop + phone, real daemon
python tests\test_three_device.py     # all three, real daemon (devices must be unlocked)
```


`test_three_device.py` asserts the guarantees that matter: exactly one device written per
laptop copy, exactly one application per device copy, no writes back into the originating
domain, and that mirrored clips never re-transfer their body.

---

## Troubleshooting

### "adb connect failed" after rebooting a device

Mostly does NOT happen in practice, once a device has paired Android's Wireless Debugging
(or Shizuku) with a remembered network at least once -- confirmed 2026-08-31 by a real phone
reboot: `service.adb.tcp.port` was back at 5555 within 5 minutes, zero cable touched. The
phone's adb keystore (`adb shell dumpsys adb`) shows a Shizuku-authorized key plus a
remembered Wi-Fi AP list, which is almost certainly why: that pairing survives reboot on the
OS side and reopens the wireless listener on its own, independent of `service.adb.tcp.port`
itself being a wiped-on-boot runtime property (that part of the explanation below is still
accurate, it is just not the whole story, and it is not something CopyClip or this repo
controls either way).

A device that has genuinely never been through that pairing -- freshly added, before its
first setup, like a new Pixel -- does need a manual re-arm. Do it once, with the device on
USB:

```bash
adb -d tcpip 5555
```

`adb tcpip 5555` only ever sets the *runtime* property `service.adb.tcp.port`, wiped on
reboot on its own (`persist.adb.tcp.port` stays empty, and making that persistent needs
root, off limits here) -- so without a Wireless Debugging/Shizuku pairing already in place,
each real reboot does need this redone. The daemon reconnects on its own either way once the
port is live. Each device reconnects independently, so one device rebooting never stops the
other from working.

### Nothing syncs, log shows `LOCK 1`

The device is locked. Documented above, not a bug. Writes queue and apply on unlock.

### Log says "Continuity has not propagated"

Samsung's phone-to-tablet sync is not working: check both devices are on the same Wi-Fi,
signed into the same Samsung account, with Bluetooth on. CopyClip has already failed over to
writing both devices directly, so sync keeps working meanwhile.

### A synced phone/tablet screenshot doesn't show up in Win+V, but pastes fine

Expected, by design, not a bug: `screenshot_exclude_from_history` (default `true`)
deliberately excludes device-synced images from Windows Clipboard History. Ctrl+V still
pastes the correct image — this only affects whether it's *listed* in Win+V. It exists
because a large synced image was found to evict the user's *other* Win+V entries, and
there's no known way to keep both (visible in Win+V and not evicting anything) — see
[bugs/BUG-005](bugs/BUG-005-duplicate-screenshot-push.md). Set the config key to `false` to
bring back Win+V visibility for synced screenshots and accept the eviction risk instead.

If you want the file itself, not just the clipboard/history: `screenshot_backup_dir` (see
[Configuration](#configuration) and [Images](#images) above) saves every synced screenshot
as a plain file regardless of this setting.

### General health check

```bash
python copyclip.py --status
```

Shows each device, the elected representative, Continuity health, and anything pending in
the outbox. Add `--log-level DEBUG` to the daemon to see every routing decision, or just
double-click `Start CopyClip (Debug).bat` for the same thing without typing the flag.

---

## Images

Copy an image on any device, paste it on any other — same mechanism as text, off by default.
`allow_images` in `config.json` is `false` until real-use confirmation of two open questions
from the pre-implementation device spike (see
[bugs/SPIKE-IMAGE-CLIPBOARD.md](bugs/SPIKE-IMAGE-CLIPBOARD.md) and
[ARCHITECTURE.md §6.6a](ARCHITECTURE.md#66a-image-clipboard-mediastore-under-app_process-and-why-it-needed-fakecontext)).
PNG/JPEG/GIF/BMP/WEBP travel byte-for-byte; only HEIC/HEIF/AVIF transcode to PNG. Images are
stored on-device under `Pictures/CopyClip` via MediaStore, and capped at `max_image_bytes`
(default 24 MiB), skipped and logged rather than truncated if oversized.

The laptop side has a counterpart: `screenshot_backup_dir` saves every image clip (local or
synced from a device) as a plain file too, same idea as the Android `Pictures/CopyClip`
store but Windows'-own-filename-style — a stopgap for a Windows 11 25H2 Snipping Tool bug,
off by default (empty string).

A device-synced image is also, by default, excluded from Windows Clipboard History
(Win+V) — `screenshot_exclude_from_history`. Ctrl+V still pastes it normally; it just
won't show up as its own entry in Win+V's list. This exists because a large synced image
was observed evicting the user's *other* Win+V entries, and there is no way to keep both
(visible in Win+V *and* not evicting anything) — see
[bugs/BUG-005](bugs/BUG-005-duplicate-screenshot-push.md) for the full investigation.

## Future scope

Designed for, deliberately not built. Full detail:
[ARCHITECTURE.md §12](ARCHITECTURE.md#12-future-scope-designed-for-not-built).

- **Files** via Taildrop handoff.
- **Clipboard history** with search.
- **More devices**, by adding to a domain or creating another one. The hub does not change.
