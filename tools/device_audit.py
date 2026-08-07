r"""Read-only audit of both Samsung devices, written to bugs/DEVICE-STATE.md.

Purpose: make device state a *diff against a known baseline* rather than something we
re-derive from memory every time something misbehaves. Every value here was worth knowing
during the toast-storm investigation, so it is captured permanently.

Strictly read-only. It runs `getprop`, `settings get`, `appops get`, `pm` and `dumpsys`,
and changes nothing.

    python tools\device_audit.py            # rewrite bugs/DEVICE-STATE.md
    python tools\device_audit.py --print    # print, do not write
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "bugs", "DEVICE-STATE.md")

# Every package that has ever plausibly touched the clipboard on these devices, plus what
# it actually is. The "what" column is the part that took real digging to establish.
WATCHED = [
    ("com.android.shell", "CopyClip's ClipBridge runs as this UID"),
    ("com.samsung.android.mdx", "Link to Windows Service; hosts ClipboardRedirectorService"),
    ("com.samsung.android.mdx.kit", "Link to Windows device discovery"),
    ("com.microsoft.appmanager", "Microsoft Phone Link; binds the clipboard redirector"),
    ("com.samsung.android.mcfserver", "Multi Continuity Framework: phone<->tab relay"),
    ("com.samsung.android.mcfds", "MCF device sync"),
    ("com.samsung.android.galaxycontinuity", "Samsung Continuity"),
    ("com.samsung.android.honeyboard", "Samsung Keyboard"),
]

# PackageManager enabled-state constants. 0 is the trap: it means "use the manifest
# default", which for all of these is ENABLED. It does not mean disabled.
ENABLED_STATES = {
    "0": "DEFAULT (enabled)",
    "1": "ENABLED (explicit)",
    "2": "DISABLED",
    "3": "DISABLED_USER",
    "4": "DISABLED_UNTIL_USED",
}


def load_devices() -> dict[str, str]:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    return {d["name"]: d["device"] for d in cfg.get("devices", []) if d.get("enabled", True)}


def sh(serial: str, cmd: str, timeout: int = 40) -> str:
    try:
        r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip().replace("\r", "")
    except Exception as exc:
        return f"<error: {exc}>"


def reachable(serial: str) -> bool:
    r = subprocess.run(["adb", "-s", serial, "get-state"],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip() == "device"


def enabled_state(serial: str, pkg: str) -> str:
    out = sh(serial, f"dumpsys package {pkg} 2>/dev/null | grep -m1 'User 0:'")
    if not out or "<error" in out:
        return "not installed"
    for token in out.split():
        if token.startswith("enabled="):
            val = token.split("=", 1)[1]
            label = ENABLED_STATES.get(val, f"unknown({val})")
            stopped = "stopped=true" in out
            return f"{label}{', stopped' if stopped else ''}"
    return "unknown"


def clipboard_op(serial: str, pkg: str) -> str:
    out = sh(serial, f"appops get {pkg} READ_CLIPBOARD 2>/dev/null")
    if not out:
        return "n/a"
    # appops answers "No operations.\nDefault mode: allow" for packages that have never
    # read; collapse it so it cannot break the markdown table it lands in.
    flat = " ".join(line.strip() for line in out.splitlines() if line.strip())
    return flat.replace("READ_CLIPBOARD: ", "").strip()


def audit(name: str, serial: str) -> list[str]:
    L: list[str] = [f"### {name} (`{serial}`)", ""]

    if not reachable(serial):
        L += ["**UNREACHABLE.** Wireless ADB does not survive a reboot on these devices "
              "(`persist.adb.tcp.port` is empty); re-arm with `adb -d tcpip 5555` over USB.",
              ""]
        return L

    model = sh(serial, "getprop ro.product.model")
    rel = sh(serial, "getprop ro.build.version.release")
    sdk = sh(serial, "getprop ro.build.version.sdk")
    patch = sh(serial, "getprop ro.build.version.security_patch")
    locked = "deviceLocked=1" in sh(serial, "dumpsys trust 2>/dev/null | grep -m1 'id=0'")

    L += [f"- Model **{model}**, Android {rel} (SDK {sdk}), patch {patch}",
          f"- Device locked right now: **{'yes' if locked else 'no'}** "
          f"(a locked device silently discards clipboard writes)",
          ""]

    L += ["**Settings**", "", "| Setting | Value | Meaning |", "| :--- | :--- | :--- |"]
    toast = sh(serial, "settings get secure clipboard_show_access_notifications")
    L.append(f"| `clipboard_show_access_notifications` | `{toast}` | "
             f"{'ON: every clipboard read raises a toast' if toast == '1' else 'off: no read toasts'} |")
    svc = sh(serial, "getprop service.adb.tcp.port") or "(unset)"
    per = sh(serial, "getprop persist.adb.tcp.port") or "(unset)"
    wifi = sh(serial, "settings get global adb_wifi_enabled")
    L.append(f"| `service.adb.tcp.port` | `{svc}` | runtime only, wiped on reboot |")
    L.append(f"| `persist.adb.tcp.port` | `{per}` | if unset, port 5555 dies on reboot |")
    L.append(f"| `adb_wifi_enabled` | `{wifi}` | Wireless debugging; persists but uses a "
             f"random mDNS port that Tailscale cannot reach |")
    L.append("")

    L += ["**Clipboard-relevant packages**", "",
          "| Package | State | READ_CLIPBOARD | What it is |",
          "| :--- | :--- | :--- | :--- |"]
    for pkg, what in WATCHED:
        L.append(f"| `{pkg}` | {enabled_state(serial, pkg)} | {clipboard_op(serial, pkg)} | {what} |")
    L.append("")

    services = sh(serial, "dumpsys activity services 2>/dev/null | grep -E "
                          "'ServiceRecord.*(mdx|mcf|appmanager|galaxycontinuity)'")
    if services and "<error" not in services:
        L += ["**Running cross-device services**", "", "```"]
        for line in services.splitlines():
            L.append(line.strip().lstrip("* ").strip())
        L += ["```", "",
              "`c:<pkg>` is the *client* that bound the service. "
              "`ClipboardRedirectorService` bound by `com.microsoft.appmanager` is what reads "
              "the clipboard for Link to Windows even when the feature looks switched off.",
              ""]

    bridge = sh(serial, "ls -l /data/local/tmp/clipbridge.dex 2>/dev/null")
    L += [f"- ClipBridge on device: `{bridge or 'NOT PRESENT'}`", ""]

    read_media = clipboard_op(serial, "com.android.shell").replace("READ_CLIPBOARD", "")
    read_media_images = sh(serial, "appops get com.android.shell READ_MEDIA_IMAGES 2>/dev/null")
    L += [f"- `com.android.shell READ_MEDIA_IMAGES`: `{read_media_images or 'n/a'}` "
          f"(image clipboard reads the Pictures/CopyClip MediaStore row through this)", ""]

    pictures = sh(serial, "ls -la /sdcard/Pictures/CopyClip/ 2>/dev/null")
    if pictures and "No such file" not in pictures:
        L += ["**Pictures/CopyClip contents**", "", "```", pictures, "```", ""]
    else:
        L += ["- `Pictures/CopyClip`: does not exist yet (created on first image sync)", ""]
    return L


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="only_print", action="store_true")
    args = ap.parse_args()

    devices = load_devices()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc = [
        "# Device state baseline",
        "",
        f"Generated by `tools/device_audit.py` at **{now}**. Read-only; rerun any time.",
        "",
        "This exists so device changes are a diff against a known baseline instead of a",
        "guess. If clipboard behaviour changes, regenerate and compare.",
        "",
        "> **Reading `enabled=`:** `0` means DEFAULT, which for all packages below means",
        "> **enabled**. It does not mean disabled. Getting this backwards sent the first",
        "> pass of the BUG-001 investigation down the wrong path.",
        "",
    ]
    for name, serial in devices.items():
        doc += audit(name, serial)

    text = "\n".join(doc)
    if args.only_print:
        print(text)
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
