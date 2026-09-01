# CopyClip: Setup Runbook

Step-by-step installation, written so a future session (human or AI) can set this up on a
fresh machine/device set without rediscovering anything, and without needing to ask
clarifying questions first. For *why* it's built this way, see
[ARCHITECTURE.md](ARCHITECTURE.md); for the quick reference and config knobs, see
[README.md](README.md). This is the one to follow start-to-finish.

Device names throughout (`phone`, `tab`) are examples, not requirements. Pick any short,
lowercase, no-space name per device, whatever you use in `config.json` is what shows up in
logs and in the Android app's "Device name" field, and the two must match exactly.

## Quick checklist

For "set up CopyClip on the laptop plus devices A, B, C":

1. Laptop, once: install Tailscale, build the dex (§1.1, §2.1), run `windows_app\tray.py`
   (§1.4), wire autostart (§1.4).
2. For each Android device, in order:
   a. Install Tailscale, sign into the same tailnet, note its Tailscale IP (§0).
   b. Pair Wireless Debugging once over USB (§2.2).
   c. Add it to `config.json`'s `devices[]` (§1.3).
   d. Verify a real round trip. Do this even for a device that can't survive a reboot
      unattended, §2 still works for it, just with a manual re-arm after each cold boot.
   e. **Optionally, on top of (d), not instead of it:** if that manual re-arm is a real
      annoyance for this device, §3 adds a one-tap manual push that works even in the gap
      before it's been re-armed.
3. If two or more devices already mirror each other outside CopyClip (e.g. two Samsung
   phones via Continuity), group them in `domains[]` (§1.3). Otherwise skip this, most
   setups have none.
4. `python copyclip.py --status` to confirm everything.

Everything below is the detail behind each step.

---

## What you're setting up

- **One laptop** running the hub (`windows_app/tray.py`), the brain, routes clips between
  everything else.
- **Most Android devices** (the common case, §2 below): **no app installed at all.**
  `ClipBridge.dex` runs under the shell UID over a plain `adb` pipe, spawned and torn down
  by the laptop. No shared key, no port config, no hub address on the device. This already
  survives a reboot once Wireless Debugging has been paired with your network once, see §2.
- **One exception** (§3 below): a device whose Android version predates persistent Wireless
  Debugging pairing (before Android 11) genuinely can't get that reboot survival no matter
  what. That device gets a real installed app, purely to add a manual "Send Clip" push as
  the fallback for when adb needs a manual re-arm. Don't reach for this tier unless a device
  actually fails the plain-adb path for this specific reason, it's real extra complexity
  (a shared key, Shizuku, a firewall rule) that most devices never need. Confirmed the hard
  way this session: it was tried on two devices that didn't need it, then reverted the same
  day, see ARCHITECTURE.md §8's "walked back off phone/tab" entry before assuming a device
  needs it.

All devices talk over **Tailscale**, install it and sign in to the same tailnet on every
device before starting.

### 0. Finding a device's Tailscale IP

Every step below needs this. Two ways:

- On the laptop, once every device is signed into Tailscale: `tailscale status` lists every
  device in the tailnet with its `100.x.x.x` address and hostname.
- On the Android device itself: open the Tailscale app, its own IP is shown on the main
  screen.

Devices are addressed by this raw IP throughout, never by hostname/MagicDNS.

---

## 1. Windows laptop

### 1.1 Prerequisites

- Python 3.13+ on `PATH` (or note its install dir, e.g. `C:\Python314`). Zero pip packages
  needed, this project uses raw `ctypes` deliberately, see [ARCHITECTURE.md](ARCHITECTURE.md).
- Tailscale installed and connected (`tailscale ip -4` should print a `100.x.x.x` address).
- This repo cloned/present locally.
- `adb` (Android platform-tools) on `PATH` or noted somewhere config can point at it.

### 1.2 The shared key

Only needed at all if some device will use §3's app tier. `copyclip.key` at the repo root is
the HMAC secret that device authenticates with. If it doesn't already exist:

```powershell
python -c "import secrets; open('copyclip.key','wb').write(secrets.token_hex(32).encode())"
```

