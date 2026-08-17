# CopyClip

One clipboard across **predator-helios-300** (Windows 11), **s24-ultra** (Galaxy S24 Ultra)
and **tab-s10-ultra** (Galaxy Tab S10 Ultra), over Tailscale.

Setup and quick reference below. For the full architecture, wire protocol, every tuning
knob and why it's set the way it is, and postmortems of real bugs found in production, see
[ARCHITECTURE.md](ARCHITECTURE.md).

Copy anywhere, paste anywhere. No app installed on either Samsung device, no cloud, no
account, no pip dependencies.

```
copy on laptop  ->  phone and tablet both have it
copy on phone   ->  laptop has it   (tablet already did, via Samsung)
copy on tablet  ->  laptop has it   (phone already did, via Samsung)
```

## Measured performance

Real numbers from these devices, not estimates.

| Payload | Median | Best |
| :--- | ---: | ---: |
| 60 B | 103 ms | 23 ms |
| 2 KB | 22 ms | 16 ms |
| 50 KB | 110 ms | 34 ms |
| laptop to tablet (via Samsung) | ~2.5 s | 2.0 s |

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

Everything needed is already on this machine. Nothing to `pip install`.

**1. Build the on-device bridge** (once, and after any edit to `ClipBridge.java`):

```bash
powershell -File android\build_dex.ps1
```

**2. Check everything is reachable:**

```bash
python copyclip.py --status
```

**3. Run it** (or just double-click `Start CopyClip.bat`, or the Desktop shortcut):

```bash
powershell -File start_copyclip.ps1
```

**4. Optional, start automatically at logon:**

```bash
powershell -File install_autostart.ps1
```

Undo with `uninstall_autostart.ps1`. This is the only step that changes anything outside
this folder.

---

## How it works

Neither Samsung device runs an installed app. `ClipBridge.dex` executes under the **shell
UID** via `app_process`, which is the only non-root identity Android grants background
clipboard access to (`appops` confirms `com.android.shell READ_CLIPBOARD: allow`). Same
mechanism scrcpy uses.

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
| `devices[]` | `name`, `transport` (`adb` or `tcp`), `device` (Tailscale IP and port), `enabled`; `tcp`-transport devices can also set `lan_host`, a same-WiFi fallback address tried when Tailscale itself is unreachable — see [ARCHITECTURE.md §9a](ARCHITECTURE.md#9a-same-wifi-lan-fallback-tier-2--tls) |
| `tls_cert_file` | Pinned self-signed cert (PEM) that turns on TLS for Tier 2, required before `lan_host` is safe to use (no Tailscale tunnel to rely on otherwise). `null` (default) keeps Tier 2 exactly as before |
| `tcp_primary_timeout_s` | How long the first Tier 2 connection candidate is given before trying `lan_host`. Default `5.0` |
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
| `screenshot_exclude_from_history` | Whether a device-synced image is excluded from Windows Clipboard History (Win+V) so it can't evict your other history entries. Default `true`. Ctrl+V still pastes it correctly either way — this only affects whether it's *listed* in Win+V. `false` brings back Win+V visibility for synced screenshots, at the cost of the eviction problem this exists to solve — see [bugs/BUG-005](bugs/BUG-005-duplicate-screenshot-push.md) |

Devices are addressed by **raw Tailscale IP, never MagicDNS**, which sidesteps NRPT
resolution breaking.

### Not syncing secrets

Clips the source app flags with Android's `EXTRA_IS_SENSITIVE` (password managers do this)
are **dropped, never transmitted**. For anything else:

```json
"denylist_regex": ["BEGIN (RSA|OPENSSH) PRIVATE KEY", "^ghp_[A-Za-z0-9]{36}$"]
```

---

## Tests

```bash
python tests\test_core.py           # pure logic, no devices, under a second
python tests\test_e2e.py            # laptop + phone, real daemon
python tests\test_three_device.py   # all three, real daemon (devices must be unlocked)
```

`test_three_device.py` asserts the guarantees that matter: exactly one device written per
laptop copy, exactly one application per device copy, no writes back into the originating
domain, and that mirrored clips never re-transfer their body.

---

## Troubleshooting

### "adb connect failed" after rebooting a device

Expected, and it is not a network problem: Tailscale auto-starts and the device stays
pingable. What dies is adbd's TCP listener. `adb tcpip 5555` only sets the *runtime*
property `service.adb.tcp.port`, which is wiped on boot, and `persist.adb.tcp.port` is empty
on both devices.

Re-arm with the device on USB once:

```bash
adb -d tcpip 5555
```

The daemon reconnects on its own. Each device reconnects independently, so one device
rebooting never stops the other from working.

(The tablet has Android's Wireless debugging enabled, which *does* survive reboot, but that
mode uses a random mDNS-advertised port and mDNS does not cross Tailscale.)

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
