"""Where hardware tests find their devices and folders, without hard-coding anyone's network.

Order of precedence: environment variable, then the user's own config.json, then a clear error.

    COPYCLIP_TEST_DEVICE_TAB=100.x.x.x:5555      address of the device named "tab"
    COPYCLIP_TEST_SCREENSHOT_DIR=C:\\path        screenshot folder
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import config as cfgmod  # noqa: E402


def _cfg() -> dict:
    return cfgmod.load_config(os.path.join(ROOT, "config.json"), validate_values=False)


def device_address(name: str) -> str:
    env = os.environ.get(f"COPYCLIP_TEST_DEVICE_{name.upper()}")
    if env:
        return env
    for dev in _cfg()["devices"]:
        if dev.get("name") == name and dev.get("device"):
            return dev["device"]
    raise SystemExit(f"No device named {name!r}: add it to config.json or set "
                     f"COPYCLIP_TEST_DEVICE_{name.upper()}")


def screenshot_dir() -> str:
    env = os.environ.get("COPYCLIP_TEST_SCREENSHOT_DIR")
    if env:
        return env
    configured = _cfg().get("screenshot_backup_dir")
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots", "Temporary Screenshots")
