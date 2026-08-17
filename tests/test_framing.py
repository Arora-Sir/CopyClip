"""Fast unit tests for transports/framing.py and the TLS wrap in transports/tcp_transport.py.

No real device needed -- the TLS tests use real ssl.SSLContext objects over real loopback
sockets (127.0.0.1), not mocks, so they exercise the actual cryptography, just not the real
LAN/cross-platform path (that's what bugs/SPIKE-LAN-TLS-FALLBACK.md's manual spike covers,
against a real phone). This is the first test coverage for this module -- there was none
before the LAN-fallback feature (see bugs/SPIKE-LAN-TLS-FALLBACK.md).

    python tests\\test_framing.py
"""

from __future__ import annotations

import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from transports.framing import AuthError, open_frame, seal      # noqa: E402
from transports.tcp_transport import TcpTransport               # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# --------------------------------------------------------------- framing

def test_framing_round_trip() -> None:
    key = secrets.token_bytes(32)
    frame = seal(key, 1, "hello")
    seq, payload = open_frame(key, frame, last_seq=0)
    check("seal/open_frame round trip", (seq, payload) == (1, "hello"))


def test_framing_tampered_mac_rejected() -> None:
    key = secrets.token_bytes(32)
    frame = seal(key, 1, "hello")
    seq_s, mac, payload = frame.split(" ", 2)
    tampered = f"{seq_s} {'0' * len(mac)} {payload}"
    try:
        open_frame(key, tampered, last_seq=0)
        check("tampered MAC rejected", False, "did not raise")
    except AuthError:
        check("tampered MAC rejected", True)


def test_framing_replay_rejected() -> None:
    key = secrets.token_bytes(32)
    frame = seal(key, 5, "hello")
    seq, _ = open_frame(key, frame, last_seq=0)
    try:
        open_frame(key, frame, last_seq=seq)
        check("replayed sequence rejected", False, "did not raise")
    except AuthError:
        check("replayed sequence rejected", True)

    reordered = seal(key, 3, "earlier")
    try:
        open_frame(key, reordered, last_seq=seq)
        check("reordered (lower) sequence rejected", False, "did not raise")
    except AuthError:
        check("reordered (lower) sequence rejected", True)


def test_framing_wrong_key_rejected() -> None:
    key_a = secrets.token_bytes(32)
    key_b = secrets.token_bytes(32)
    frame = seal(key_a, 1, "hello")
    try:
        open_frame(key_b, frame, last_seq=0)
        check("frame sealed with a different key rejected", False, "did not raise")
    except AuthError:
        check("frame sealed with a different key rejected", True)


# --------------------------------------------------------------- TLS wrap

def _generate_cert(cn: str, out_dir: str) -> "tuple[str, str]":
    """One throwaway self-signed cert via the system openssl CLI -- same tool the real
    provisioning step in setup_termux.md uses, so this test exercises the real mechanism.
    """
    cert = os.path.join(out_dir, f"{cn}.pem")
    key = os.path.join(out_dir, f"{cn}.key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", key, "-out", cert, "-subj", f"/CN={cn}"],
        check=True, capture_output=True,
    )
    return cert, key


def _serve_one_tls_connection(cert: str, key: str, port_holder: list, ready: threading.Event,
                              reply_prefix: bytes = b"ACK:") -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    try:
        tls_conn = ctx.wrap_socket(conn, server_side=True)
        data = tls_conn.recv(4096)
        tls_conn.sendall(reply_prefix + data)
        tls_conn.close()
    except ssl.SSLError:
        pass  # expected for the untrusted-cert-rejection test
    finally:
        srv.close()


