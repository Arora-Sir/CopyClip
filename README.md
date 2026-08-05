# CopyClip

Instant bidirectional clipboard sync between **predator-helios-300** (Windows 11) and
**s24-ultra** (Galaxy S24 Ultra, Android 16) over Tailscale.

Copy on either device, paste on the other. No app installed on the phone, no cloud, no
account, no pip dependencies.

## Measured performance

Real numbers from this machine and phone, not estimates:

| Payload | Median | Best |
| :--- | ---: | ---: |
| 60 B | 103 ms | 23 ms |
| 2 KB | 22 ms | 16 ms |
| 50 KB | 110 ms | 34 ms |
| 93 KB round trip | 163 ms | byte exact |

Largest verified clip: **93,247 characters**, byte exact, including emoji, Devanagari,
tabs and CRLF. Default cap is 800,000 characters.

---

## The one thing you must know

**Android denies all clipboard access while the phone is locked.** Not the screen being
off, the *lock*. This is measured, not theoretical: with the keyguard up, `getPrimaryClip`
returns null and `setPrimaryClip` is accepted and then silently discarded, with no error
raised anywhere.

CopyClip handles this rather than losing data:

- **Phone to laptop** is unaffected in practice. If you are copying something on the phone,
  the phone is unlocked by definition.
- **Laptop to phone** while the phone is locked gets **queued on the phone** and applied the
  moment you unlock. The log shows `phone deferred write (locked); will apply on unlock`.

So: copy a link on the laptop with your phone in your pocket, pull the phone out, unlock,
and it is already on the clipboard.

---

## Setup

Everything needed is already installed on this machine. Nothing to `pip install`.

```bash
cd <repo>
```

**1. Build the on-device bridge** (once, and after any edit to `ClipBridge.java`):

```bash
powershell -File android\build_dex.ps1
```

**2. Check everything is reachable:**

```bash
python copyclip.py --status
```

**3. Run it:**

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

The phone runs **no installed app**. `ClipBridge.dex` is executed under the **shell UID**
via `app_process`, which is the only non-root identity Android grants background clipboard
access to (`appops` confirms `com.android.shell READ_CLIPBOARD: allow`). This is the same
mechanism scrcpy uses.

```
Windows clipboard                                  Android clipboard
      |                                                    ^
      v                                                    |
[ ctypes AddClipboardFormatListener ]      [ ClipBridge.dex, shell UID ]
      |                                                    ^
      +---------> copyclip.py <--- one persistent pipe -----+
                                   adb shell -T over Tailscale
```

Design points that are load bearing:

- **One long-lived pipe**, not a process per copy. Spawning `adb shell` each time costs
  200 to 400 ms and would defeat the whole point.
- **`adb shell -T`** disables PTY allocation. Without it adb rewrites `\n` as `\r\n` and
  silently corrupts every multi-line clip.
- **Base64 on the wire**, so newlines, tabs, emoji and Devanagari survive intact.
- **Version-adaptive reflection.** `IClipboard` method signatures have changed repeatedly
  (attribution tag in Android 11, device id in Android 14), so overloads are resolved at
  runtime instead of being hardcoded. scrcpy issue #4285 is the cautionary tale.
- **Poll `ClipDescription.getTimestamp()`, not `addPrimaryClipChangedListener`.** The
  listener needs a hidden AIDL stub and breaks outright on Android 14. Polling a
  description is cheap and never transfers content until something actually changed.
- **Loop guard on both sides.** A clip is never sent back towards where it came from.
  This matters doubly here because Samsung Continuity independently syncs this phone with
  the Tab S10 Ultra.

---

## Configuration

`config.json`:

| Key | Default | Meaning |
| :--- | :--- | :--- |
| `device` | `100.64.0.11:5555` | Phone's Tailscale IP and ADB port. Raw IP on purpose, never MagicDNS |
| `max_chars` | `800000` | Oversized clips are **skipped and logged**, never truncated |
| `sync_laptop_to_phone` | `true` | Direction toggle |
| `sync_phone_to_laptop` | `true` | Direction toggle |
| `adopt_phone_clip_on_connect` | `false` | When false, the first clip after connecting is a baseline only, so starting the daemon never clobbers your laptop clipboard |
| `denylist_regex` | `[]` | Clips matching any pattern are never synced |
| `reconnect_max_s` | `30` | Reconnect backoff ceiling |
| `log_level` | `INFO` | Set `DEBUG` to see every frame |

### Not syncing secrets

Clips that the source app flags with Android's `EXTRA_IS_SENSITIVE` (password managers do
this) are **dropped, never transmitted**. For anything else, use `denylist_regex`, for
example:

```json
"denylist_regex": ["BEGIN (RSA|OPENSSH) PRIVATE KEY", "^ghp_[A-Za-z0-9]{36}$"]
```

---

## Troubleshooting

### "adb connect failed" after rebooting the phone

Expected. Wireless ADB does not survive a reboot on this device: `service.adb.tcp.port`
is set but `persist.adb.tcp.port` is not, and making it persistent requires root.

Re-arm it (phone connected by USB once):

```bash
adb -d tcpip 5555
```

Then CopyClip reconnects on its own. To avoid this permanently, use the Tier 2 transport
(see `termux/setup_termux.md`), which runs over Shizuku and survives reboots.

### Nothing syncs, log shows `LOCK 1`

The phone is locked. This is the documented constraint above, not a bug. Laptop-to-phone
writes are queued and applied on unlock.

### Clips from the laptop do not arrive

```bash
python copyclip.py --status
```

Checks the dex, the Windows clipboard, the ADB link, and the phone's clipboard in one shot.

### Images do not sync

v1 is text only by design. Non-text clips are detected and skipped cleanly, never mangled.
See "Future scope".

---

## Layout

```
copyclip.py             daemon: clipboard watcher, loop guard, reconnect supervisor
winclip.py              Windows clipboard via raw ctypes (no pywin32)
config.json             settings
transports/             pluggable channel (Tier 1 adb today, Tier 2 Termux next)
android/ClipBridge.java on-device helper source, readable and rebuildable
android/build_dex.ps1   javac + d8 -> bin/clipbridge.dex
bin/clipbridge.dex      build output
tests/test_e2e.py       full-stack test, both directions
otherCopyProjects/      reference clones, see its NOTES.md
logs/                   rotating logs
```

Nothing here is an opaque blob. `bin/clipbridge.dex` is built from
`android/ClipBridge.java` by a script in this folder; rebuild it yourself any time.

---

## Future scope

Designed for, deliberately not built yet. The wire protocol already carries a MIME type and
chunked binary framing specifically so these do not require a rewrite.

- **Image sync.** Windows `CF_DIB` to Android needs a MediaStore insert to obtain a
  `content://` URI for `ClipData.newUri()`. A `file://` URI throws
  `FileUriExposedException`, which is the trap most implementations fall into.
- **File sync** via Taildrop handoff.
- **Clipboard history** with search.
- **Tab S10 Ultra as a third node.** Needs explicit loop arbitration because Samsung
  Continuity already syncs it with the phone.
