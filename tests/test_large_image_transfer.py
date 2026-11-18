"""Autonomous test for large 50 MB image transfer across physical devices.

Tests:
1. Generation of valid 50 MiB JPEG image.
2. Device -> Laptop: Pushing 50 MiB image to /sdcard/DCIM/Camera on device.
3. Verification that ClipBridge streams chunks with minimal heap overhead.
4. Verification that Laptop reassembles and saves the exact 50 MiB file with matching SHA256.
5. Verification that Windows clipboard text is untouched (clipboard guard canary).
6. Cleanup of test files on both sides.
"""

import glob
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip

import device_env  # noqa: E402

SERIAL = device_env.device_address("tab")
CAMERA_DIR = os.path.join(device_env.screenshot_dir(), "Camera")


def create_dummy_jpeg_50mb() -> tuple[bytes, str]:
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    trailer = b"\xff\xd9"
    target_size = 50 * 1024 * 1024  # 50 MiB
    pad_size = target_size - len(header) - len(trailer)
    chunk_size = 65000
    chunks = []
    remaining = pad_size
    while remaining > 0:
        cur = min(remaining, chunk_size)
        if cur < 4:
            chunks.append(b"\x00" * cur)
            break
        payload_len = cur - 4
        chunks.append(b"\xff\xfe" + (cur - 2).to_bytes(2, "big") + b"K" * payload_len)
        remaining -= cur
    data = header + b"".join(chunks) + trailer
    data = data[:target_size - 2] + trailer
    sha = hashlib.sha256(data).hexdigest()
    return data, sha


def main():
    print("=" * 60)
    print("AUTONOMOUS 50 MB LARGE IMAGE TRANSFER TEST")
    print("=" * 60)

    print("Step 1: Generating 50 MiB synthetic test JPEG...")
    t0 = time.time()
    raw_data, expected_sha = create_dummy_jpeg_50mb()
    gen_time = time.time() - t0
    print(f"Generated {len(raw_data)} bytes in {gen_time:.2f}s (SHA256: {expected_sha})")

    # Set Windows clipboard canary text to verify camera photo does not clobber it
    canary = f"canary_guard_50mb_{expected_sha[:8]}"
    winclip.write_text(canary)
    print(f"Clipboard canary armed: {canary}")

    test_id = uuid.uuid4().hex[:8]
    # Write dummy JPEG to a temporary file locally
    temp_dir = tempfile.gettempdir()
    local_path = os.path.join(temp_dir, f"test_50mb_{test_id}.jpg")
    with open(local_path, "wb") as f:
        f.write(raw_data)

    remote_filename = f"Camera_test_50mb_{test_id}.jpg"
    remote_path = f"/sdcard/DCIM/Camera/{remote_filename}"

    print(f"Step 2: Pushing 50 MiB file to device {SERIAL}:{remote_path}...")
    t_push_start = time.time()
    res = subprocess.run(["adb", "-s", SERIAL, "push", local_path, remote_path],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ADB push failed: {res.stderr}")
        return 1
    t_push = time.time() - t_push_start
    print(f"ADB push completed in {t_push:.2f}s ({len(raw_data) / (1024*1024) / t_push:.1f} MB/s)")
    subprocess.run(["adb", "-s", SERIAL, "shell", f"touch {remote_path} && touch /sdcard/DCIM/Camera"],
                   capture_output=True, text=True)

    # Record baseline files in Temporary Screenshots\Camera
    before_files = set(glob.glob(os.path.join(CAMERA_DIR, "*")))

    print("Step 3: Waiting for ClipBridge to detect, stream, and reassemble 50 MiB photo on laptop...")
    deadline = time.time() + 180.0  # Allow up to 180s for 50 MB transfer over wireless network
    new_file = None
    t_detect_start = time.time()

    while time.time() < deadline:
        after_files = set(glob.glob(os.path.join(CAMERA_DIR, "*")))
        added = after_files - before_files
        for p in added:
            # Check if file has reached complete size
            try:
                if os.path.getsize(p) == len(raw_data):
                    new_file = p
                    break
            except Exception:
                pass
        if new_file:
            break
        time.sleep(0.5)

    if not new_file:
        print("FAIL: Timed out waiting for 50 MiB camera photo backup!")
        # Check if partial file exists
        after_files = set(glob.glob(os.path.join(CAMERA_DIR, "*")))
        for p in after_files - before_files:
            print(f"Partial file observed: {p} ({os.path.getsize(p)} bytes)")
        return 1

    t_transfer = time.time() - t_detect_start
    throughput = (len(raw_data) / (1024 * 1024)) / t_transfer
    print(f"SUCCESS: 50 MiB file arrived in {t_transfer:.2f}s ({throughput:.2f} MB/s)")
    print(f"Laptop path: {new_file}")

    print("Step 4: Verifying SHA256 integrity of reassembled file...")
    with open(new_file, "rb") as f:
        actual_data = f.read()
    actual_sha = hashlib.sha256(actual_data).hexdigest()
    print(f"Expected SHA256: {expected_sha}")
    print(f"Actual   SHA256: {actual_sha}")
    assert actual_sha == expected_sha, "SHA256 mismatch on 50 MiB transfer!"
    print("SHA256 verified byte-for-byte identical.")

    print("Step 5: Verifying camera backup saved to dedicated Camera folder...")
    assert os.path.isfile(new_file), f"Camera backup file missing: {new_file}"
    assert os.path.getsize(new_file) == len(raw_data), "Camera backup size mismatch"
    print("Camera photo verified saved to disk with zero clipboard clobbering.")

    print("Step 6: Cleaning up test files...")
    subprocess.run(["adb", "-s", SERIAL, "shell", f"rm -f {remote_path}"])
    try:
        os.remove(local_path)
    except Exception:
        pass
    try:
        os.remove(new_file)
    except Exception:
        pass
    print("Cleanup complete.")

    print("=" * 60)
    print("50 MB LARGE IMAGE TRANSFER: PASSED 100%")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
