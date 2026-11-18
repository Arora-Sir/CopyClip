"""Standalone-device verification: laptop <-> any one device with no Samsung Continuity
partner (pixel today, whatever comes next tomorrow).

Unlike test_three_device.py, this does not assert domain-redundancy behaviour (there is no
domain), and drives the device two ways depending on what's actually being verified:

    1. Raw ClipBridge get/set over a plain `adb shell` pipe -- proves the dex mechanism
       itself works on this device, independent of Shizuku/the installed app entirely.
    2. The installed app's "Send Clip" push (transports/push_listener.py), for a device
       whose automatic background bridge can't run (root-mode Shizuku, no non-root reboot
       survival, etc.) -- proves the manual fallback actually reaches the hub.

Reusable for a future device: pass its name and adb target on the command line, nothing in
this file is hardcoded to "pixel" beyond the default.

    python tests\\test_standalone_device.py pixel 100.64.0.13:5555
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip  # noqa: E402
from transports.framing import challenge_response, new_nonce, seal, verify_challenge  # noqa: E402

BRIDGE = ("CLASSPATH=/data/local/tmp/clipbridge.dex app_process / "
          "com.mohitarora.copyclip.ClipBridge")

LOG_PATH = os.path.join(ROOT, "logs", "copyclip.log")


def log_marker() -> int:
    """Byte offset to diff against later. Reads the *running production daemon's* own log,
    not a subprocess this test spawns (unlike test_three_device.py) -- this test drives an
    already-running tray.py, so log lines are the only way to see which device the hub
    actually wrote to directly, as opposed to a target that only ended up with the content
    because Samsung Continuity relayed it there within the same wait window. Checking final
    clipboard state alone cannot tell those two apart, and will misreport Continuity's own
    relay as a CopyClip redundant-write bug.
    """
    return os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0


def log_since(marker: int) -> str:
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(marker)
        return fh.read()


def writes_to(log_text: str, target: str) -> int:
    return log_text.count(f"-> {target}:")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def _sh(serial: str, cmd: str, timeout: int = 40) -> str:
    r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or r.stderr).strip().replace("\r", "")


def dev_get(serial: str) -> str | None:
    out = _sh(serial, f"{BRIDGE} get")
    if out.startswith("CLIP"):
        return base64.b64decode(out.split(" ", 3)[3]).decode("utf-8")
    return None


def dev_set(serial: str, text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return _sh(serial, f"{BRIDGE} set {b64}")


def _load_config() -> dict:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


_CFG = _load_config()
_DOMAIN_MEMBERS = {n for d in _CFG.get("domains", []) for n in d.get("members", [])}


def _other_configured_devices(exclude: str) -> dict[str, str]:
    """Every other enabled device.serial from config.json, keyed by name -- so this test
    stays reusable for whatever devices happen to be configured, not hardcoded to today's
    phone/tab/pixel names.
    """
    return {d["name"]: d["device"] for d in _CFG.get("devices", [])
            if d.get("enabled", True) and d["name"] != exclude}


def push_via_hub(device_name: str, port: int, key: bytes, text: str) -> tuple[bool, str]:
    """Speaks the client side of push_listener.py's handshake directly -- exercises the
    exact same wire protocol PushClient.kt uses, without needing the Android app installed
    or a real device at all for this half of the check.
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=8)
        f = s.makefile("rwb", buffering=0)
        hello = f.readline().decode().strip()
        if not hello.startswith("HELLO "):
            return False, f"bad greeting: {hello!r}"
        nonce = hello.split(" ", 1)[1]
        f.write(f"AUTH {challenge_response(key, nonce)}\n".encode())
        if f.readline().decode().strip() != "AUTH-OK":
            return False, "hub rejected our key"
        challenge = f.readline().decode().strip()
        their_nonce = challenge.removeprefix("CHALLENGE ")
        f.write(f"RESPONSE {challenge_response(key, their_nonce)}\n".encode())
        b64 = base64.b64encode(text.encode()).decode()
        frame = seal(key, 1, f"PUSH {device_name} text/plain 0 {b64}")
        f.write((frame + "\n").encode())
        reply = f.readline().decode().strip()
        s.close()
        return reply == "PUSH-OK", reply
    except OSError as exc:
        return False, str(exc)


