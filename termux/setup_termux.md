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

**The dex goes to `/data/local/tmp`, not into Termux's own home.** Confirmed live (not
theoretical): `app_process`, launched through `rish` (shell UID, what Shizuku grants), got
"Permission denied" trying to read a dex placed in Termux's private app directory
(`/data/data/com.termux/files/home`) — that directory is sandboxed per-app, and shell UID is
a *different* identity than Termux's own, so it can't read into it even via Shizuku.
`/data/local/tmp` is world-readable and is exactly where `transports/adb_transport.py`
already puts this same dex for Tier 1 — same convention, already proven working daily.

From the laptop:

```bash
python -c "import secrets;open(r'<repo>\copyclip.key','wb').write(secrets.token_hex(32).encode())"
```

```bash
adb -s 100.64.0.11:5555 push <repo>\bin\clipbridge.dex /data/local/tmp/clipbridge.dex
```

```bash
adb -s 100.64.0.11:5555 push <repo>\copyclip.key /sdcard/copyclip.key
```

The dex needs no further step — it's already shell-UID-readable exactly where it landed.
The HMAC key is different: `copyclip_node.py` itself (running as Termux's own UID, not
shell) reads it, so it belongs in Termux's home, not `/data/local/tmp`. In Termux:

```bash
cp /sdcard/copyclip.key ~/.copyclip.key && chmod 600 ~/.copyclip.key && rm /sdcard/copyclip.key
```

The key never leaves your devices. Removing it from `/sdcard` matters: that path is readable
by any app holding storage permission.

### 4a. TLS cert, only if you'll ever use the same-WiFi fallback (`lan_host`)

Tier 2 above relies on Tailscale's own tunnel for confidentiality. If you're setting up
`lan_host` (a same-WiFi fallback for when Tailscale itself is down — see
[ARCHITECTURE.md §9a](../ARCHITECTURE.md#9a-same-wifi-lan-fallback-tier-2--tls) and
[bugs/SPIKE-LAN-TLS-FALLBACK.md](../bugs/SPIKE-LAN-TLS-FALLBACK.md)), do this too — skip it
if you're only ever reaching this device over Tailscale.

One shared keypair, generated once on the laptop, reused for every device (same trust model
`copyclip.key` already uses — one shared secret, not per-device):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout copyclip_tls.key -out copyclip_tls.pem -subj "/CN=copyclip-lan-fallback"
```

Push both to each device, same channel as the HMAC key:

```bash
adb -s 100.64.0.11:5555 push copyclip_tls.pem /sdcard/copyclip_tls.pem
adb -s 100.64.0.11:5555 push copyclip_tls.key /sdcard/copyclip_tls.key
```

In Termux:

```bash
cp /sdcard/copyclip_tls.pem ~/.copyclip_tls.crt
cp /sdcard/copyclip_tls.key ~/.copyclip_tls.key
chmod 600 ~/.copyclip_tls.key
rm /sdcard/copyclip_tls.pem /sdcard/copyclip_tls.key
```

The laptop only ever needs the public `copyclip_tls.pem` (set as `config.json`'s
`tls_cert_file`) — never the private key. Keep `copyclip_tls.key` off the laptop entirely.

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

Expect `copyclip node listening on :8385`. If you did step 4a, it auto-detects the cert/key
files and prints `TLS enabled` instead of the HMAC-only warning; no extra flag needed with
the default filenames (`--cert-file`/`--tls-key-file` only matter if you used different
paths).

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

If you did step 4a and want the same-WiFi fallback, also add `lan_host` (its
router-DHCP-reserved LAN IP) to this device's entry in `devices[]`, and `tls_cert_file` at
the top level of `config.json` (outside `devices[]`, shared by every device):

```json
{ "name": "phone", "transport": "tcp", "tcp_host": "100.64.0.11", "tcp_port": 8385,
  "lan_host": "192.0.2.42", "enabled": true }
```

```json
"tls_cert_file": "copyclip_tls.pem"
```

Without `tls_cert_file` set, `lan_host` is simply never tried — `TcpTransport` only adds a
fallback candidate once TLS is configured (see ARCHITECTURE.md §9a for why: no Tailscale
tunnel means no confidentiality without it).

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

Confidentiality comes from Tailscale's WireGuard layer, when the peer is reached by its
Tailscale address. The HMAC above is about authorisation, not encryption, and keeps working
the same way regardless. **If you set up `lan_host` (step 4a, ARCHITECTURE.md §9a)**, there
is no Tailscale tunnel underneath that fallback path — confidentiality there comes instead
from `TcpTransport`'s own TLS wrap (`tls_cert_file`), which is why step 4a says to treat it
as required, not optional, once `lan_host` is in use. Without both `lan_host` and
`tls_cert_file` set, none of this changes anything — everything above still applies exactly
as before.

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