def test_tls_wrap_and_pin_round_trip() -> None:
    tmp = tempfile.mkdtemp(prefix="copyclip_test_tls_")
    try:
        cert, key = _generate_cert("copyclip-test", tmp)
        port_holder: list = []
        ready = threading.Event()
        t = threading.Thread(target=_serve_one_tls_connection, args=(cert, key, port_holder, ready))
        t.start()
        ready.wait(timeout=5)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=cert)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        raw = socket.create_connection(("127.0.0.1", port_holder[0]), timeout=5)
        tls_conn = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        tls_conn.sendall(b"hello-tls")
        reply = tls_conn.recv(4096)
        tls_conn.close()
        t.join(timeout=5)
        check("TLS handshake + pinned-cert verification + round trip", reply == b"ACK:hello-tls",
              repr(reply))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_tls_untrusted_cert_rejected_no_downgrade() -> None:
    """A server presenting a DIFFERENT cert than the one pinned must be rejected outright --
    this is the direct test of "someone else on the LAN can't impersonate the phone."
    """
    tmp = tempfile.mkdtemp(prefix="copyclip_test_tls_")
    try:
        server_cert, server_key = _generate_cert("real-server", tmp)
        wrong_cert, _wrong_key = _generate_cert("attacker", tmp)

        port_holder: list = []
        ready = threading.Event()
        t = threading.Thread(target=_serve_one_tls_connection,
                             args=(server_cert, server_key, port_holder, ready))
        t.start()
        ready.wait(timeout=5)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=wrong_cert)  # pinned to the WRONG cert on purpose
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        raw = socket.create_connection(("127.0.0.1", port_holder[0]), timeout=5)
        rejected = False
        try:
            ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        except ssl.SSLCertVerificationError:
            rejected = True
        finally:
            raw.close()
        t.join(timeout=5)
        check("untrusted cert rejected (no plaintext downgrade)", rejected)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- fallback hosts

def test_fallback_host_selection() -> None:
    calls: list = []

    def fake_create_connection(addr, timeout=None):
        host, _port = addr
        calls.append((host, timeout))
        if host == "primary-unreachable":
            raise OSError("connection refused")
        return mock.MagicMock(spec=socket.socket)

    with mock.patch("socket.create_connection", side_effect=fake_create_connection):
        t = TcpTransport("primary-unreachable", 8385, b"x" * 32,
                         fallback_hosts=("lan-fallback",))
        try:
            # Isolate host-selection from handshake/reading -- those are exercised by the
            # other tests. _read_loop is stubbed so the real (daemon) reader thread start()
            # spawns exits immediately instead of iterating a Mock socket forever.
            t._handshake = lambda: None  # noqa: SLF001
            t._read_loop = lambda: None  # noqa: SLF001
            t.inbox.put("READY 1")
            t.start()
        except Exception as exc:
            check("fallback host selection", False, repr(exc))
            return
        check("primary tried first with primary_timeout_s, fallback with the full timeout",
              calls == [("primary-unreachable", 5.0), ("lan-fallback", 15.0)], str(calls))
        check("connected_host records the winning candidate",
              t.connected_host == "lan-fallback", t.connected_host)


def test_no_fallback_configured_is_unchanged() -> None:
    """Regression check: with no fallback_hosts, behavior must be byte-identical to before
    this feature existed -- exactly one candidate, using `timeout`, not `primary_timeout_s`.
    """
    calls: list = []

    def fake_create_connection(addr, timeout=None):
        calls.append((addr, timeout))
        return mock.MagicMock(spec=socket.socket)

    with mock.patch("socket.create_connection", side_effect=fake_create_connection):
        t = TcpTransport("only-host", 8385, b"x" * 32, timeout=15.0)
        t._handshake = lambda: None  # noqa: SLF001
        t._read_loop = lambda: None  # noqa: SLF001
        t.inbox.put("READY 1")
        t.start()
        check("zero-fallback case makes exactly one connection attempt", len(calls) == 1, str(calls))
        check("zero-fallback case uses the full `timeout`, not primary_timeout_s",
              calls == [(("only-host", 8385), 15.0)], str(calls))


def main() -> int:
    for name, fn in [
        ("framing-round-trip", test_framing_round_trip),
        ("framing-tampered-mac", test_framing_tampered_mac_rejected),
        ("framing-replay", test_framing_replay_rejected),
        ("framing-wrong-key", test_framing_wrong_key_rejected),
        ("tls-round-trip", test_tls_wrap_and_pin_round_trip),
        ("tls-untrusted-cert-rejected", test_tls_untrusted_cert_rejected_no_downgrade),
        ("fallback-host-selection", test_fallback_host_selection),
        ("no-fallback-unchanged", test_no_fallback_configured_is_unchanged),
    ]:
        print(f"{name}:")
        fn()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all framing/tcp_transport unit tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
