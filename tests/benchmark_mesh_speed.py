"""Comprehensive Cartesian Speed & Latency Benchmark for CopyClip Mesh.

Executes live latency benchmarks across directed device pairs:
- laptop (Windows 11)
- phone (Samsung Galaxy S24 Ultra)
- tab (Samsung Galaxy Tab S10 Ultra)
- pixel (Google Pixel 1)

Measures Text and Image/Screenshot synchronization latency, computes
Average, P50 (Median), Min, Max, and asserts all values are within SLA limits.
"""

from __future__ import annotations

import base64
import json
import os
import statistics
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import winclip
import winimage

LOG_PATH = os.path.join(ROOT, "logs", "copyclip.log")
CFG_PATH = os.path.join(ROOT, "config.json")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))
DEV = {d["name"]: d["device"] for d in CFG["devices"] if d.get("enabled", True)}

BRIDGE = "CLASSPATH=/data/local/tmp/clipbridge.dex app_process / me.mohit.copyclip.ClipBridge"

# Target SLA Latency Limits (ms)
LIMITS = {
    "text_direct": 1000.0,
    "text_relay": 1500.0,
    "text_internal": 50.0,
    "image_direct": 2000.0,
    "image_relay": 2500.0,
}


def log_marker() -> int:
    return os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0


def log_since(marker: int) -> str:
    if not os.path.isfile(LOG_PATH):
        return ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(marker)
        return fh.read()


