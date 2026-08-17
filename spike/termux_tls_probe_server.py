#!/data/data/com.termux/files/usr/bin/python
"""SPIKE, not production code -- see bugs/SPIKE-LAN-TLS-FALLBACK.md.

Runs inside Termux on the phone/tablet. Measures whether Termux's Python `ssl` module can
actually do a real TLS 1.3 server handshake over the network (not just that the module
imports -- that was already confirmed separately), before any of transports/tcp_transport.py
or termux/copyclip_node.py is touched for real, per CopyClip's "spike unproven platform
behavior before writing production code" rule.

Standalone on purpose (same reasoning as copyclip_node.py's own framing duplication): no
import from the repo, so this one file can be copied to the phone on its own.

Usage (in Termux):
    python termux_tls_probe_server.py --cert copyclip_tls_probe.pem --key copyclip_tls_probe.key
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert", default="copyclip_tls_probe.pem")
    ap.add_argument("--key", default="copyclip_tls_probe.key")
    ap.add_argument("--port", type=int, default=8386)
    args = ap.parse_args()

    print(f"ssl module: {ssl.OPENSSL_VERSION}, TLS1.3={ssl.HAS_TLSv1_3}", flush=True)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    print(f"listening on :{args.port}, waiting for one connection...", flush=True)

    conn, addr = srv.accept()
    print(f"raw TCP connection from {addr[0]}, starting TLS handshake...", flush=True)
    try:
        tls_conn = ctx.wrap_socket(conn, server_side=True)
    except ssl.SSLError as exc:
        print(f"TLS HANDSHAKE FAILED: {exc}", flush=True)
        return 1

    print(f"TLS handshake OK. Cipher: {tls_conn.cipher()}, "
          f"version: {tls_conn.version()}", flush=True)

    data = tls_conn.recv(4096)
    print(f"received {len(data)} bytes: {data!r}", flush=True)
    tls_conn.sendall(b"ACK:" + data)
    print("sent ACK, closing", flush=True)
    tls_conn.close()
    srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
