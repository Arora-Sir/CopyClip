#!/data/data/com.termux/files/usr/bin/python
"""CopyClip phone-side node (Tier 2). Runs inside Termux on the S24 Ultra.

Bridges a Tailscale-reachable TCP socket to ClipBridge, which it launches through `rish`
so the helper runs under the shell UID (the only non-root identity Android grants
background clipboard access to).

Why this exists: wireless ADB does not survive a phone reboot on this device, but Shizuku
does. Same dex and same protocol as the ADB tier, different pipe.

Self-contained on purpose: framing is duplicated here rather than imported so this file can
be copied to the phone on its own. The optional TLS wrap below (--cert-file/--tls-key-file)
is duplicated from transports/tcp_transport.py for the same reason -- see
bugs/SPIKE-LAN-TLS-FALLBACK.md for why it exists (a same-WiFi fallback for when Tailscale
itself drops, which has no WireGuard tunnel underneath to rely on for confidentiality) and
transports/framing.py's docstring for how the two layers (TLS here, HMAC below) divide the
work: this is the only place a certificate is provisioned, so if it's ever changed, mirror
the change in _wrap_tls's counterpart in tcp_transport.py by hand.

    python copyclip_node.py --key-file ~/.copyclip.key
    python copyclip_node.py --key-file ~/.copyclip.key --cert-file ~/.copyclip_tls.crt --tls-key-file ~/.copyclip_tls.key
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading

DEFAULT_PORT = 8385
# NOT ~/clipbridge.dex: that's inside Termux's own private, sandboxed app directory
# (/data/data/com.termux/files/home), which `uid=2000` (shell -- what rish/Shizuku grants)
# cannot read, confirmed live: `ls` on that path as the rish-granted shell returned
# "Permission denied" even though Termux's own process can read it fine. app_process
# aborted instantly trying to load a CLASSPATH it had no permission to open. /data/local/tmp
# is world-readable and is exactly where transports/adb_transport.py already pushes this
# same dex for Tier 1 -- same fix, same convention, confirmed working live.
DEX = "/data/local/tmp/clipbridge.dex"
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
    # Passing the command as argv (`rish sh -c "..."` or `sh rish -c "..."`) never got further
    # than rish's own "Entering shell..." banner, no matter how the args were shaped or
    # quoted -- confirmed by hand at an interactive prompt too: running `~/rish` bare drops
    # into an interactive shell (that exact banner), and only a command *typed into* that
    # shell's stdin afterward actually ran. So this spawns rish with no arguments at all, and
    # writes the real command to its stdin right after, exactly like typing it interactively
    # -- argv-passing appears not to be a supported mode for this rish build/version at all.
    proc = subprocess.Popen([rish],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    # `CLASSPATH=... app_process ...` is POSIX env-assignment-prefix syntax -- needs a real
    # shell to parse it, which is exactly what typing it into rish's own interactive shell
    # (uid 2000) provides.
    cmd = f"CLASSPATH={DEX} app_process / {MAIN} serve\n"
    proc.stdin.write(cmd.encode())
    proc.stdin.flush()
    return proc


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

    # rish prints its own preamble (observed: "Entering shell...") on proc.stdout before our
    # command's real output begins -- confirmed to land on stdout, not stderr, so it can't be
    # split off by stream (see spawn_bridge()'s stderr comment, which turned out to only be
    # half the fix). Discard anything before the first line that's actually a real ClipBridge
    # verb; forward everything unconditionally after that, same as before this existed.
    _DEVICE_VERBS = {"READY", "CLIP", "DESC", "STAMP", "ACK", "DEFER", "LOCK", "SKIP",
                     "PONG", "ERR", "CLIPB", "CLIPC"}

    def pump_bridge_to_net() -> None:
        nonlocal tx_seq
        seen_real_output = False
        try:
            for raw in proc.stdout:
                payload = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not payload:
                    continue
                if not seen_real_output:
                    if payload.split(" ", 1)[0] not in _DEVICE_VERBS:
                        print(f"[bridge preamble, discarded] {payload}", file=sys.stderr)
                        continue
                    seen_real_output = True
                with send_lock:
                    tx_seq += 1
                    f.write((seal(key, tx_seq, payload) + "\n").encode())
        except Exception as exc:
            print(f"bridge->net ended: {exc}", file=sys.stderr)

    def drain_bridge_stderr() -> None:
        # rish's own banner and any real ClipBridge crash output land here now -- printed
        # locally for whoever is watching this Termux session, never forwarded as protocol
        # data (that's the bug this split fixed; see spawn_bridge()'s comment).
        try:
            for raw in proc.stderr:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    print(f"[bridge stderr] {text}", file=sys.stderr)
        except Exception:
            pass

    t = threading.Thread(target=pump_bridge_to_net, daemon=True)
    t.start()
    threading.Thread(target=drain_bridge_stderr, daemon=True).start()

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


def build_tls_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--key-file", default=os.path.expanduser("~/.copyclip.key"))
    ap.add_argument("--cert-file", default=os.path.expanduser("~/.copyclip_tls.crt"),
                    help="TLS cert (PEM). If this and --tls-key-file both exist, every "
                         "connection is TLS-wrapped -- required once this node is reachable "
                         "other than through Tailscale (see module docstring).")
    ap.add_argument("--tls-key-file", default=os.path.expanduser("~/.copyclip_tls.key"))
    args = ap.parse_args()

    if not os.path.isfile(args.key_file):
        raise SystemExit(f"{args.key_file} missing. Create it with the same key as the laptop.")
    key = open(args.key_file, "rb").read().strip()
    if len(key) < 32:
        raise SystemExit("key is too short; use at least 32 bytes")

    tls_ctx: "ssl.SSLContext | None" = None
    if os.path.isfile(args.cert_file) and os.path.isfile(args.tls_key_file):
        tls_ctx = build_tls_context(args.cert_file, args.tls_key_file)
        print(f"TLS enabled ({ssl.OPENSSL_VERSION})", flush=True)
    else:
        print("TLS not configured (cert/key file missing) -- HMAC-only, safe only when "
              "reached over Tailscale", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Android forbids non-system apps binding ports below 1024, hence 8385.
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    print(f"copyclip node listening on :{args.port}", flush=True)

    while True:
        conn, addr = srv.accept()
        print(f"client {addr[0]}", flush=True)
        if tls_ctx is not None:
            try:
                conn = tls_ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError as exc:
                print(f"TLS handshake failed, dropping client: {exc}", file=sys.stderr)
                conn.close()
                continue
        # One client at a time: a second would fight over the same clipboard.
        try:
            serve_client(conn, key)
        except Exception as exc:
            print(f"client error: {exc}", file=sys.stderr)
        print("client gone", flush=True)


if __name__ == "__main__":
    sys.exit(main())
