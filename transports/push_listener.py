"""Inbound tier: a device pushes ONE clip to the hub, instead of the hub polling it.

Every other transport has the hub dialing OUT to a device's bridge (TcpTransport,
AdbTransport). This is the one direction that runs backwards, and exists for exactly one
case: a device Android will not grant background clipboard access to (no shell UID
available: see docs/bugs/SPIKE-PIXEL-ACCESSIBILITY.md) can still push a clip in response to a
user action, because being the foreground app when the user taps "Send Clip" is a real,
unprivileged read exemption Android already grants, confirmed live against this project's
own ClipboardService logs. Same HMAC mutual handshake as tcp_transport.py, just played from
the opposite role: framing.py's primitives are direction-agnostic on purpose.

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

import hashlib
import hmac
import logging
import socket
import threading

from typing import TYPE_CHECKING

from core.models import ClipPayload

if TYPE_CHECKING:   # annotation only: a transport must not depend on the routing hub
    from core.hub import Hub

from .framing import AuthError, challenge_response, new_nonce, open_frame, verify_challenge

log = logging.getLogger("copyclip.push")

# Longest line accepted at each stage. Handshake lines are tiny; the frame line carries a
# whole base64 text clip, so it gets room for the 800k-character default plus overhead.
MAX_HANDSHAKE_LINE = 256
MAX_FRAME_LINE = 8 * 1024 * 1024

# Node names a remote peer may never claim: a clip "from" the laptop would be written back
# to every device as if the user had copied it on this PC.
RESERVED_NODE_NAMES = frozenset({"laptop"})


def _read_line(f, limit: int) -> str | None:
    """One newline-terminated line, or None if it is longer than `limit` (hostile peer)."""
    raw = f.readline(limit + 1)
    if len(raw) > limit and not raw.endswith(b"\n"):
        return None
    return raw.decode("utf-8", "replace").strip()


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
        # One push per connection, so a hung or hostile peer costs one thread and a timeout,
        # never a wedged shared listener. 60s timeout allows multi-megabyte image streams to finish cleanly.
        conn.settimeout(60.0)
        f = None
        try:
            f = conn.makefile("rwb", buffering=0)
            nonce = new_nonce()
            f.write(f"HELLO {nonce}\n".encode())

            auth_line = _read_line(f, MAX_HANDSHAKE_LINE)
            expected = f"AUTH {challenge_response(self.key, nonce)}"
            if auth_line is None or not hmac.compare_digest(auth_line.encode(), expected.encode()):
                f.write(b"AUTH-FAIL\n")
                log.warning("push from %s: bad AUTH, dropping", addr[0])
                return
            f.write(b"AUTH-OK\n")

            my_nonce = new_nonce()
            f.write(f"CHALLENGE {my_nonce}\n".encode())
            resp_line = _read_line(f, MAX_HANDSHAKE_LINE)
            if resp_line is None or not resp_line.startswith("RESPONSE ") or not verify_challenge(
                    self.key, my_nonce, resp_line.split(" ", 1)[1]):
                log.warning("push from %s: failed our challenge; wrong key or impostor",
                            addr[0])
                return

            frame = _read_line(f, MAX_FRAME_LINE)
            if not frame:
                if frame is None:
                    log.warning("push from %s: frame line too long, dropping", addr[0])
                return
            try:
                _, payload_line = open_frame(self.key, frame, 0)
            except AuthError as exc:
                log.warning("push from %s: %s", addr[0], exc)
                return
            if payload_line.startswith("STREAM "):
                self._handle_stream(payload_line, f, addr)
            else:
                self._handle_push(payload_line, f)
        except (OSError, ValueError) as exc:
            log.debug("push connection from %s ended: %s", addr[0], exc)
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _handle_stream(self, line: str, f, addr) -> None:
        parts = line.split(" ")
        if len(parts) < 6:
            f.write(b"STREAM-FAIL malformed\n")
            return
        if parts[1] in RESERVED_NODE_NAMES:
            log.warning("direct stream from %s claimed reserved node name %r", addr[0], parts[1])
            f.write(b"STREAM-FAIL reserved_name\n")
            return
        if len(parts) == 6:
            _, node, mime, sensitive, expected_sha, total_bytes_s = parts
            is_camera = False
        else:
            _, node, mime, sensitive, is_camera_s, expected_sha, total_bytes_s = parts[:7]
            is_camera = (is_camera_s == "1" or is_camera_s.lower() == "true")

        try:
            total_bytes = int(total_bytes_s)
        except ValueError:
            f.write(b"STREAM-FAIL bad_length\n")
            return

        max_allowed = getattr(self.hub.policy, "max_image_bytes", 62914560)
        if total_bytes > max_allowed:
            log.warning("direct stream from %s exceeds cap (%d > %d)", node, total_bytes, max_allowed)
            f.write(b"STREAM-FAIL oversize\n")
            return

        hasher = hashlib.sha256()
        buf = bytearray()
        remaining = total_bytes
        chunk_size = 262144
        try:
            while remaining > 0:
                chunk = f.read(min(remaining, chunk_size))
                if not chunk:
                    log.warning("direct stream from %s truncated (read %d of %d bytes)",
                                node, len(buf), total_bytes)
                    f.write(b"STREAM-FAIL truncated\n")
                    return
                buf.extend(chunk)
                hasher.update(chunk)
                remaining -= len(chunk)
        except Exception as exc:
            log.warning("direct stream from %s socket read error: %s", node, exc)
            f.write(b"STREAM-FAIL read_error\n")
            return

        actual_sha = hasher.hexdigest()
        if actual_sha.lower() != expected_sha.lower():
            log.warning("direct stream from %s sha mismatch (got %s, expected %s)",
                        node, actual_sha, expected_sha)
            f.write(b"STREAM-FAIL sha_mismatch\n")
            return

        raw_data = bytes(buf)
        payload = ClipPayload(
            mime=mime,
            data=raw_data,
            origin=node,
            sensitive=(sensitive == "1"),
            is_camera=is_camera,
        )
        log.info("[%s] direct TCP stream received (%d bytes, sha=%s, camera=%s)",
                 node, len(raw_data), payload.short, is_camera)
        self.hub.on_clip(payload)
        f.write(b"STREAM-OK\n")

    def _handle_push(self, line: str, f) -> None:
        parts = line.split(" ", 4)
        if len(parts) < 5 or parts[0] != "PUSH":
            f.write(b"PUSH-FAIL malformed\n")
            return
        if parts[1] in RESERVED_NODE_NAMES:
            log.warning("push claimed reserved node name %r", parts[1])
            f.write(b"PUSH-FAIL reserved_name\n")
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