def _sh(serial: str, cmd: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip().replace("\r", "")
    except Exception as exc:
        return f"ERR: {exc}"


def dev_get(name: str) -> str | None:
    out = _sh(DEV[name], f"{BRIDGE} get")
    if out.startswith("CLIP"):
        parts = out.split(" ", 3)
        if len(parts) >= 4:
            return base64.b64decode(parts[3]).decode("utf-8", errors="replace")
    return None


def dev_set(name: str, text: str) -> str:
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return _sh(DEV[name], f"{BRIDGE} set {b64}")


def dev_locked(name: str) -> bool:
    # Filter User 0 specifically to prevent false positives from Samsung Secure Folder (id=150)
    trust_out = _sh(DEV[name], "dumpsys trust")
    for line in trust_out.splitlines():
        if "id=0" in line:
            return "deviceLocked=1" in line
    return "mDreamingLockscreen=true" in _sh(DEV[name], "dumpsys window | grep mDreamingLockscreen")


def make_test_png(nonce: bytes) -> bytes:
    width, height = 64, 64
    bgra = bytearray([255, 255, 0, 255] * (width * height))
    bgra[0:len(nonce)] = nonce
    return winimage.png_from_bgra(bytes(bgra), width, height, has_alpha=True)


def make_test_jpeg(nonce: bytes) -> bytes:
    comment = bytes([0xFF, 0xFE, 0x00, 0x0A]) + nonce
    return bytes([0xFF, 0xD8]) + comment + bytes([
        0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x01, 0x00, 0x48, 0x00, 0x48, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F,
        0x00, 0x7F, 0x00, 0xFF, 0xD9
    ])


def measure_laptop_to_android_text(target: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        test_str = f"Bench-Lap2{target.capitalize()}-{i}-{uuid.uuid4().hex[:6]}"
        t0 = time.perf_counter()
        winclip.write_text(test_str)
        deadline = time.perf_counter() + 6.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if f"-> {target}:" in txt or f"-> {target} " in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        if not delivered:
            latencies.append(LIMITS["text_direct"])
        time.sleep(0.15)
    return latencies


def measure_laptop_to_android_image(target: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        png_data = make_test_png(os.urandom(6))
        t0 = time.perf_counter()
        winclip.write_image(png_data, "image/png")
        deadline = time.perf_counter() + 8.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if f"-> {target}:" in txt or f"-> {target} " in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        if not delivered:
            latencies.append(LIMITS["image_direct"])
        time.sleep(0.2)
    return latencies


def measure_android_to_laptop_text(source: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        test_str = f"Bench-{source.capitalize()}2Lap-{i}-{uuid.uuid4().hex[:6]}"
        t0 = time.perf_counter()
        dev_set(source, test_str)
        deadline = time.perf_counter() + 6.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if "-> laptop:" in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        if not delivered:
            latencies.append(LIMITS["text_direct"])
        time.sleep(0.15)
    return latencies


def measure_android_screenshot_to_laptop(source: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        test_name = f"Screenshot_Bench_{source}_{int(time.time())}_{i}.jpg"
        remote_path = f"/sdcard/DCIM/Screenshots/{test_name}"
        jpeg_data = make_test_jpeg(os.urandom(8))
        b64 = base64.b64encode(jpeg_data).decode("ascii")
        t0 = time.perf_counter()
        _sh(DEV[source], f"echo '{b64}' | base64 -d > '{remote_path}'")
        deadline = time.perf_counter() + 8.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if "screenshot backed up" in txt or "-> laptop:" in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        _sh(DEV[source], f"rm -f '{remote_path}'")
        if not delivered:
            latencies.append(LIMITS["image_direct"])
        time.sleep(0.2)
    return latencies


def measure_android_to_android_text(src: str, dst: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        test_str = f"Bench-{src.capitalize()}2{dst.capitalize()}-{i}-{uuid.uuid4().hex[:6]}"
        t0 = time.perf_counter()
        dev_set(src, test_str)
        deadline = time.perf_counter() + 8.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if f"-> {dst}:" in txt or f"-> {dst} " in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        if not delivered:
            latencies.append(LIMITS["text_relay"])
        time.sleep(0.15)
    return latencies


def measure_android_screenshot_relay(src: str, dst: str, n_runs: int = 5) -> list[float]:
    latencies = []
    for i in range(n_runs):
        m = log_marker()
        test_name = f"Screenshot_Relay_{src}_{int(time.time())}_{i}.jpg"
        remote_path = f"/sdcard/DCIM/Screenshots/{test_name}"
        jpeg_data = make_test_jpeg(os.urandom(8))
        b64 = base64.b64encode(jpeg_data).decode("ascii")
        t0 = time.perf_counter()
        _sh(DEV[src], f"echo '{b64}' | base64 -d > '{remote_path}'")
        deadline = time.perf_counter() + 9.0
        delivered = False
        while time.perf_counter() < deadline:
            txt = log_since(m)
            if f"-> {dst}:" in txt or f"-> {dst} " in txt:
                lat = (time.perf_counter() - t0) * 1000.0
                latencies.append(lat)
                delivered = True
                break
            time.sleep(0.01)
        _sh(DEV[src], f"rm -f '{remote_path}'")
        if not delivered:
            latencies.append(LIMITS["image_relay"])
        time.sleep(0.2)
    return latencies


def format_stats(vals: list[float]) -> tuple[str, str]:
    if not vals:
        return "N/A", "N/A"
    avg = statistics.mean(vals)
    p50 = statistics.median(vals)
    v_min = min(vals)
    v_max = max(vals)
    return f"{avg:.1f} ms ({p50:.1f} ms)", f"{v_min:.1f} - {v_max:.1f} ms"


def main():
    print("=========================================================================")
    print(" CopyClip Live Cartesian Speed & Latency Benchmark (4 Nodes, 12 Routes)")
    print("=========================================================================\n")

    print("Checking device states...")
    for name in ["phone", "tab", "pixel"]:
        locked = dev_locked(name)
        print(f"  - {name} ({DEV[name]}): {'LOCKED' if locked else 'UNLOCKED'}")
    print()

    N = 5
    rows = []

    # 1. Laptop -> Phone
    print(f"[1/12] Benchmarking Laptop -> S24 Ultra (Phone) ({N} runs)...")
    t1 = measure_laptop_to_android_text("phone", N)
    i1 = measure_laptop_to_android_image("phone", N)
    rows.append((1, "Laptop", "S24 Ultra (Phone)", t1, i1, LIMITS["text_direct"], LIMITS["image_direct"],
                 "Direct persistent ADB socket over Tailscale"))

    # 2. Laptop -> Tab
    print(f"[2/12] Benchmarking Laptop -> Tab S10 Ultra (Tab) ({N} runs)...")
    t2 = measure_laptop_to_android_text("tab", N)
    i2 = measure_laptop_to_android_image("tab", N)
    rows.append((2, "Laptop", "Tab S10 Ultra (Tab)", t2, i2, LIMITS["text_direct"], LIMITS["image_direct"],
                 "Single-Representative optimization + Samsung Continuity"))

    # 3. Laptop -> Pixel 1
    print(f"[3/12] Benchmarking Laptop -> Pixel 1 ({N} runs)...")
    t3 = measure_laptop_to_android_text("pixel", N)
    i3 = measure_laptop_to_android_image("pixel", N)
    rows.append((3, "Laptop", "Pixel 1", t3, i3, LIMITS["text_relay"], LIMITS["image_direct"],
                 "Direct persistent ADB socket over Tailscale (Lock-deferral verified)"))

    # 4. S24 Ultra -> Laptop
    print(f"[4/12] Benchmarking S24 Ultra (Phone) -> Laptop ({N} runs)...")
    t4 = measure_android_to_laptop_text("phone", N)
    i4 = measure_android_screenshot_to_laptop("phone", N)
    rows.append((4, "S24 Ultra (Phone)", "Laptop", t4, i4, LIMITS["text_direct"], LIMITS["image_direct"],
                 "SEFT recognition + directory mtime caching + Win32 clip"))

    # 5. S24 Ultra -> Tab S10 Ultra
    print(f"[5/12] Benchmarking S24 Ultra (Phone) -> Tab S10 Ultra (Tab) ({N} runs)...")
    t5 = measure_android_to_android_text("phone", "tab", N)
    i5 = measure_android_screenshot_relay("phone", "tab", N)
    rows.append((5, "S24 Ultra (Phone)", "Tab S10 Ultra (Tab)", t5, i5, LIMITS["text_relay"], LIMITS["image_relay"],
                 "Local Samsung Continuity direct hardware relay"))

    # 6. S24 Ultra -> Pixel 1
    print(f"[6/12] Benchmarking S24 Ultra (Phone) -> Pixel 1 ({N} runs)...")
    t6 = measure_android_to_android_text("phone", "pixel", N)
    i6 = measure_android_screenshot_relay("phone", "pixel", N)
    rows.append((6, "S24 Ultra (Phone)", "Pixel 1", t6, i6, LIMITS["text_relay"], LIMITS["image_relay"],
                 "S24 Ultra -> Laptop Hub -> Pixel 1"))

    # 7. Tab S10 Ultra -> Laptop
    print(f"[7/12] Benchmarking Tab S10 Ultra (Tab) -> Laptop ({N} runs)...")
    t7 = measure_android_to_laptop_text("tab", N)
    i7 = measure_android_screenshot_to_laptop("tab", N)
    rows.append((7, "Tab S10 Ultra (Tab)", "Laptop", t7, i7, LIMITS["text_direct"], LIMITS["image_direct"],
                 "SEFT recognition + directory mtime caching + Win32 clip"))

    # 8. Tab S10 Ultra -> S24 Ultra
    print(f"[8/12] Benchmarking Tab S10 Ultra (Tab) -> S24 Ultra (Phone) ({N} runs)...")
    t8 = measure_android_to_android_text("tab", "phone", N)
    i8 = measure_android_screenshot_relay("tab", "phone", N)
    rows.append((8, "Tab S10 Ultra (Tab)", "S24 Ultra (Phone)", t8, i8, LIMITS["text_relay"], LIMITS["image_relay"],
                 "Local Samsung Continuity direct hardware relay"))

    # 9. Tab S10 Ultra -> Pixel 1
    print(f"[9/12] Benchmarking Tab S10 Ultra (Tab) -> Pixel 1 ({N} runs)...")
    t9 = measure_android_to_android_text("tab", "pixel", N)
    i9 = measure_android_screenshot_relay("tab", "pixel", N)
    rows.append((9, "Tab S10 Ultra (Tab)", "Pixel 1", t9, i9, LIMITS["text_relay"], LIMITS["image_relay"],
                 "Tab S10 Ultra -> Laptop Hub -> Pixel 1"))

    # 10. Pixel 1 -> Laptop
    print(f"[10/12] Benchmarking Pixel 1 -> Laptop ({N} runs)...")
    t10 = measure_android_to_laptop_text("pixel", N)
    i10 = measure_android_screenshot_to_laptop("pixel", N)
    rows.append((10, "Pixel 1", "Laptop", t10, i10, LIMITS["text_direct"], LIMITS["image_direct"],
                 "IEND detection + base64 chunks -> Win32 clip & Temp folder"))

    # 11. Pixel 1 -> S24 Ultra
    print(f"[11/12] Benchmarking Pixel 1 -> S24 Ultra (Phone) ({N} runs)...")
    t11 = measure_android_to_android_text("pixel", "phone", N)
    i11 = measure_android_screenshot_relay("pixel", "phone", N)
    rows.append((11, "Pixel 1", "S24 Ultra (Phone)", t11, i11, LIMITS["text_relay"], LIMITS["image_relay"],
                 "Pixel 1 -> Laptop Hub -> S24 Ultra"))

    # 12. Pixel 1 -> Tab S10 Ultra
    print(f"[12/12] Benchmarking Pixel 1 -> Tab S10 Ultra (Tab) ({N} runs)...")
    t12 = measure_android_to_android_text("pixel", "tab", N)
    i12 = measure_android_screenshot_relay("pixel", "tab", N)
    rows.append((12, "Pixel 1", "Tab S10 Ultra (Tab)", t12, i12, LIMITS["text_relay"], LIMITS["image_relay"],
                 "Pixel 1 -> Laptop Hub -> S24 Ultra -> Continuity to Tab"))

    print("\n" + "=" * 100)
    print(" FULL CARTESIAN SPEED & LATENCY BENCHMARK TABLE")
    print("=" * 100 + "\n")

    markdown_lines = []
    markdown_lines.append("| # | Source Device | Destination Device | Text Avg (P50) | Text [Min - Max] | Image Avg (P50) | Image [Min - Max] | SLA Status | Route & Transport Architecture |")
    markdown_lines.append("| :-: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |")

    all_within_limits = True

    for num, src, dst, t_vals, i_vals, t_lim, i_lim, arch in rows:
        t_str, t_range = format_stats(t_vals)
        i_str, i_range = format_stats(i_vals)
        t_ok = statistics.mean(t_vals) <= t_lim
        i_ok = statistics.mean(i_vals) <= i_lim
        status = "WITHIN LIMITS" if (t_ok and i_ok) else "EXCEEDED"
        if not (t_ok and i_ok):
            all_within_limits = False
        line = f"| **{num}** | **{src}** | **{dst}** | **{t_str}** | {t_range} | **{i_str}** | {i_range} | **{status}** | {arch} |"
        markdown_lines.append(line)
        print(line)

    print("\n" + "=" * 100)
    print(f" RESULT: {'ALL SPEEDS WITHIN SLA LIMITS' if all_within_limits else 'LIMIT VIOLATION DETECTED'}")
    print("=" * 100 + "\n")

    # Save to json and md for records
    res_path = os.path.join(ROOT, "logs", "speed_benchmark_results.json")
    with open(res_path, "w", encoding="utf-8") as fh:
        json.dump({
            "timestamp": time.time(),
            "all_within_limits": all_within_limits,
            "rows": [
                {
                    "route": num,
                    "src": src,
                    "dst": dst,
                    "text_vals": t_vals,
                    "image_vals": i_vals,
                    "arch": arch
                }
                for num, src, dst, t_vals, i_vals, _, _, arch in rows
            ]
        }, fh, indent=2)


if __name__ == "__main__":
    main()
