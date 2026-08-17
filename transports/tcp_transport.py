"""Tier 2 transport: TCP to a Termux-hosted node on the phone, over Tailscale, with an
optional same-WiFi LAN fallback for when Tailscale itself is unreachable (see
`bugs/SPIKE-LAN-TLS-FALLBACK.md` -- Android's Doze mode was found to silently kill
Tailscale's background connection for hours at a time, which is what motivated this).

Exists because wireless ADB does not survive a phone reboot on this device
(`service.adb.tcp.port` is set, `persist.adb.tcp.port` is not, and making it persistent
needs root). Shizuku does survive reboots, so this tier reaches ClipBridge through `rish`
instead of `adb`.

Same dex, same ClipBridge protocol as Tier 1. Only the pipe differs, plus HMAC framing
(`framing.py`) because this one is a real socket other devices could reach.

Confidentiality: the Tailscale-only path relies entirely on Tailscale's own WireGuard
tunnel, same as before. The optional `lan_host` fallback has no such tunnel underneath it,
so it MUST be paired with `tls_cert_file` -- see `start()` below, which never falls back to
a plaintext socket once TLS is configured. `framing.py`'s HMAC layer keeps running either
way, as defense in depth.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading

from . import Transport, TransportError
from .framing import AuthError, open_frame, seal, verify_challenge

log = logging.getLogger("copyclip.tcp")


class TcpTransport(Transport):
    name = "tcp"

    def __init__(self, host: str, port: int, key: bytes, timeout: float = 15.0, *,
                 fallback_hosts: "tuple[str, ...]" = (), primary_timeout_s: float = 5.0,
                 tls_cert_file: "str | None" = None) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.key = key
        self.timeout = timeout
        # Tried in order after `host`, e.g. a same-WiFi lan_host when the Tailscale address
        # (host) is unreachable -- typically because Tailscale itself dropped, not the LAN.
        self.fallback_hosts = tuple(fallback_hosts)
        # Deliberately shorter than `timeout`: a dead Tailscale connection should fail fast
        # into the fallback rather than sitting out the full `timeout` first. Only the last
        # candidate tried uses `timeout` -- see start(). With no fallback_hosts configured,
        # exactly one candidate is tried with `timeout`, unchanged from before this existed.
        self.primary_timeout_s = primary_timeout_s
        # Path to a pinned self-signed cert (PEM). When set, every candidate connection is
        # wrapped in TLS and verified against this exact cert -- required before lan_host is
        # safe to use, since there is no Tailscale tunnel underneath that path. No candidate
        # ever silently downgrades to plaintext when this is configured; a handshake or
        # cert-verification failure is a hard error, not a reason to try the next host as if
        # it were an ordinary connection failure.
        self.tls_cert_file = tls_cert_file
        self._sock: "socket.socket | ssl.SSLSocket | None" = None
        self._file = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._tx_seq = 0
        self._rx_seq = 0
        # Which candidate host actually succeeded -- surfaced for diagnostics, so it's
        # visible in logs whether a sync happened over Tailscale or the LAN fallback.
        self.connected_host: "str | None" = None

    def start(self) -> None:
        candidates = (self.host, *self.fallback_hosts)
        last_exc: Exception | None = None
        for i, host in enumerate(candidates):
            is_last = i == len(candidates) - 1
            timeout = self.timeout if is_last else self.primary_timeout_s
            try:
                sock = socket.create_connection((host, self.port), timeout=timeout)
            except OSError as exc:
                last_exc = exc
                log.info("tcp candidate %s:%s unreachable (%s)%s", host, self.port, exc,
                         "" if is_last else "; trying next candidate")
                continue

            if self.tls_cert_file:
                try:
                    sock = self._wrap_tls(sock, host)
                except ssl.SSLError as exc:
                    # Not a "try the next candidate" situation: a cert/handshake failure on
                    # a reachable host means someone answered who isn't the pinned peer --
                    # exactly the scenario this feature exists to catch, not a blip to paper
                    # over by silently moving on.
                    sock.close()
                    raise TransportError(
                        f"TLS verification failed for {host}:{self.port}: {exc}") from exc

            self._sock = sock
            self.connected_host = host
            break
        else:
            raise TransportError(
                f"cannot reach any of {candidates} on port {self.port}: {last_exc}")

        self._sock.settimeout(None)
        # TCP_NODELAY matters here: clipboard frames are small and latency sensitive, and
        # Nagle would otherwise sit on them waiting for more data. Works the same on an
        # SSLSocket -- it wraps the same underlying fd.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rwb", buffering=0)

        try:
            self._handshake()
        except Exception:
            self.close()
            raise

        if self.connected_host == self.host:
            log.info("ClipBridge reachable via primary address %s", self.host)
        else:
            log.warning("ClipBridge reachable only via LAN fallback %s (primary %s failed)",
                        self.connected_host, self.host)

        self._reader = threading.Thread(target=self._read_loop, name="copyclip-tcp-reader",
                                        daemon=True)
        self._reader.start()

        first = self.inbox.get(timeout=self.timeout)
        if not first.startswith("READY"):
            self.close()
            raise TransportError(f"unexpected greeting: {first!r}")
        log.info("ClipBridge up via Termux on %s:%s (%s)", self.connected_host, self.port, first)

    def _wrap_tls(self, sock: socket.socket, host: str) -> ssl.SSLSocket:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=self.tls_cert_file)
        # No real DNS name to match -- trust is "is this the one pinned cert", not "does the
        # name match", so hostname checking is meaningless here and must be off for
        # verify_mode=CERT_REQUIRED to work against an IP-addressed self-signed cert.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx.wrap_socket(sock, server_hostname=host)

    def _handshake(self) -> None:
        """Mutual proof of the shared key before any clipboard data moves."""
        assert self._file is not None
        line = self._file.readline().decode("utf-8", "replace").strip()
        if not line.startswith("HELLO "):
            raise TransportError(f"bad handshake greeting: {line!r}")
        nonce = line.split(" ", 1)[1]

        from .framing import challenge_response
        self._file.write((f"AUTH {challenge_response(self.key, nonce)}\n").encode())

        reply = self._file.readline().decode("utf-8", "replace").strip()
        if reply != "AUTH-OK":
            raise TransportError(f"authentication rejected: {reply!r}")

        # Now prove the server holds the key too, so a rogue listener cannot harvest clips.
        my_nonce = self._new_nonce()
        self._file.write(f"CHALLENGE {my_nonce}\n".encode())
        resp = self._file.readline().decode("utf-8", "replace").strip()
        if not resp.startswith("RESPONSE ") or not verify_challenge(
                self.key, my_nonce, resp.split(" ", 1)[1]):
            raise TransportError("server failed our challenge; wrong key or impostor")

    @staticmethod
    def _new_nonce() -> str:
        from .framing import new_nonce
        return new_nonce()

    def _read_loop(self) -> None:
        try:
            assert self._file is not None
            for raw in self._file:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    self._rx_seq, payload = open_frame(self.key, line, self._rx_seq)
                except AuthError as exc:
                    log.error("dropping unauthenticated frame: %s", exc)
                    break
                if payload:
                    self.inbox.put(payload)
        except Exception as exc:
            log.debug("tcp reader ended: %s", exc)
        finally:
            self._mark_dead()
            self.inbox.put("ERR transport-closed")

    def send_line(self, line: str) -> None:
        if self._file is None or not self.alive():
            raise TransportError("tcp channel is not open")
        try:
            with self._write_lock:
                self._tx_seq += 1
                self._file.write((seal(self.key, self._tx_seq, line) + "\n").encode("utf-8"))
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._mark_dead()
            raise TransportError(f"tcp write failed: {exc}") from exc

    def close(self) -> None:
        self._mark_dead()
        for obj in (self._file, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._file = None
        self._sock = None
