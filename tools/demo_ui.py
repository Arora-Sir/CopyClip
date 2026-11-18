"""Run the CopyClip UI against fake devices, with no phones, clipboard or tray involved.

    python tools/demo_ui.py                 # healthy mesh
    python tools/demo_ui.py --degraded      # pixel offline, one clip queued
    python tools/demo_ui.py --open          # also open it as an app window

Prints the sign-in URL. Config and logs live in a throwaway temp directory, so nothing in
the real config.json, logs or state is read or written.
"""

from __future__ import annotations

import argparse
import os
import sys
import struct
import tempfile
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config as cfgmod                       # noqa: E402
from core.hub import Hub                                # noqa: E402
from core.models import ClipPayload, WriteResult        # noqa: E402
from core.outbox import Outbox                          # noqa: E402
from core.policy import Policy                          # noqa: E402
from domains.sync_domain import SyncDomain              # noqa: E402
from tests.fakes import FakeNode                        # noqa: E402
from windows_app.control_api import ControlServer       # noqa: E402
from windows_app.service import ControlService          # noqa: E402

SAMPLE_LOG = """2026-11-11 09:57:40 INFO    copyclip.tray: started (pid 18420), 3 devices configured
2026-11-11 09:58:03 WARNING copyclip.node.android: [tab] connect failed (2 consecutive): device offline
2026-11-11 09:58:11 INFO    copyclip.node.android: [tab] connected
2026-11-11 10:09:20 INFO    copyclip.hub: sensitive clip from phone, hidden from history
2026-11-11 10:09:44 ERROR   copyclip.node.windows: [laptop] write failed
Traceback (most recent call last):
  File "nodes/windows_node.py", line 343, in write
OSError: OpenClipboard denied
2026-11-11 10:09:44 ERROR   copyclip.node.windows: (previous message repeated 1954 more times)
2026-11-11 12:31:02 WARNING copyclip.node.android: [pixel] connect failed: adb connect timed out after 8 s
2026-11-11 12:35:09 INFO    copyclip.hub: routed text 214 chars from laptop to phone, tab
"""


def demo_png(width: int, height: int, hue: int) -> bytes:
    """A small made-up screenshot (gradient, title bar and blocks) so the picture history has something to show."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < height // 14:
                px = (36, 38, 46)
            elif (x // (width // 5) + y // (height // 6)) % 3 == 0 and height // 8 < y < height - height // 8:
                px = (70 + hue % 90, 130 + (x * 60) // width, 210 - (y * 80) // height)
            else:
                px = (28 + (x * 40) // width + hue % 20, 32 + (y * 36) // height, 52 + ((x + y) * 30) // (width + height))
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class DemoLaptop(FakeNode):
    """Stands in for this PC: keeps image clips as files so the UI can show and re-copy them."""

    def __init__(self, folder: str) -> None:
        super().__init__("laptop")
        self.folder, self.saved = folder, {}
        os.makedirs(folder, exist_ok=True)

    def write(self, payload):
        if payload.is_image and not payload.is_camera:
            path = os.path.join(self.folder, f"Screenshot demo {len(self.saved) + 1}.png")
            with open(path, "wb") as fh:
                fh.write(payload.data)
            self.saved[payload.sha256] = path
        return super().write(payload)

    def saved_path(self, sha):
        return self.saved.get(sha)

    def copy_file_to_clipboard(self, path, mime):
        print("copy to clipboard:", path)

    def copy_text_to_clipboard(self, text):
        print("copy text to clipboard:", len(text), "characters")


def build(degraded: bool, port: int, token: str):
    tmp = tempfile.mkdtemp(prefix="copyclip-demo-")
    cfg_path = os.path.join(tmp, "config.json")
    logs = os.path.join(tmp, "logs")
    os.makedirs(logs)
    cfgmod.save_config(cfg_path, cfgmod.with_defaults({
        "devices": [
            {"name": "phone", "label": "Galaxy S24 Ultra", "device": "100.64.0.11:5555"},
            {"name": "tab", "label": "Galaxy Tab S10 Ultra", "device": "100.64.0.12:5555"},
            {"name": "pixel", "label": "Pixel 1", "device": "100.64.0.13:5555"},
        ],
        "domains": [{"name": "samsung", "members": ["phone", "tab"]}],
        "allow_images": True, "allow_sensitive": True,
        "screenshot_backup_dir": os.path.join(tmp, "Screenshots"),
    }))
    with open(os.path.join(logs, "copyclip.log"), "w", encoding="utf-8") as fh:
        fh.write(SAMPLE_LOG)

    nodes = {n: FakeNode(n) for n in ("phone", "tab", "pixel")}
    nodes["laptop"] = DemoLaptop(os.path.join(tmp, "Screenshots"))
    nodes["pixel"]._available = not degraded
    nodes["pixel"].last_error = "adb connect timed out after 8 s" if degraded else None
    domain = SyncDomain("samsung", [nodes["phone"], nodes["tab"]])
    hub = Hub(nodes=list(nodes.values()), domains=[domain], policy=Policy(allow_images=True, allow_sensitive=True),
              outbox=Outbox(os.path.join(tmp, "state", "outbox.json")))
    if degraded:
        nodes["pixel"].result = WriteResult.UNAVAILABLE

    hub.preview_hold_s = 0.05     # the demo pictures are tiny; do not wait for the preview guard
    hub.on_clip(ClipPayload.from_text("Meeting notes: ship the redesign on Friday and update the README.", "laptop"))
    hub.on_clip(ClipPayload(mime="image/png", data=demo_png(270, 585, 10), origin="phone"))
    hub.on_clip(ClipPayload(mime="image/png", data=demo_png(640, 400, 40), origin="tab"))
    hub.on_clip(ClipPayload.from_text("123456", "phone", sensitive=True))
    hub.on_clip(ClipPayload.from_text("https://example.com/docs/getting-started", "tab"))
    hub.on_clip(ClipPayload.from_text("x" * 900_000, "laptop"))   # skipped: too large
    hub.on_clip(ClipPayload(mime="image/jpeg", data=bytes(5_200_000), origin="tab", is_camera=True))
    time.sleep(0.4)
    nodes["pixel"].result = WriteResult.OK

    opened = []
    svc = ControlService(
        hub, config_path=cfg_path, logs_dir=logs, key_path=os.path.join(tmp, "copyclip.key"),
        open_path=lambda p: print("open folder:", p), pick_folder=lambda initial: os.path.join(tmp, "Picked"),
        open_window=lambda route: print("open window:", route), quit_app=lambda: os._exit(0),
        restart=lambda: print("restart requested"), read_clipboard=lambda: "demo clipboard text",
        rollover_logs=lambda: open(os.path.join(logs, "copyclip.log"), "w").close(),
        run_cmd=lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, stdout="connected to demo", stderr=""))
    return ControlServer(svc, port=port, token=token), tmp, opened


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--degraded", action="store_true")
    ap.add_argument("--port", type=int, default=8399)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    server, tmp, _ = build(args.degraded, args.port, "demo-token-demo-token-demo-token")
    server.start()
    print("CopyClip UI demo:", server.launch_url())
    print("temp data:", tmp)
    if args.open:
        from windows_app import launcher
        launcher.open_window(server.launch_url())
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