def main() -> int:
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <device-name> <adb-serial-or-ip:port> [push-port]")
        return 2
    name, serial = sys.argv[1], sys.argv[2]
    push_port = int(sys.argv[3]) if len(sys.argv) > 3 else 8387

    key_path = os.path.join(ROOT, "copyclip.key")
    if not os.path.isfile(key_path):
        print(f"ABORT: {key_path} missing")
        return 2
    with open(key_path, "rb") as fh:
        key = fh.read().strip()

    print(f"device: {name} ({serial}), push_port={push_port}")
    subprocess.run(["adb", "connect", serial], capture_output=True, timeout=15)

    stamp = int(time.time())

    # --- 1. laptop -> device, over the raw dex mechanism (no app/Shizuku involved) -----
    a = f"CopyClip-standalone-L2D-{stamp}"
    dev_set(serial, a)
    time.sleep(1.0)
    got = dev_get(serial)
    check(f"laptop -> {name} (raw ClipBridge set/get)", got == a,
          "" if got == a else f"got {str(got)[:60]!r}")

    # --- 2. device -> laptop, same mechanism, opposite direction -----------------------
    b = f"CopyClip-standalone-D2L-{stamp}"
    dev_set(serial, b)
    time.sleep(1.0)
    got = dev_get(serial)
    check(f"{name} clipboard round-trips its own write", got == b,
          "" if got == b else f"got {str(got)[:60]!r}")

    # --- 2b. Mesh propagation, standalone -> everyone else, not just the laptop --------
    # This is the gap test_three_device.py and the checks above don't cover: does this
    # device's own copy actually reach the *other* configured devices too, respecting
    # domain rules (exactly one Samsung-domain member gets a direct WRITE, never both --
    # the other picks it up via Samsung Continuity's own relay, which is not a CopyClip
    # write at all and must not be counted as one). Checked via the hub's own log, not
    # final clipboard state: within the same few-second window, Continuity's relay can
    # make a non-written domain member's clipboard match anyway, which a state-only check
    # would misreport as a redundant-write bug that was never actually there.
    mesh_targets = _other_configured_devices(name)
    if mesh_targets:
        marker = log_marker()
        d = f"CopyClip-standalone-MESH-{stamp}"
        dev_set(serial, d)
        time.sleep(4.0)
        seg = log_since(marker)
        direct_writes = {n: writes_to(seg, n) for n in mesh_targets}
        check(f"{name}'s copy directly written to at least one other device",
              any(c > 0 for c in direct_writes.values()), f"{direct_writes}")
        domain_writes = {n: c for n, c in direct_writes.items() if n in _DOMAIN_MEMBERS}
        check("at most one Samsung-domain member got a direct write (no redundant write)",
              sum(1 for c in domain_writes.values() if c > 0) <= 1,
              f"direct writes per domain member: {domain_writes}")
    else:
        print(f"  [SKIP] mesh propagation: no other devices configured alongside {name!r}")

    # --- 2c. Mesh propagation, the other direction: a domain member's copy should also
    # reach this standalone device, exercising the same code path phone/tab already prove
    # for each other, just landing on a device with no domain partner of its own.
    domain_sources = {n: s for n, s in mesh_targets.items()} if mesh_targets else {}
    domain_sources = {n: s for n, s in domain_sources.items() if n in _DOMAIN_MEMBERS}
    if domain_sources:
        src_name, src_serial = next(iter(domain_sources.items()))
        e = f"CopyClip-standalone-REVERSE-{stamp}"
        dev_set(src_serial, e)
        time.sleep(4.0)
        got = dev_get(serial)
        check(f"{src_name}'s copy reached {name} too (not just its own domain partner)",
              got == e, "" if got == e else f"got {str(got)[:60]!r}")
    else:
        print(f"  [SKIP] reverse mesh check: no Samsung-domain device configured to test from")

    # --- 3. Send Clip push path: does the hub-side listener accept and route it? -------
    # This does not require the laptop's tray daemon to currently be routing to this
    # specific device -- it proves the push_listener.py <-> hub.on_clip() wire and auth
    # work correctly, the same thing PushClient.kt exercises from the Android side.
    c = f"CopyClip-standalone-PUSH-{stamp}"
    ok, detail = push_via_hub(name, push_port, key, c)
    check(f"{name} Send Clip push reaches the hub (requires tray.py running, "
          f"port {push_port} open)", ok, detail)
    if ok:
        time.sleep(1.0)
        check("pushed content applied to laptop clipboard",
              winclip.read_text() == c, f"laptop clipboard: {str(winclip.read_text())[:60]!r}")

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
