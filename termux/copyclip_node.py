#!/data/data/com.termux/files/usr/bin/python
"""CopyClip phone-side node (Tier 2). Runs inside Termux on the S24 Ultra.

Bridges a Tailscale-reachable TCP socket to ClipBridge, which it launches through `rish`
so the helper runs under the shell UID (the only non-root identity Android grants
background clipboard access to).

Why this exists: wireless ADB does not survive a phone reboot on this device, but Shizuku
does. Same dex and same protocol as the ADB tier, different pipe.

Self-contained on purpose: framing is duplicated here rather than imported so this file can
be copied to the phone on its own.

    python copyclip_node.py --key-file ~/.copyclip.key
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
import threading

DEFAULT_PORT = 8385
DEX = os.path.expanduser("~/clipbridge.dex")
MAIN = "me.mohit.copyclip.ClipBridge"


# --- framing (mirror of transports/framing.py) ---------------------------

class AuthError(RuntimeError):
    pass


def _mac(key: bytes, seq: int, payload: str) -> str:
    return hmac.new(key, f"{seq}:{payload}".encode(), hashlib.sha256).hexdigest()


def seal(key: bytes, seq: int, payload: str) -> str:
    return f"{seq} {_mac(key, seq, payload)} {payload}"


def open_frame(key: bytes, raw: str, last_seq: int) -> tuple[int, str]:
    parts = raw.split(" ", 2)
    if len(parts) == 2:
        parts.append("")
    if len(parts) < 3:
        raise AuthError("malformed frame")
    seq_s, mac, payload = parts
    try:
        seq = int(seq_s)
    except ValueError as exc:
        raise AuthError("bad sequence") from exc
    if seq <= last_seq:
        raise AuthError("replay or reorder")
    if not hmac.compare_digest(mac, _mac(key, seq, payload)):
        raise AuthError("bad MAC")
    return seq, payload


def challenge_response(key: bytes, nonce: str) -> str:
    return hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()


# --- bridge --------------------------------------------------------------

def find_rish() -> str:
    for cand in (os.path.expanduser("~/rish"), "/data/data/com.termux/files/home/rish"):
        if os.path.isfile(cand):
            return cand
    raise SystemExit(
        "rish not found in ~. Open Shizuku > 'Use Shizuku in terminal apps' and export it."
    )


def spawn_bridge() -> subprocess.Popen:
    if not os.path.isfile(DEX):
        raise SystemExit(f"{DEX} missing. Push it from the laptop first (see setup_termux.md).")
    rish = find_rish()
    cmd = f"CLASSPATH={DEX} app_process / {MAIN} serve"
    return subprocess.Popen(["sh", rish, "-c", cmd],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)


def serve_client(conn: socket.socket, key: bytes) -> None:
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    f = conn.makefile("rwb", buffering=0)

    # Prove the client holds the key before starting anything privileged.
    nonce = secrets.token_hex(16)
    f.write(f"HELLO {nonce}\n".encode())
    line = f.readline().decode("utf-8", "replace").strip()
    if not line.startswith("AUTH ") or not hmac.compare_digest(
            line.split(" ", 1)[1], challenge_response(key, nonce)):
        f.write(b"AUTH-FAIL\n")
        print("rejected client: bad key", file=sys.stderr)
        return
    f.write(b"AUTH-OK\n")

    # Then prove it back, so a client cannot be tricked into talking to an impostor.
    line = f.readline().decode("utf-8", "replace").strip()
    if not line.startswith("CHALLENGE "):
        return
    f.write((f"RESPONSE {challenge_response(key, line.split(' ', 1)[1])}\n").encode())

    proc = spawn_bridge()
    tx_seq = 0
    rx_seq = 0
    send_lock = threading.Lock()

    def pump_bridge_to_net() -> None:
        nonlocal tx_seq
        try:
            for raw in proc.stdout:
                payload = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not payload:
                    continue
                with send_lock:
                    tx_seq += 1
                    f.write((seal(key, tx_seq, payload) + "\n").encode())
        except Exception as exc:
            print(f"bridge->net ended: {exc}", file=sys.stderr)

    t = threading.Thread(target=pump_bridge_to_net, daemon=True)
    t.start()

    try:
        for raw in f:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            try:
                rx_seq, payload = open_frame(key, line, rx_seq)
            except AuthError as exc:
                print(f"dropping frame: {exc}", file=sys.stderr)
                break
            proc.stdin.write((payload + "\n").encode())
            proc.stdin.flush()
    except Exception as exc:
        print(f"net->bridge ended: {exc}", file=sys.stderr)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            f.close()
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--key-file", default=os.path.expanduser("~/.copyclip.key"))
    args = ap.parse_args()

    if not os.path.isfile(args.key_file):
        raise SystemExit(f"{args.key_file} missing. Create it with the same key as the laptop.")
    key = open(args.key_file, "rb").read().strip()
    if len(key) < 32:
        raise SystemExit("key is too short; use at least 32 bytes")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Android forbids non-system apps binding ports below 1024, hence 8385.
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    print(f"copyclip node listening on :{args.port}", flush=True)

    while True:
        conn, addr = srv.accept()
        print(f"client {addr[0]}", flush=True)
        # One client at a time: a second would fight over the same clipboard.
        try:
            serve_client(conn, key)
        except Exception as exc:
            print(f"client error: {exc}", file=sys.stderr)
        print("client gone", flush=True)


if __name__ == "__main__":
    sys.exit(main())
