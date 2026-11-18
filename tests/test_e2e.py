"""End-to-end test for the laptop-to-phone path.

Exercises the whole stack (Windows clipboard -> daemon -> adb -> ClipBridge -> Android
clipboard and back), not just the transport. Requires the phone to be UNLOCKED, because
Android denies clipboard access while the keyguard is up.

Kept alongside test_three_device.py because it still runs when the tablet is unavailable,
and it isolates a phone-only failure from a Samsung Continuity failure.

    python tests\\test_e2e.py
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

# Resolve the phone from the device list. Falls back to the v1 flat key so an older
# config still runs this suite.
_enabled = [d for d in CFG.get("devices", []) if d.get("enabled", True)]
_phone = next((d for d in _enabled if d["name"] == "phone"), _enabled[0] if _enabled else None)
DEV = _phone["device"] if _phone else CFG["device"]
ONESHOT = ("CLASSPATH=/data/local/tmp/clipbridge.dex app_process / "
           "com.mohitarora.copyclip.ClipBridge")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def phone_get() -> str | None:
    """Read the phone clipboard out-of-band, independent of the running daemon."""
    r = subprocess.run(["adb", "-s", DEV, "shell", f"{ONESHOT} get"],
                       capture_output=True, text=True, timeout=30)
    line = (r.stdout or "").strip().replace("\r", "")
    if line.startswith("CLIP"):
        return base64.b64decode(line.split(" ", 3)[3]).decode("utf-8")
    return None


def phone_set(text: str) -> str:
    """Write the phone clipboard out-of-band, simulating the user copying on the phone."""
    b64 = base64.b64encode(text.encode()).decode()
    r = subprocess.run(["adb", "-s", DEV, "shell", f"{ONESHOT} set {b64}"],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr).strip().replace("\r", "")


def phone_locked() -> bool:
    r = subprocess.run(["adb", "-s", DEV, "shell",
                        "dumpsys trust 2>/dev/null | grep -m1 'id=0'"],
                       capture_output=True, text=True, timeout=30)
    return "deviceLocked=1" in r.stdout


def main() -> int:
    if phone_locked():
        print("ABORT: phone is locked. Android denies clipboard access behind the keyguard.")
        print("Unlock the phone and rerun.")
        return 2

    print("starting daemon...")
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "copyclip.py")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=ROOT)
    lines: list[str] = []
    ready = threading.Event()

    def reader():
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line.rstrip())
            if "] connected" in line:
                ready.set()

    threading.Thread(target=reader, daemon=True).start()

    try:
        if not ready.wait(45):
            print("daemon never connected. output:")
            print("\n".join(lines[-20:]))
            return 1
        print("daemon connected\n")
        time.sleep(1.5)  # let the baseline clip settle

        stamp = int(time.time())

        # --- laptop -> phone ------------------------------------------------
        a = f"CopyClip-L2P-{stamp}\nline2\twith-tab\nनमस्ते 🎯 mixed unicode"
        winclip.write_text(a)
        time.sleep(3.0)
        got = phone_get()
        check("laptop -> phone", got == a,
              "" if got == a else f"phone has {got!r:.70}")

        # --- phone -> laptop ------------------------------------------------
        b = f"CopyClip-P2L-{stamp}\nsecond direction ✅ परीक्षण"
        phone_set(b)
        time.sleep(3.0)
        got = winclip.read_text()
        check("phone -> laptop", got == b,
              "" if got == b else f"windows has {got!r:.70}")

        # --- large payload --------------------------------------------------
        big = ("BIG%06d " % stamp) * 6000  # ~54 KB
        winclip.write_text(big)
        time.sleep(5.0)
        got = phone_get()
        check(f"large payload ({len(big)} chars)", got == big,
              "" if got == big else f"got {len(got) if got else 0} chars")

        # --- loop guard -----------------------------------------------------
        # After a phone->laptop sync the content must come to rest, not ping-pong.
        c = f"CopyClip-LOOP-{stamp}"
        phone_set(c)
        time.sleep(3.0)
        win_after = winclip.read_text()
        time.sleep(3.0)
        stable = winclip.read_text() == win_after == c and phone_get() == c
        check("loop guard (content settles)", stable,
              "" if stable else "clipboard kept changing")

        # --- sensitive filter ------------------------------------------------
        transfers = sum(1 for ln in lines if "phone -> laptop:" in ln or "laptop -> phone:" in ln)
        check("no runaway transfers", transfers <= 8, f"{transfers} transfers logged")

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
        print("\n".join(lines[-25:]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
