"""SPIKE, not production code -- see bugs/SPIKE-LAN-TLS-FALLBACK.md.

Runs on the laptop. Pairs with spike/termux_tls_probe_server.py running in Termux on the
phone/tablet. Connects over the real LAN (not loopback) to the phone's real IP address,
pins the exact self-signed cert generated for this spike (via load_verify_locations, the
same mechanism the real feature will use), and confirms a full TLS handshake plus a byte
round-trip actually works end to end -- proving the cross-platform TLS path, not just that
each side's `ssl` module imports in isolation.

Usage:
    python spike\\termux_tls_probe_client.py --host 192.0.2.51 --cert copyclip_tls_probe.pem
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="phone/tablet's real LAN IP")
    ap.add_argument("--port", type=int, default=8386)
    ap.add_argument("--cert", default="copyclip_tls_probe.pem")
    args = ap.parse_args()

    print(f"ssl module: {ssl.OPENSSL_VERSION}, TLS1.3={ssl.HAS_TLSv1_3}", flush=True)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=args.cert)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED

    print(f"dialing {args.host}:{args.port}...", flush=True)
    raw = socket.create_connection((args.host, args.port), timeout=10)
    try:
        tls_conn = ctx.wrap_socket(raw)
    except ssl.SSLCertVerificationError as exc:
        print(f"CERT VERIFICATION FAILED (this would be correct if the peer's cert "
              f"doesn't match the pinned one): {exc}", flush=True)
        return 1
    except ssl.SSLError as exc:
        print(f"TLS HANDSHAKE FAILED: {exc}", flush=True)
        return 1

    print(f"TLS handshake OK. Cipher: {tls_conn.cipher()}, "
          f"version: {tls_conn.version()}", flush=True)

    payload = b"hello-from-laptop-over-real-lan-tls"
    tls_conn.sendall(payload)
    reply = tls_conn.recv(4096)
    print(f"sent {len(payload)} bytes, received reply: {reply!r}", flush=True)
    tls_conn.close()

    if reply == b"ACK:" + payload:
        print("ROUND TRIP CONFIRMED CORRECT", flush=True)
        return 0
    print("ROUND TRIP MISMATCH -- reply did not match what was sent", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