Its raw file bytes (the hex-looking ASCII text) are used **as-is** as the key, never
hex-decoded on either end. See [Security notes](#security-notes) before copying it anywhere.

### 1.3 config.json

Copy/edit `config.json` at the repo root. Each device gets an entry in `devices[]`, name of
your choosing (§0 above). For the common, no-app case (§2):

```json
{
  "name": "<your-device-name>",
  "transport": "adb",
  "device": "<its-tailscale-ip>:5555",
  "enabled": true
}
```

**Even a device that can't do §2's reboot survival should still use this exact same
`enabled: true` entry.** Confirmed 2026-09-01: `pixel`, which needs a manual re-arm every
cold boot (§2.2's tell), runs perfectly well on this path the rest of the time, verified
live both directions. The only cost of no persistent pairing is that manual re-arm after a
reboot, not anything about the sync itself, don't leave a device `enabled: false` just
because it can't survive a reboot unattended, that setting means "don't even try", which is
strictly worse than "try, and it'll just queue and retry if this device happens to be
unreachable right now" (the daemon already handles that gracefully for any device, that's
what the reconnect backoff in ARCHITECTURE.md §6.7 is for). Only leave a device `enabled:
false` if you genuinely don't want CopyClip talking to it at all right now.

**Only add a `domains[]` group if devices already mirror each other outside CopyClip** (e.g.
two Samsung phones via Continuity, or a similar vendor-level sync). Most setups have zero
domains, every device is just standalone in `devices[]`:

```json
"domains": [
  { "name": "<group-name>", "members": ["<device-a>", "<device-b>"], "grace_s": 8.0, "fail_threshold": 3 }
]
```

If you group devices that *don't* actually mirror each other, CopyClip will only write to
one of them and the other will simply never receive anything, this only makes sense for
devices with a real, independent sync relationship already.

### 1.4 Run it

```powershell
python windows_app\tray.py
```

This starts the tray icon, the dashboard (right-click, "Open dashboard"), and **registers
itself in the HKCU Run key automatically**, no manual step needed for it to come back at
login on any machine. To take over from an already-running instance (e.g. after pulling code
changes): `python windows_app\tray.py --replace`.

**Start Menu shortcut** (optional, for manual launch/pinning, the Run key above already
covers login):

```powershell
powershell -File windows_app\create_shortcut.ps1
```

**Optional extra layer, only if this machine already runs the AHK automation framework at
`<software>\Programming\AutoHotKey\AllScripts\`** (this machine's own personal
infrastructure, not part of the CopyClip repo, and not present on a fresh machine, the Run
key above is already sufficient there): this framework's `RestartNamedPythonServer()` helper
adds crash-recovery and a boot-time force-relaunch (so an edited `.py` picks up on the
global reload hotkey) on top of the Run key. If setting this up fresh on this same kind of
machine, add:

In `local_paths.ahk`, a path variable and one `WATCHDOG_APPS` entry:

```autohotkey
PATH_COPYCLIP := "<repo>"
```

```autohotkey
, {name: "CopyClip_Python.exe", path: """<ahk-dir>\CopyClip_Python.exe"" ""<repo>\windows_app\tray.py"""}
```

(append that as one more element inside the existing `WATCHDOG_APPS := [ ... ]` array,
adjust the two paths if this repo or the AHK folder live somewhere else)

In `BackgroundAutomations.ahk`, one line in the auto-start section:

```autohotkey
RestartNamedPythonServer("CopyClip", PATH_COPYCLIP "\windows_app\tray.py",,, true)
```

If this line is ever found **commented out** (a leading `;`) on a machine that already has
this set up, that was a deliberate choice at some point, check with whoever owns the machine
before uncommenting rather than assuming it's a bug.

### 1.5 Verify

```powershell
python copyclip.py --status
```

Shows each configured device, reachability, and Continuity health if applicable.

---

## 2. Android device, the common case (no app)

### 2.1 Build the on-device bridge

Once per machine, and after any edit to `ClipBridge.java`:

```powershell
powershell -File android\build_dex.ps1
```

### 2.2 Pair Wireless Debugging once

This is what makes the connection survive a reboot without an app or root. With the device
on USB:

```bash
adb -d tcpip 5555
```

Then, on the device, Settings, Developer options, Wireless debugging, turn it on and pair
with this network. If Developer options isn't visible: Settings, About phone, tap "Build
number" 7 times. Once paired, a real reboot has been confirmed (2026-08-31) to bring wireless
adb back within about 5 minutes on its own, no cable, no app, needed again. This needs
doing once per device, not once per setup.

**If the device is pre-Android-11, this pairing won't survive a reboot** (that OS feature
doesn't exist yet on it), and it'll need `adb -d tcpip 5555` re-armed over USB after every
cold start instead. **Use this section anyway, don't skip to §3**, confirmed 2026-09-01 on
exactly this kind of device: everything else here works identically, the manual re-arm is
the only real difference. Plan for needing it every time; a clean reboot test the same day
saw wireless adb come back on its own with no re-arm run by anyone, cause still not
identified (a MacroDroid task was suspected and ruled out directly with the device's owner,
see ARCHITECTURE.md §8 for what's still an open candidate), so treat that as a possible
bonus, not something to rely on.
A tiny double-click `.bat` wrapping `adb -d tcpip 5555` then `adb connect <ip>:5555` makes
that one step painless on the times it is needed, keep it wherever you keep this
device's other tools, it doesn't need to live in this repo.

### 2.3 Add it to config.json (§1.3) and verify

Use `"enabled": true` regardless of whether this device can survive a reboot unattended,
`enabled: false` means "don't even try", which is worse than just letting the daemon's own
reconnect backoff (ARCHITECTURE.md §6.7) handle a device that's briefly unreachable, exactly
like it already does for any device that's merely offline. `python copyclip.py --status`
should list it as connected. Copy something on each end and confirm it appears on the other.
Nothing to install on the device itself, ever.

**What actually happens while this device is unreachable** (e.g. between a reboot and
re-arming it), if it also has a separate tool like Sefirah paired to the laptop: copies
*from* other devices still arrive, but not through this daemon, the content reaches the
laptop first through this daemon's own already-working link to whatever device it came
from, then the separate tool relays it onward from the laptop's own clipboard as an
independent second hop. Copies *from* this device have no such fallback: nothing else in
this setup can complete that direction, only this daemon can, so a manual push (§3.4) is
the only way out until it's reachable again. Confirmed live 2026-09-01, see ARCHITECTURE.md
§2's own update note for the full breakdown.

---

## 3. Optional extra: "Send Clip" manual push (any device, but built for one that fails §2)

Not a replacement for §2, an addition on top of it. Do §2 first regardless, even for a
device with no reboot survival, then come here only if you also want a one-tap manual push
that works without needing this device to be currently re-armed/reachable at all, useful
specifically for the moments a pre-Android-11 device hasn't been USB-re-armed since its
last reboot yet.

### 3.1 Install and pair Shizuku

1. Install Shizuku from the Play Store (or F-Droid).
2. Pair it via Wireless debugging the same way as §2.2, then start the Shizuku service from
   within the app.
3. **Check whether Shizuku itself is offering root or shell mode.** If it's rooted (Magisk),
   the app's automatic background bridge will correctly refuse to use it, see
   [ARCHITECTURE.md](ARCHITECTURE.md), this project never runs privileged code as root. That's
   expected on some devices, not a bug, skip straight to §3.3 (manual push still works fine
   without the automatic bridge).

### 3.2 Install the CopyClip app

Build the APK (`android_app/`, package `com.mohitarora.copyclip`):

```powershell
cd android_app
.\gradlew.bat assembleDebug
```

Output: `android_app\app\build\outputs\apk\debug\app-debug.apk`. Install it:

```bash
adb install -r android_app\app\build\outputs\apk\debug\app-debug.apk
```

**If the build fails with a Kotlin/JDK version error** (`IllegalArgumentException` parsing a
JDK version), a newer system-wide JDK is shadowing the one this toolchain expects.
`android_app\gradle.properties` already pins `org.gradle.java.home` to Android Studio's own
bundled JDK for exactly this reason, if that path doesn't exist on this machine, point it at
any JDK 21 install instead (JDK 25+ is currently too new for this project's Kotlin compiler
version).

### 3.3 Configure the app

Open CopyClip. In the **Pairing** card, fill in all four fields, then tap **Save**:

- **Shared key (hex)**: the exact contents of the laptop's `copyclip.key` file, pasted in.
- **Port**: this device's own bridge listen port (default `8386`, fine to leave as-is unless
  it collides with something else on this device).
- **Hub address**: the laptop's Tailscale IP (§0).
- **Device name**: must match this device's `"name"` in the laptop's `config.json` (§1.3)
  exactly, case-sensitive.

If Shizuku offered shell mode (§3.1), also grant it access when prompted after saving.

### 3.4 "Send Clip" (this device to everyone else)

A **Send Clip** button lives both in-app and as an action on the persistent notification.
Tapping it reads this device's current clipboard, text or a PNG/JPEG/GIF/BMP/WEBP image
(works with zero special permission, being genuinely in focus is the one exemption
Android's background-clipboard restriction always grants), and pushes it straight into the
laptop's routing pipeline, same dedup/domain-awareness as an automatic sync. Requires
§3.5's firewall rule and §3.3 fully filled in. Sent as one frame, not chunked, fine for a
normal screenshot, an unrecognized image format is reported as a failure rather than
silently converted.

### 3.5 Firewall rule for "Send Clip"

Once, on the laptop, regardless of how many §3-tier devices there are. The laptop has
otherwise never needed to *accept* an inbound connection for CopyClip, every other path has
the hub dialing out, so Windows Firewall blocks this one by default, silently (the device
sees a timeout, not a clear rejection). Run **as Administrator**:

```powershell
New-NetFirewallRule -DisplayName "CopyClip Send-Clip inbound" -Direction Inbound -Protocol TCP -LocalPort 8387 -Action Allow
```

### 3.6 The app's own automatic bridge (separate from §2, usually not worth relying on)

The app's own Shizuku-hosted background bridge only works if Shizuku offered shell mode
(§3.1); on a rooted Shizuku it stays off by design, permanently, no amount of re-arming
fixes that. Even when Shizuku *is* shell mode, prefer §2 for this device's actual automatic
sync, it's simpler (no app dependency) and already proven, this bridge exists mainly as a
historical first attempt. Restarting it after reboot needs Shizuku manually reopened with
the device on USB (`adb -d tcpip 5555` after Shizuku is running), the same physical step §2
already needs anyway for a device without persistent pairing, so there's no *extra* cost to
also having it running if it happens to work on your device, just don't depend on it being
the only thing keeping sync alive.

---

## Security notes

- `copyclip.key`'s raw bytes are the one secret §3's tier depends on. On the Android app
  it's stored in ordinary `Context.MODE_PRIVATE` SharedPreferences, genuinely private on a
  non-rooted device (enforced by the OS's own per-app sandboxing, not just a promise), but
  **readable by root** on any device that has it, same as any other app's private data. A
  leaked key alone isn't enough to do anything: whoever has it also needs live access to your
  Tailscale mesh, since payload transport is already inside Tailscale's own encrypted tunnel
  and the HMAC layer is authentication/integrity on top of that, not the only barrier. If a
  key is ever suspected compromised, regenerate it (§1.2) and repaste on every device that
  uses it, no code change needed.
- §2 (the common case, no app) has no shared key at all, its security comes entirely from
  Tailscale's own tunnel and adb's own RSA-key authorization, exactly as before this project
  existed, see [ARCHITECTURE.md §9](ARCHITECTURE.md#9-security--trust-model).
- The firewall rule in §3.5 opens exactly one TCP port, gated behind the same mutual-HMAC
  handshake the app tier always uses (`transports/framing.py`), an unauthenticated
  connection gets `AUTH-FAIL` and nothing else.

## Battery / background cost

§2 (the common case) has zero installed-app cost, it's the same `ClipBridge.dex` polling
workload that ran before this whole runbook existed, kept alive by the laptop's own adb
session, nothing new on the device's own battery accounting. §3's app tier runs a foreground
service (a visible low-priority notification is the trade-off) so Android's Doze/App-Standby
won't suspend it, that's the entire point of using it at all, and also its entire real cost:
the underlying poll workload is identical either way, the delta is specifically the OS no
longer being free to defer it. Not independently benchmarked in mAh; check Settings, Battery,
Battery Usage after a day of real use for actual numbers on a device that uses §3. The
laptop side is unaffected regardless of which tier any device uses, the tray app runs the
exact same `core/hub.py` daemon that would otherwise run as a bare script, plus a
near-zero-cost tray icon message loop.

## Troubleshooting

See [README.md](README.md#troubleshooting) for wire-level issues (locked device, Continuity
degraded, etc.). For anything §3/Shizuku-specific, see `bugs/SPIKE-ANDROID-USERSERVICE.md`
and `bugs/SPIKE-PIXEL-ACCESSIBILITY.md`.
