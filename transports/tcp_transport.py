"""Tier 2 transport: TCP to a Termux-hosted node on the phone, over Tailscale.

Exists because wireless ADB does not survive a phone reboot on this device
(`service.adb.tcp.port` is set, `persist.adb.tcp.port` is not, and making it persistent
needs root). Shizuku does survive reboots, so this tier reaches ClipBridge through `rish`
instead of `adb`.

Same dex, same ClipBridge protocol as Tier 1. Only the pipe differs, plus HMAC framing
because this one is a real socket other tailnet devices could reach.
"""

from __future__ import annotations

import logging
import socket
import threading

from . import Transport, TransportError
from .framing import AuthError, open_frame, seal, verify_challenge

log = logging.getLogger("copyclip.tcp")


class TcpTransport(Transport):
    name = "tcp"

    def __init__(self, host: str, port: int, key: bytes, timeout: float = 15.0) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.key = key
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._tx_seq = 0
        self._rx_seq = 0

    def start(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError(f"cannot reach {self.host}:{self.port}: {exc}") from exc

        self._sock.settimeout(None)
        # TCP_NODELAY matters here: clipboard frames are small and latency sensitive, and
        # Nagle would otherwise sit on them waiting for more data.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rwb", buffering=0)

        try:
            self._handshake()
        except Exception:
            self.close()
            raise

        self._reader = threading.Thread(target=self._read_loop, name="copyclip-tcp-reader",
                                        daemon=True)
        self._reader.start()

        first = self.inbox.get(timeout=self.timeout)
        if not first.startswith("READY"):
            self.close()
            raise TransportError(f"unexpected greeting: {first!r}")
        log.info("ClipBridge up via Termux on %s:%s (%s)", self.host, self.port, first)

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
