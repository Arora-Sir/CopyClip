"""Single entry point for every CopyClip test tier.

    python tools/run_tests.py                 # unit tier plus clipboard-free legacy scripts
    python tools/run_tests.py --system        # real Windows clipboard/WIC/mutex tests
    python tools/run_tests.py --device tests/test_e2e.py   # hardware script, explicit opt-in

Tiers
    unit    tests/unit/*, plus tests/test_android_node.py (fake transports only).
            Safe anywhere, any time. Never touches the clipboard or a device.
    system  tests/test_core.py, test_imaging.py, test_sensitive_sync.py. These write the real
            Windows clipboard, which the running daemon would sync to the phones, so this
            tier refuses to run while the daemon holds its single-instance mutex, and it
            needs COPYCLIP_SYSTEM_TESTS=1.
    device  Hardware scripts. Need COPYCLIP_DEVICE_TESTS=1 and an explicit script path.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

CLIPBOARD_FREE_SCRIPTS = ["test_android_node.py"]
SYSTEM_SCRIPTS = ["test_core.py", "test_imaging.py", "test_sensitive_sync.py"]
DAEMON_MUTEX = "Local\\CopyClipDaemonSingleton"


def daemon_running() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, DAEMON_MUTEX)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def run_script(name_or_path: str) -> bool:
    path = name_or_path if os.path.isabs(name_or_path) else os.path.join(ROOT, name_or_path)
    if not os.path.isfile(path):
        path = os.path.join(TESTS, os.path.basename(name_or_path))
    print(f"\n=== {os.path.relpath(path, ROOT)} ===", flush=True)
    return subprocess.run([sys.executable, path], cwd=ROOT).returncode == 0


def run_unit() -> bool:
    sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover(os.path.join(TESTS, "unit"), top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return result.wasSuccessful()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--system", action="store_true", help="run the real-clipboard tier")
    ap.add_argument("--device", nargs="+", metavar="SCRIPT", help="run hardware scripts")
    args = ap.parse_args()

    if args.device:
        if os.environ.get("COPYCLIP_DEVICE_TESTS") != "1":
            print("Refusing: set COPYCLIP_DEVICE_TESTS=1. These scripts write real devices.")
            return 2
        return 0 if all([run_script(s) for s in args.device]) else 1

    if args.system:
        if os.environ.get("COPYCLIP_SYSTEM_TESTS") != "1":
            print("Refusing: set COPYCLIP_SYSTEM_TESTS=1. These tests overwrite your clipboard.")
            return 2
        if daemon_running():
            print("Refusing: CopyClip is running and would sync the test clips to your devices.")
            print("Quit the tray app first.")
            return 2
        return 0 if all([run_script(s) for s in SYSTEM_SCRIPTS]) else 1

    ok = run_unit()
    for script in CLIPBOARD_FREE_SCRIPTS:
        ok = run_script(script) and ok
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
