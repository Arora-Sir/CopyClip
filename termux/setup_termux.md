# Tier 2 setup: Termux + Shizuku (reboot-proof)

**Status: written and reviewed, not yet verified end to end.** It needs phone-side installs
that only you can do. Tier 1 (ADB) is tested and working; this is the upgrade that removes
the reboot problem.

## Why bother

Measured on your phone:

```
[service.adb.tcp.port]: [5555]      <- set
persist.adb.tcp.port                 <- ABSENT
```

Wireless ADB therefore dies on every reboot, and making it persistent needs root, which is
off limits. Shizuku survives reboots, so this tier reaches ClipBridge through `rish` instead
of `adb` and keeps working after the phone restarts.

Same `clipbridge.dex`, same protocol. Only the pipe changes.

---

## One-time setup

### 1. Install the missing Termux add-ons

You have Termux (`com.termux`) but not these. Install from **F-Droid or GitHub, matching the
source you installed Termux from** (Play Store and F-Droid builds use different signing keys
and will refuse to talk to each other):

- **Termux:Boot** so the node starts after a reboot

### 2. Termux packages

```bash
pkg update && pkg install python
```

### 3. Export `rish` from Shizuku

In the Shizuku app: **Use Shizuku in terminal apps**, then follow the on-screen steps to
export `rish` and `rish_shizuku.dex` into Termux's home directory (`~`).

Verify it gives you shell UID:

```bash
sh ~/rish -c id
```

Expect `uid=2000(shell)`. If it errors, Shizuku is not running; start it and retry.

### 4. Copy the bridge and a shared key onto the phone

From the laptop:

```bash
python -c "import secrets;open(r'<repo>\copyclip.key','wb').write(secrets.token_hex(32).encode())"
```

```bash
adb -s 100.64.0.11:5555 push <repo>\bin\clipbridge.dex /sdcard/clipbridge.dex
```

```bash
adb -s 100.64.0.11:5555 push <repo>\copyclip.key /sdcard/copyclip.key
```

Then in Termux:

```bash
cp /sdcard/clipbridge.dex ~/clipbridge.dex && cp /sdcard/copyclip.key ~/.copyclip.key && chmod 600 ~/.copyclip.key && rm /sdcard/copyclip.key
```

The key never leaves your devices. Removing it from `/sdcard` matters: that path is readable
by any app holding storage permission.

### 5. Copy the node script

```bash
adb -s 100.64.0.11:5555 push <repo>\termux\copyclip_node.py /sdcard/copyclip_node.py
```

In Termux:

```bash
cp /sdcard/copyclip_node.py ~/copyclip_node.py
```

### 6. Start it

```bash
python ~/copyclip_node.py
```

Expect `copyclip node listening on :8385`.

### 7. Autostart after reboot

```bash
mkdir -p ~/.termux/boot
```

Create `~/.termux/boot/copyclip` containing:

```sh
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
python ~/copyclip_node.py >> ~/copyclip_node.log 2>&1
```

```bash
chmod +x ~/.termux/boot/copyclip
```

Shizuku must also be running for `rish` to work. On Android 11+ it can be restarted from the
Shizuku app over wireless debugging without a PC.

---

## Point the laptop at Tier 2

In `config.json`:

```json
"transport": "tcp",
"tcp_host": "100.64.0.11",
"tcp_port": 8385,
"key_file": "copyclip.key"
```

Then:

```bash
python copyclip.py --status
```

Switch back any time with `"transport": "adb"`.

---

## Security

The socket listens on all interfaces because Android routes Tailscale traffic through a VPN
interface, so binding only to the Tailscale address is unreliable. That means any device on
your tailnet could *reach* the port, which is exactly why the protocol is authenticated:

- **Mutual challenge-response** on connect. Both ends prove they hold the key before any
  clipboard data moves, so neither a rogue client nor an impostor server gets anything.
- **Per-frame HMAC-SHA256** with a strictly increasing sequence number inside the MAC, so
  frames cannot be forged, replayed, or reordered.

Confidentiality comes from Tailscale's WireGuard layer. The HMAC is about authorisation
inside the tailnet, not encryption.

---

## Troubleshooting

**`rish: uid=10xxx` instead of `2000`**
Shizuku is not running, or `rish` was exported before Shizuku started. Restart Shizuku and
re-export.

**Node starts, laptop cannot connect**
Check the phone is up on the tailnet: `tailscale status` on the laptop. Connect by raw
Tailscale IP, never MagicDNS (you have a history of NRPT corruption breaking name
resolution while raw IPs keep working).

**Works, then stops after the screen is off a while**
Add `termux-wake-lock` (already in the boot script) and exempt Termux from battery
optimisation in Android settings.

**`DEFER set locked` in the log**
Not a fault. Android denies clipboard access while the phone is locked; the write is queued
and applied on unlock. See the main README.
