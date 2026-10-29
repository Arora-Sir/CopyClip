"""Live end-to-end regression tests on connected device.
Tests screenshot sync latency, camera auto-backup, and direct password sync.
"""

from __future__ import annotations

import io
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
import winimage  # noqa: E402

LOG_PATH = os.path.join(ROOT, "logs", "copyclip.log")
TARGET_DEVICE = "100.64.0.12:5555"
SS_DIR = r"<home>\Pictures\Screenshots\Temporary Screenshots"
CAM_DIR = os.path.join(SS_DIR, "Camera")


def get_log_offset() -> int:
    if not os.path.isfile(LOG_PATH):
        return 0
    return os.path.getsize(LOG_PATH)


def read_log_since(offset: int) -> str:
    if not os.path.isfile(LOG_PATH):
        return ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def test_screenshot_latency(device: str) -> None:
    print(f"\n[TEST 1] Screenshot sync latency test on {device}...")
    marker = get_log_offset()
    test_id = uuid.uuid4().hex[:8]
    local_tmp = os.path.join(ROOT, f"temp_ss_{test_id}.png")
    remote_path = f"/sdcard/DCIM/Screenshots/Screenshot_test_{test_id}.png"

    # Create solid test image with unique color and pixel to ensure unique hash
    r = (int(time.time() * 1000) + uuid.uuid4().int) % 256
    g = (uuid.uuid4().int >> 8) % 256
    b = (uuid.uuid4().int >> 16) % 256
    img = Image.new("RGBA", (400, 300), color=(r, g, b, 255))
    img.putpixel((0, 0), ((r + 50) % 256, (g + 50) % 256, (b + 50) % 256, 255))
    img.save(local_tmp, format="PNG")
    target_bytes = os.path.getsize(local_tmp)

    try:
        t0 = time.time()
        # Push test screenshot to device
        cmd = ["adb", "-s", device, "push", local_tmp, remote_path]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"Pushed to {remote_path} ({target_bytes} bytes) in {time.time() - t0:.3f}s")

        # Wait for CopyClip to process screenshot
        found = False
        elapsed = 0.0
        while time.time() - t0 < 15.0:
            time.sleep(0.1)
            log_text = read_log_since(marker)
            if "screenshot backed up to" in log_text and ("-> laptop" in log_text or "screenshot written to clipboard" in log_text):
                elapsed = time.time() - t0
                found = True
                break

        print(f"Detection and sync completed in {elapsed:.3f}s (found: {found})")
        assert found, f"Screenshot sync was not detected in logs within 15 seconds on {device}"
        assert elapsed < 5.0, f"Screenshot latency {elapsed:.2f}s exceeded 5s budget on {device}"

        # Verify clipboard formats on Windows
        assert winclip.user32.IsClipboardFormatAvailable(winclip.CF_DIB), "CF_DIB missing from clipboard"
        assert winclip.user32.IsClipboardFormatAvailable(winclip.CF_DIBV5), "CF_DIBV5 missing from clipboard"
        print("Both CF_DIB and CF_DIBV5 verified on clipboard.")

        # Verify image readback
        img_result = winclip.read_image()
        assert img_result is not None, "Clipboard image readback returned None"
        img_bytes, mime = img_result
        assert len(img_bytes) > 0, "Clipboard image readback returned empty data"
        print(f"Clipboard image verified ({len(img_bytes)} bytes, mime: {mime})")

    finally:
        if os.path.isfile(local_tmp):
            os.remove(local_tmp)
        subprocess.run(["adb", "-s", device, "shell", f"rm -f {remote_path}"],
                       capture_output=True, check=False)


