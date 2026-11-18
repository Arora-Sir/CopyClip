"""Live test verifying Samsung scroll capture (long screenshot) two-stage sync.
Proves the 45-second re-arm window picks up file growth under the same filename.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip  # noqa: E402

LOG_PATH = os.path.join(ROOT, "logs", "copyclip.log")
import device_env  # noqa: E402

TARGET_DEVICE = device_env.device_address("tab")


def get_log_offset() -> int:
    return os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0


def read_log_since(offset: int) -> str:
    if not os.path.isfile(LOG_PATH):
        return ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def test_scroll_capture() -> None:
    print("[TEST] Simulating Samsung scroll capture on Tab S10 Ultra...")
    marker = get_log_offset()
    test_id = uuid.uuid4().hex[:6]
    remote_name = f"Screenshot_20261029_{test_id}_ScrollTest.png"
    remote_path = f"/sdcard/DCIM/Screenshots/{remote_name}"

    local_initial = os.path.join(ROOT, f"temp_init_{test_id}.png")
    local_stitched = os.path.join(ROOT, f"temp_stitch_{test_id}.png")

    # Stage 1: Initial single-page screenshot with unique color
    r = (int(time.time() * 1000) + uuid.uuid4().int) % 256
    g = (uuid.uuid4().int >> 8) % 256
    b = (uuid.uuid4().int >> 16) % 256
    img1 = Image.new("RGBA", (400, 300), color=(r, g, b, 255))
    img1.putpixel((0, 0), ((r + 40) % 256, (g + 40) % 256, (b + 40) % 256, 255))
    img1.save(local_initial, format="PNG")
    len1 = os.path.getsize(local_initial)

    # Stage 2: Tall stitched screenshot (400x1200, significantly larger)
    img2 = Image.new("RGBA", (400, 1200), color=(r, g, b, 255))
    for y in range(0, 1200, 100):
        for x in range(400):
            img2.putpixel((x, y), (0, 255, 0, 255))
    img2.save(local_stitched, format="PNG")
    len2 = os.path.getsize(local_stitched)

    print(f"Initial image size: {len1} bytes. Stitched image size: {len2} bytes.")

    try:
        # Step 1: Push initial shot
        t0 = time.time()
        subprocess.run(["adb", "-s", TARGET_DEVICE, "push", local_initial, remote_path],
                       capture_output=True, text=True, check=True)
        print(f"Stage 1 pushed in {time.time() - t0:.3f}s")

        # Wait for stage 1 sync
        found_stage1 = False
        while time.time() - t0 < 8.0:
            time.sleep(0.1)
            log_text = read_log_since(marker)
            if f"-> laptop: {len1} chars" in log_text or "screenshot backed up to" in log_text:
                found_stage1 = True
                break

        print(f"Stage 1 initial sync detected: {found_stage1} ({time.time() - t0:.3f}s)")
        assert found_stage1, "Stage 1 initial screenshot did not sync"

        # Wait 3 seconds, simulating user tapping 'Capture more' and Samsung finalizing stitch
        time.sleep(3.0)

        # Step 2: Overwrite the SAME remote path with the tall stitched image
        t1 = time.time()
        subprocess.run(["adb", "-s", TARGET_DEVICE, "push", local_stitched, remote_path],
                       capture_output=True, text=True, check=True)
        print(f"Stage 2 stitched image overwritten under same filename in {time.time() - t1:.3f}s")

        # Wait for stage 2 sync (must detect same filename grew inside 45s window)
        found_stage2 = False
        while time.time() - t1 < 12.0:
            time.sleep(0.2)
            log_text = read_log_since(marker)
            if f"-> laptop: {len2} chars" in log_text:
                found_stage2 = True
                break

        elapsed2 = time.time() - t1
        print(f"Stage 2 stitched sync detected: {found_stage2} ({elapsed2:.3f}s)")
        assert found_stage2, "Stage 2 stitched overwrite was not detected under same filename"

        # Verify Windows clipboard holds the new stitched image
        assert winclip.user32.IsClipboardFormatAvailable(winclip.CF_DIB), "CF_DIB missing from clipboard"
        assert winclip.user32.IsClipboardFormatAvailable(winclip.CF_DIBV5), "CF_DIBV5 missing from clipboard"
        img_res = winclip.read_image()
        assert img_res is not None, "Clipboard image returned None"
        img_bytes, mime = img_res
        assert len(img_bytes) == len2, f"Expected {len2} bytes on clipboard, got {len(img_bytes)}"
        print(f"Clipboard verified holding full stitched image ({len(img_bytes)} bytes)")

        print("\nSAMSUNG LONG SCREENSHOT (SCROLL CAPTURE) VERIFIED.")

    finally:
        if os.path.isfile(local_initial):
            os.remove(local_initial)
        if os.path.isfile(local_stitched):
            os.remove(local_stitched)
        subprocess.run(["adb", "-s", TARGET_DEVICE, "shell", f"rm -f {remote_path}"],
                       capture_output=True, check=False)


if __name__ == "__main__":
    test_scroll_capture()
