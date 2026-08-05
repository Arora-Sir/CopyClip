"""Three-device verification: laptop + S24 Ultra + Tab S10 Ultra.

Runs the real daemon and drives the devices out-of-band with one-shot ClipBridge calls,
so nothing here is mocked. Both devices must be UNLOCKED: Android denies clipboard access
behind the keyguard.

    python tests\\test_three_device.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
DEV = {d["name"]: d["device"] for d in CFG["devices"] if d.get("enabled", True)}
BRIDGE = ("CLASSPATH=/data/local/tmp/clipbridge.dex app_process / "
          "me.mohit.copyclip.ClipBridge")

# Samsung took ~2.0-2.5 s to mirror in measurement; give it generous headroom.
CONTINUITY_WAIT = 7.0

results: list[tuple[str, bool, str]] = []
lines: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def _sh(serial: str, cmd: str, timeout: int = 40) -> str:
    r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or r.stderr).strip().replace("\r", "")


def dev_get(name: str) -> str | None:
    out = _sh(DEV[name], f"{BRIDGE} get")
    if out.startswith("CLIP"):
        return base64.b64decode(out.split(" ", 3)[3]).decode("utf-8")
    return None


def dev_set(name: str, text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return _sh(DEV[name], f"{BRIDGE} set {b64}")


def dev_locked(name: str) -> bool:
    return "deviceLocked=1" in _sh(DEV[name], "dumpsys trust 2>/dev/null | grep -m1 'id=0'")


def since(marker: int) -> list[str]:
    return lines[marker:]


def writes_to(logs: list[str], target: str) -> int:
    """Count deliveries the daemon logged into `target`."""
    return sum(1 for ln in logs if f"-> {target}:" in ln)


def main() -> int:
    locked = [n for n in DEV if dev_locked(n)]
    if locked:
        print(f"ABORT: locked device(s): {', '.join(locked)}. Unlock and rerun.")
        return 2

    print(f"devices: {', '.join(f'{k}={v}' for k, v in DEV.items())}")
    print("starting daemon...")
    # DEBUG so the per-frame routing decisions are observable. The redundancy guarantee
    # is a headline behaviour, and asserting it needs the "body not fetched" decision to
    # actually appear in the log rather than being inferred.
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "copyclip.py"),
                             "--log-level", "DEBUG"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=ROOT)
    connected: set[str] = set()
    ready = threading.Event()

    def reader():
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            lines.append(line)
            for name in DEV:
                if f"[{name}] connected" in line:
                    connected.add(name)
            if connected >= set(DEV):
                ready.set()

    threading.Thread(target=reader, daemon=True).start()

    try:
        if not ready.wait(60):
            print(f"only connected: {sorted(connected)}")
            print("\n".join(lines[-25:]))
            return 1
        print(f"daemon connected to {sorted(connected)}\n")
        time.sleep(2.0)
        stamp = int(time.time())

        # --- 1. laptop -> both devices --------------------------------------
        m = len(lines)
        a = f"CopyClip-L2ALL-{stamp} नमस्ते 🎯"
        winclip.write_text(a)
        time.sleep(CONTINUITY_WAIT)
        on_phone, on_tab = dev_get("phone"), dev_get("tab")
        check("laptop -> phone", on_phone == a,
              "" if on_phone == a else f"phone={on_phone!r:.50}")
        check("laptop -> tab (via Continuity)", on_tab == a,
              "" if on_tab == a else f"tab={on_tab!r:.50}")

        # --- 2. the redundancy guarantee ------------------------------------
        seg = since(m)
        n_phone, n_tab = writes_to(seg, "phone"), writes_to(seg, "tab")
        check("exactly one device written (no redundant call)",
              (n_phone + n_tab) == 1, f"phone={n_phone} tab={n_tab}")

        # --- 3. phone -> laptop ---------------------------------------------
        m = len(lines)
        b = f"CopyClip-P2L-{stamp} ✅"
        dev_set("phone", b)
        time.sleep(CONTINUITY_WAIT)
        got = winclip.read_text()
        check("phone -> laptop", got == b, "" if got == b else f"laptop={got!r:.50}")
        seg = since(m)
        check("phone copy applied to laptop exactly once",
              writes_to(seg, "laptop") == 1, f"{writes_to(seg, 'laptop')} applications")
        check("no write back into the Samsung domain",
              writes_to(seg, "phone") == 0 and writes_to(seg, "tab") == 0,
              f"phone={writes_to(seg, 'phone')} tab={writes_to(seg, 'tab')}")

        # --- 4. tab -> laptop (the new capability) ---------------------------
        m = len(lines)
        c = f"CopyClip-T2L-{stamp} परीक्षण"
        dev_set("tab", c)
        time.sleep(CONTINUITY_WAIT)
        got = winclip.read_text()
        check("tab -> laptop", got == c, "" if got == c else f"laptop={got!r:.50}")
        seg = since(m)
        check("tab copy applied to laptop exactly once",
              writes_to(seg, "laptop") == 1, f"{writes_to(seg, 'laptop')} applications")
        check("tab copy caused no domain writes",
              writes_to(seg, "phone") == 0 and writes_to(seg, "tab") == 0,
              f"phone={writes_to(seg, 'phone')} tab={writes_to(seg, 'tab')}")

        # --- 5. body fetched only for genuinely new clips --------------------
        skipped = sum(1 for ln in lines if "already known; body not fetched" in ln)
        check("mirrored clips did not re-transfer their body", skipped >= 1,
              f"{skipped} announcement(s) short-circuited")

        # --- 6. large payload across all three -------------------------------
        m = len(lines)
        big = ("BIG%06d " % stamp) * 6000       # ~54 KB
        winclip.write_text(big)
        time.sleep(CONTINUITY_WAIT + 3)
        p, t = dev_get("phone"), dev_get("tab")
        lap = winclip.read_text()
        check(f"large payload ({len(big)} chars) reached both",
              p == big and t == big,
              "" if p == big and t == big
              else (f"phone={len(p) if p else 0} tab={len(t) if t else 0} "
                    f"laptop={len(lap) if lap else 0} | "
                    f"laptop_head={lap[:60]!r} phone_head={(p or '')[:60]!r}"))

        # --- 7. settles, no ping-pong ----------------------------------------
        time.sleep(4)
        stable = (winclip.read_text() == big and dev_get("phone") == big
                  and dev_get("tab") == big)
        check("content settles across all three", stable)

        # --- 8. clipboard read minimisation (BUG-001) ------------------------
        # Reading clip *content* on a device raises Android's "Shell pasted from your
        # clipboard" toast. These two assertions are the real fix, expressed as behaviour:
        # our own writes must never be read back, and a genuine copy must be read exactly
        # once across the whole Samsung domain rather than once per device.
        m = len(lines)
        winclip.write_text(f"CopyClip-READS-L-{stamp}")
        time.sleep(CONTINUITY_WAIT)
        fetches = sum(1 for ln in since(m) if "fetching body" in ln)
        check("laptop copy triggers ZERO device content reads", fetches == 0,
              f"{fetches} content fetch(es); each one would raise a Shell toast")

        m = len(lines)
        dev_set("phone", f"CopyClip-READS-P-{stamp}")
        time.sleep(CONTINUITY_WAIT)
        fetches = sum(1 for ln in since(m) if "fetching body" in ln)
        check("device copy triggers exactly ONE content read domain-wide", fetches == 1,
              f"{fetches} content fetch(es); was 2 before the standby stopped reading")

        total = sum(writes_to(lines, x) for x in ("laptop", "phone", "tab"))
        check("no runaway transfers", total <= 14, f"{total} deliveries logged")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\ndaemon log tail:")
        print("\n".join(lines[-40:]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