def test_camera_auto_backup(device: str) -> None:
    print(f"\n[TEST 2] Camera auto-backup test on {device}...")
    marker = get_log_offset()
    test_id = uuid.uuid4().hex[:8]
    local_tmp = os.path.join(ROOT, f"temp_cam_{test_id}.jpg")
    remote_path = f"/sdcard/DCIM/Camera/Camera_test_{test_id}.jpg"

    # Set distinctive clipboard text before test
    canary_text = f"canary_text_guard_{test_id}"
    winclip.write_text(canary_text)
    time.sleep(0.2)

    # Create test JPEG image with unique color and pixel
    r = (int(time.time() * 1000) + uuid.uuid4().int) % 256
    g = (uuid.uuid4().int >> 8) % 256
    b = (uuid.uuid4().int >> 16) % 256
    img = Image.new("RGB", (320, 240), color=(r, g, b))
    img.putpixel((0, 0), ((r + 30) % 256, (g + 30) % 256, (b + 30) % 256))
    img.save(local_tmp, format="JPEG", quality=90)

    try:
        t0 = time.time()
        # Push test photo to camera folder
        cmd = ["adb", "-s", device, "push", local_tmp, remote_path]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"Pushed to {remote_path} in {time.time() - t0:.3f}s")

        # Wait for photo backup to appear in CAM_DIR or logs
        found_file = False
        backup_file_path = ""
        while time.time() - t0 < 15.0:
            time.sleep(0.2)
            log_text = read_log_since(marker)
            if "camera photo backed up to" in log_text:
                found_file = True
                for line in log_text.splitlines():
                    if "camera photo backed up to" in line:
                        parts = line.split("camera photo backed up to ")
                        if len(parts) > 1:
                            backup_file_path = parts[1].split(" (")[0].strip()
                break

        elapsed = time.time() - t0
        print(f"Camera backup completed in {elapsed:.3f}s (file: {backup_file_path})")
        assert found_file, f"Camera photo was not saved to laptop Camera backup folder from {device}"
        assert os.path.getsize(backup_file_path) > 0, "Saved camera photo file is empty"

        # Verify active Windows clipboard was NOT clobbered
        current_clip = winclip.read_text()
        print(f"Windows clipboard content check: {current_clip}")
        assert current_clip == canary_text, f"Clipboard was clobbered. Expected {canary_text}, got {current_clip}"
        print("Clipboard guard verified. Camera photo did not clobber active clipboard.")

    finally:
        if os.path.isfile(local_tmp):
            os.remove(local_tmp)
        subprocess.run(["adb", "-s", device, "shell", f"rm -f {remote_path}"],
                       capture_output=True, check=False)
        if backup_file_path and os.path.isfile(backup_file_path):
            os.remove(backup_file_path)


def test_direct_password_sync() -> None:
    print("\n[TEST 3] Direct password sync test...")
    secret_text = f"P@ssw0rd_Test_{uuid.uuid4().hex[:6]}"

    # Write sensitive text with CanIncludeInClipboardHistory=0
    winclip.write_text_sensitive(secret_text)
    time.sleep(0.3)

    read_back = winclip.read_text()
    assert read_back == secret_text, f"Expected {secret_text}, got {read_back}"
    print("Direct password write and read verified on Windows.")

    # Re-copy the same password text
    winclip.write_text_sensitive(secret_text)
    time.sleep(0.3)
    read_back2 = winclip.read_text()
    assert read_back2 == secret_text, f"Repeated copy failed. Expected {secret_text}, got {read_back2}"
    print("Repeated password copy verified without drop.")


if __name__ == "__main__":
    devices = [TARGET_DEVICE]
    if len(sys.argv) > 1:
        devices = [sys.argv[1]]
    elif "100.64.0.11:5555" != TARGET_DEVICE:
        # Also test phone if connected
        devices = [TARGET_DEVICE, "100.64.0.11:5555"]

    for dev in devices:
        print(f"\n==========================================")
        print(f"RUNNING SUITE ON DEVICE: {dev}")
        print(f"==========================================")
        test_screenshot_latency(dev)
        test_camera_auto_backup(dev)

    test_direct_password_sync()
    print("\nALL FLEET REGRESSION TESTS PASSED.")
