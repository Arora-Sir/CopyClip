"""Inbound tier: a device pushes ONE clip to the hub, instead of the hub polling it.

Every other transport has the hub dialing OUT to a device's bridge (TcpTransport,
AdbTransport). This is the one direction that runs backwards, and exists for exactly one
case: a device Android will not grant background clipboard access to (no shell UID
available -- see bugs/SPIKE-PIXEL-ACCESSIBILITY.md) can still push a clip in response to a
user action, because being the foreground app when the user taps "Send Clip" is a real,
unprivileged read exemption Android already grants, confirmed live against this project's
own ClipboardService logs. Same HMAC mutual handshake as tcp_transport.py, just played from
the opposite role -- framing.py's primitives are direction-agnostic on purpose.

Wire, one connection per push:
    hub  -> HELLO <nonce>
    dev  -> AUTH <resp>            (proves the device holds the key)
    hub  -> AUTH-OK | AUTH-FAIL
    hub  -> CHALLENGE <nonce>
    dev  -> RESPONSE <resp>        (proves the hub holds the key too)
    dev  -> <seq> <mac> PUSH <node> <mime> <sens> <b64>   (one HMAC-framed line, see framing.py)
    hub  -> PUSH-OK | PUSH-FAIL <reason>
"""

from __future__ import annotations

import logging
import socket
import threading

from core.hub import Hub
from core.models import ClipPayload

from .framing import AuthError, challenge_response, new_nonce, open_frame, verify_challenge

log = logging.getLogger("copyclip.push")


class PushListener:
    def __init__(self, port: int, key: bytes, hub: Hub) -> None:
        self.port = port
        self.key = key
        self.hub = hub
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(4)
        self._sock = srv
        self._thread = threading.Thread(target=self._accept_loop, name="copyclip-push",
                                        daemon=True)
        self._thread.start()
        log.info("push listener up on :%s", self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True,
                             name="copyclip-push-client").start()

    def _serve(self, conn: socket.socket, addr) -> None:
        # One push per connection, so a hung or hostile peer costs one thread and a 20s
        # timeout, never a wedged shared listener -- there is no persistent state here for
        # a slow client to stall. 20s not 10s: an image push arrives as one unchunked line
        # (see ARCHITECTURE.md's Send Clip section), a slow link needs more room than a
        # few-byte text push ever did.
        conn.settimeout(20.0)
        f = None
        try:
            f = conn.makefile("rwb", buffering=0)
            nonce = new_nonce()
            f.write(f"HELLO {nonce}\n".encode())

            auth_line = f.readline().decode("utf-8", "replace").strip()
            if auth_line != f"AUTH {challenge_response(self.key, nonce)}":
                f.write(b"AUTH-FAIL\n")
                log.warning("push from %s: bad AUTH, dropping", addr[0])
                return
            f.write(b"AUTH-OK\n")

            my_nonce = new_nonce()
            f.write(f"CHALLENGE {my_nonce}\n".encode())
            resp_line = f.readline().decode("utf-8", "replace").strip()
            if not resp_line.startswith("RESPONSE ") or not verify_challenge(
                    self.key, my_nonce, resp_line.split(" ", 1)[1]):
                log.warning("push from %s: failed our challenge; wrong key or impostor",
                            addr[0])
                return

            frame = f.readline().decode("utf-8", "replace").strip()
            if not frame:
                return
            try:
                _, payload_line = open_frame(self.key, frame, 0)
            except AuthError as exc:
                log.warning("push from %s: %s", addr[0], exc)
                return
            self._handle_push(payload_line, f)
        except (OSError, ValueError) as exc:
            log.debug("push connection from %s ended: %s", addr[0], exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_push(self, line: str, f) -> None:
        parts = line.split(" ", 4)
        if len(parts) < 5 or parts[0] != "PUSH":
            f.write(b"PUSH-FAIL malformed\n")
            return
        _, node, mime, sensitive, b64 = parts
        try:
            payload = ClipPayload.from_b64(b64, mime=mime, origin=node,
                                           sensitive=sensitive == "1")
        except Exception:
            log.exception("push from %s: undecodable payload", node)
            f.write(b"PUSH-FAIL undecodable\n")
            return
        log.info("[%s] manual push (%d chars)", node, payload.char_len)
        self.hub.on_clip(payload)
        f.write(b"PUSH-OK\n")
