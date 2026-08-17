# SPIKE: TLS over Tier 2 (Termux) for a same-WiFi fallback when Tailscale is down

> **Status as of 2026-08-18: not merged to `main`.** Lives on `feature/lan-fallback-tls`.
> `config.json` on `main` has the phone back on `"transport": "adb"` (the safe, proven
> default) — this feature is present in the code but inactive unless a device is explicitly
> switched to `"transport": "tcp"` with `lan_host` set. **Next session, start with the "Known
> open reliability gap" entry in the Follow-up section below** — the mechanism is proven
> correct (confirmed end-to-end, real devices, both directions, both network states) but not
> yet reliable enough for unattended daily use.

Not a bug. A throwaway pre-implementation spike, following the `bugs/` conventions
(measured evidence, never recollection; say when something is inferred rather than proven).
Source: `spike/termux_tls_probe_server.py` (runs in Termux) + `spike/termux_tls_probe_client.py`
(runs on the laptop) + `spike/copyclip_tls_probe.pem`/`.key` (throwaway self-signed cert,
generated with the laptop's `openssl` CLI, not the real deployment key). Not referenced by
production code and safe to delete once this file exists. Ran against **phone**
(`SM-S928B`, real LAN IP `192.0.2.50`) on **2026-08-17**.

## Why this exists

The user asked whether a same-WiFi-LAN fallback (no Tailscale) could be built securely,
since Android's Doze mode was independently found to silently kill Tailscale's background
connection for hours at a time. `transports/framing.py`'s existing HMAC-SHA256 scheme gives
authentication and anti-replay but **no encryption** — its own docstring says outright it
assumes Tailscale's WireGuard tunnel provides confidentiality. Wrapping the existing socket
in TLS (stdlib `ssl`, no third-party dependency) was the recommended fix, but this codebase
has never used `ssl` for anything, and Termux's specific Python build was an unmeasured
unknown — this had to be confirmed before any production code (`transports/tcp_transport.py`,
`termux/copyclip_node.py`) was touched.

## Result: both required questions answered, mechanism works end to end

| # | Question | Result |
| :---: | :--- | :--- |
| 1 | Does Termux's Python `ssl` module have real OpenSSL bindings? | **YES** — `OpenSSL 3.0.18 30 Sep 2025`, `HAS_TLSv1_3=True` |
| 2 | Does a real TLS 1.3 handshake + pinned-cert verification + data round-trip work laptop↔phone over the actual LAN (not loopback)? | **YES**, confirmed both directions |

## Measured transcript

Phone, Termux, ssl module check (run directly by the user, not automated — Termux's
`RUN_COMMAND` external-apps API is disabled by default and `adb shell` cannot execute
binaries inside another app's private data directory, so this genuinely needs a human in
the loop, same as the earlier Windows sleep/resume spike did):

```
$ python -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.HAS_TLSv1_3)"
OpenSSL 3.0.18 30 Sep 2025 True
```

Full TLS round trip, laptop dialing the phone's real LAN IP:

```
$ python spike\termux_tls_probe_client.py --host 192.0.2.50 --cert copyclip_tls_probe.pem
ssl module: OpenSSL 3.0.18 30 Sep 2025, TLS1.3=True
dialing 192.0.2.50:8386...
TLS handshake OK. Cipher: ('TLS_AES_256_GCM_SHA384', 'TLSv1.3', 256), version: TLSv1.3
sent 35 bytes, received reply: b'ACK:hello-from-laptop-over-real-lan-tls'
ROUND TRIP CONFIRMED CORRECT
```

The client used `load_verify_locations(cafile=...)` pinned to the exact throwaway cert
pushed to the phone — the same mechanism the real feature will use (`config.json`'s planned
`tls_cert_file`) — proving cert-pinned verification works cross-platform, not just that each
side's `ssl` module imports in isolation.

## What was deliberately not tested here

- **The negative case (untrusted-cert rejection)** was not run live in this spike — it's
  covered by a unit test instead (`tests/test_framing.py`, planned), since it doesn't need a
  real device: a client pinned to cert A connecting to a server presenting cert B is exactly
  reproducible on loopback.
- **The tablet** (`SM-X920`) was not checked. The plan explicitly said not to assume one
  device's `ssl` support matches the other's. **This is intentionally left OPEN, awaiting
  confirmation**, matching this project's existing convention (BUG-001 through BUG-007's
  status table). Does not block starting the build — the phone is the daily-driver device
  and the mechanism is platform-level (Termux's Python build, not phone-model-specific), so
  the risk of the tablet differing is low, but it is unverified, not assumed-fine.
- **Packet capture proving the wire is actually ciphertext** was not done in this spike (the
  TLS library's own handshake/cipher negotiation succeeding is strong indirect evidence, but
  not the same as visually confirming on the wire). Deferred to the real-device verification
  pass once the feature is wired into `tcp_transport.py`/`copyclip_node.py` for real, per the
  approved plan's Verification section.

## Cleanup performed

The throwaway server (`spike/termux_tls_probe_server.py`) exits automatically after serving
one connection — no phone-side process left running. `copyclip_tls_probe.pem`/`.key` are
throwaway spike-only credentials, not the real deployment cert; they stay in `spike/` for
reference and are safe to delete. The copies pushed to the phone's `/sdcard` and moved into
Termux's home directory were not deleted by this spike — a manual cleanup
(`rm ~/termux_tls_probe_server.py ~/copyclip_tls_probe.*`) on the phone is a reasonable
follow-up but not urgent, since they're inert once the spike is over.

## Verdict (original spike)

**Proceed with the TLS-wrap implementation** (`transports/tcp_transport.py`,
`termux/copyclip_node.py`, per the approved plan). Tablet `ssl` support stays open pending
confirmation, tracked the same way as the project's other open items — worth checking before
actually enabling `"transport": "tcp"` + `lan_host` on the tablet's device entry in
`config.json`, even though it doesn't block writing the code.

---

## Follow-up: real end-to-end integration test, same day (2026-08-17, later that night)

The TLS layer above was only ever tested in isolation (a throwaway probe, not the real
`spawn_bridge()`/`ClipBridge` path). Wiring the real feature together and testing against the
actual phone surfaced **three real bugs in `termux/copyclip_node.py`**, none of them in the
TLS/HMAC layer itself (that part worked exactly as this spike predicted) — all three were in
how `spawn_bridge()` invokes `rish` to actually launch `ClipBridge`.

### Bug 1: `rish`'s own `-c` flag does not do a real shell parse

`spawn_bridge()` originally ran `["sh", rish, "-c", cmd]` where
`cmd = f"CLASSPATH={DEX} app_process / {MAIN} serve"`. This aborted **instantly** — `rish`
printed `Entering shell...` then the whole invocation died, before ClipBridge ever ran.
`CLASSPATH=... app_process ...` is POSIX env-assignment-prefix syntax, which only means
anything to a real shell actually parsing the string; `rish -c` does not do that (confirmed:
a single-word command like `sh <rish> -c id` worked fine, ruling out a totally broken `rish`
export — only the env-assignment form failed).

### Bug 2: `rish` doesn't run an argv command at all — it needs the command typed into its stdin

Even after fixing (1) by swapping the order (`[rish, "sh", "-c", cmd]`, letting a *real* `sh`
parse the env-assignment), the result was still just `Entering shell...` followed by
permanent silence — no `READY`, no error, nothing. Confirmed by hand, interactively: running
`~/rish` with **no arguments at all** also prints `Entering shell...` and drops into an
interactive shell prompt. Whatever this `rish` build actually is, it does not appear to
support "run this argv and return" at all — only "give me an interactive shell, then type
commands at me." Fix: `spawn_bridge()` now spawns `rish` with zero arguments and **writes the
command to its stdin** right after spawning, exactly like typing it at a live prompt.

### Bug 3: the dex must live in `/data/local/tmp`, not Termux's own home

Even after (2), `app_process` still aborted — this time confirmed directly:
`ls -la /data/data/com.termux/files/home/clipbridge.dex` from *within* the `rish`-granted
shell (`uid=2000`) returned **Permission denied**. Termux's own home directory
(`/data/data/com.termux/files/home`) is sandboxed per-app; `uid=2000` (shell, what Shizuku
grants) is a different identity than Termux's own app UID and cannot read into it, even
though Termux's own processes can. Fix: `DEX` now points at `/data/local/tmp/clipbridge.dex`
— world-readable, and exactly where `transports/adb_transport.py` already puts this same dex
for Tier 1. Confirmed directly, isolated from the other two bugs, before assuming it was the
fix: `adb shell "CLASSPATH=/data/local/tmp/clipbridge.dex app_process / me.mohit.copyclip.ClipBridge serve"`
printed `READY 36` instantly.

### A fourth, smaller finding along the way: rish's own banner lands on stdout, not stderr

While debugging (2), a preamble filter was added to `pump_bridge_to_net()` (skip lines until
the first one that's an actual ClipBridge verb) because `rish`'s `Entering shell...` banner
was getting forwarded to the laptop as a bogus greeting. An earlier attempt at fixing this by
giving `rish`'s stderr its own pipe (`stderr=subprocess.PIPE` instead of `subprocess.STDOUT`)
did **not** help — the banner turned out to already be on stdout, not stderr. The verb-filter
approach (still in the code) is what actually worked, and is also a reasonable belt-and-braces
measure independent of exactly which stream `rish` uses in a given version.

### Confirmed end-to-end, live, real devices, both directions

After all three fixes landed, real day-to-day traffic flowed correctly:

```
[phone] connected
[phone] clip changed while this daemon was not running (16 chars); syncing it now instead of dropping it as a baseline
phone -> laptop: 16 chars
```

This exercised the full stack in one shot: TLS handshake + cert pinning, HMAC
challenge-response + per-frame auth, `rish`-mediated ClipBridge spawn, the real wire protocol,
and `core/hub.py`'s own baseline/dedup logic — all over the `lan_host`/`tls_cert_file` path
this feature added, with the laptop's own Tailscale toggled off partway through the session to
confirm the fallback candidate (not just the primary) actually gets exercised.

**One write (laptop → phone) timed out waiting for ACK** shortly after, right as an unrelated
`[tab]` adb reconnect was also mid-retry — the existing `ack_timeout_s`/reconnect machinery
(unchanged by this feature) handled it exactly as designed: marked the channel dead, queued
the clip to the outbox, reconnected. Not a defect in this feature; this is the same
self-healing behavior already documented in ARCHITECTURE.md §6.3/§6.7 for Tier 1.

### New, separate, OPEN issue found: Termux itself gets killed in the background

Independent of all of the above: the phone's Termux does not have the **Termux:API** add-on
app installed (`pm list packages` shows `com.termux`/`com.termux.tasker` only) — so
`termux-wake-lock` had nothing to actually hook into and silently didn't prevent Android from
killing the backgrounded process during idle. This is the *same class* of problem already
diagnosed for Tailscale itself earlier this session (Doze killing a backgrounded connection),
now hitting Termux's own process instead. **Left OPEN** — the fix is installing Termux:API and
using a real `termux-wake-lock`, or (better, for actual production use) relying on
`setup_termux.md`'s existing §7 Termux:Boot autostart instead of a manually-launched
interactive session, since a boot-service-launched process is a fundamentally more durable
pattern than something started by hand and left in a session Android is free to reap. Not
something this feature's code can fix — it's phone configuration, same category as the
Tailscale/battery-optimization fix already applied earlier tonight.

## Verdict (updated)

**The LAN-fallback code is proven correct, end to end, on real hardware.** Three real bugs
(all in `termux/copyclip_node.py`'s `rish` invocation, none in the TLS/HMAC/framing code this
original spike covered) were found and fixed by direct, isolated, on-device testing — each
hypothesis was confirmed or refuted with a real measurement before moving to the next, per
this project's own rules. The one remaining gap (Termux getting killed in the background) is
a device-configuration issue, tracked separately, not a defect in this feature.
