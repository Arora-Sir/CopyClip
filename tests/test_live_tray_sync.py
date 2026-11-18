"""Live check against a RUNNING tray: PC <-> phone <-> tablet, plus the control API.

Needs the tray running (python windows_app\\tray.py) and both devices unlocked. Devices are
pinned by the addresses in config.json and driven by one-shot ClipBridge calls, so nothing is
mocked. It overwrites the clipboards of this PC and of the named devices with test strings.

    set COPYCLIP_DEVICE_TESTS=1
    python tools\\run_tests.py --device tests\\test_live_tray_sync.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip  # noqa: E402
from core import config as cfgmod  # noqa: E402

CFG = cfgmod.load_config(os.path.join(ROOT, "config.json"), validate_values=False)
DEV = {d["name"]: d["device"] for d in CFG["devices"] if d.get("enabled", True)}
TEST_DEVICES = ("phone", "tab")
BRIDGE = "CLASSPATH=/data/local/tmp/clipbridge.dex app_process / com.mohitarora.copyclip.ClipBridge"
CONTINUITY_WAIT = 8.0
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return ok


def api(method: str, path: str, body=None):
    with open(os.path.join(ROOT, CFG["state_dir"], "control.json"), encoding="utf-8") as fh:
        ctl = json.load(fh)
    req = urllib.request.Request(
        f"http://127.0.0.1:{ctl['port']}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-CopyClip-Token": ctl["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def sh(name: str, cmd: str) -> str:
    r = subprocess.run(["adb", "-s", DEV[name], "shell", cmd], capture_output=True, text=True, timeout=40)
    return (r.stdout or r.stderr).strip().replace("\r", "")


def model(name: str) -> str:
    return sh(name, "getprop ro.product.model")


def dev_get(name: str):
    out = sh(name, f"{BRIDGE} get")
    return base64.b64decode(out.split(" ", 3)[3]).decode("utf-8") if out.startswith("CLIP") else None


def dev_set(name: str, text: str) -> str:
    return sh(name, f"{BRIDGE} set {base64.b64encode(text.encode()).decode()}")


def wait_for(fn, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return None


def main() -> int:
    print("identity checks (allow-list):")
    expected = {"phone": "SM-S928", "tab": "SM-X92"}
    for n in TEST_DEVICES:
        m = model(n)
        if not check(f"{n} is the expected model", m.startswith(expected[n]), f"{DEV[n]} -> {m}"):
            print("Refusing to continue: device identity does not match the allow-list.")
            return 2

    print("tray API:")
    st = api("GET", "/api/status")
    check("tray reports version", bool(st.get("version")), st.get("version", ""))
    for n in TEST_DEVICES:
        check(f"{n} connected in the tray", st["nodes"][n]["available"] and not st["nodes"][n]["locked"])
    check("Samsung group healthy", all(d["healthy"] for d in st["domains"]))

    tag = uuid.uuid4().hex[:8]

    print("PC -> phone and tablet:")
    text = f"pc-to-devices {tag} हिन्दी \U0001F600\nline2\ttab"
    winclip.write_text(text)
    check("phone received PC clip", wait_for(lambda: dev_get("phone") == text, 10) is not None)
    check("tablet received PC clip (via Samsung)", wait_for(lambda: dev_get("tab") == text, CONTINUITY_WAIT + 5) is not None)

    print("phone -> PC:")
    text = f"phone-to-pc {tag}"
    dev_set("phone", text)
    check("PC clipboard got the phone clip", wait_for(lambda: winclip.read_text() == text, 12) is not None)

    print("tablet -> PC:")
    text = f"tab-to-pc {tag}"
    dev_set("tab", text)
    got = wait_for(lambda: winclip.read_text() == text, 12)
    check("PC clipboard got the tablet clip", got is not None)

    print("Activity screen data:")
    ev = api("GET", "/api/activity?limit=20")["events"]
    check("activity recorded the three clips", sum(1 for e in ev if e["kind"] == "text") >= 3, f"{len(ev)} events")
    check("PC to devices event lists both targets",
          any(e["origin"] == "laptop" and e["preview"] and f"pc-to-devices {tag}" in e["preview"] for e in ev))
    check("no preview leaked for non-text", all(e["preview"] is None for e in ev if e["kind"] != "text"))

    print("pause:")
    api("POST", "/api/pause", {"paused": True})
    paused_text = f"while-paused {tag}"
    winclip.write_text(paused_text)
    time.sleep(5)
    check("a paused sync does not reach the phone", dev_get("phone") != paused_text)
    api("POST", "/api/pause", {"paused": False})
    after = f"after-resume {tag}"
    winclip.write_text(after)
    check("sync works again after resume", wait_for(lambda: dev_get("phone") == after, 10) is not None)

    print("settings round trip:")
    before = api("GET", "/api/config")["config"]["max_chars"]
    r = api("PUT", "/api/config", {"max_chars": before - 1})
    check("live setting applied without restart", r["applied"] == ["max_chars"] and not r["restart_required"])
    api("PUT", "/api/config", {"max_chars": before})
    check("setting restored", api("GET", "/api/config")["config"]["max_chars"] == before)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
