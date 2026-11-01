"""An Android device as a ClipboardNode.

Wraps a Transport plus the ClipBridge line protocol, and owns its **own** connect and
reconnect supervisor. That independence is the point: with three devices, the phone
rebooting or the tablet dropping off Wi-Fi must not stall anything else. Each node fails,
backs off and recovers entirely on its own.

Protocol spoken (unchanged from v1 for text, plus DESC and the chunked image verbs):
    in : READY <sdk> | CLIP <mime> <sens> <b64> | SKIP <mime> <why>
         ACK <verb> | DEFER <verb> <why> | LOCK <0|1> | DESC <mime> <sens> <ts> | ERR <msg>
         CLIPB <id> <mime> <sens> <sha256> <bytes> <nchunks> | CLIPC <id> <idx> <b64chunk>
    out: SET <b64> | GET | DESC | PING | QUIT
         SETB <id> <mime> <sha256> <bytes> <nchunks> | SETC <id> <idx> <b64chunk>

Images use SETB/SETC and CLIPB/CLIPC instead of SET/CLIP because a multi-megabyte payload
as one line would block the pump thread and starve PING/LOCK frames; text's single-line SET
path is untouched. See the image-sync implementation plan, section 5, for the full framing
rationale.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.models import ClipDescriptor, ClipPayload, WriteResult
from transports import TransportError
from transports.framing import challenge_response, seal

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.android")

TransportFactory = Callable[[], object]

# Raw bytes per chunk. ~64 KiB after base64, comfortably under any line-length limit on
# either transport (adb shell pipe or the TCP/Termux tier).
CHUNK_BYTES = 48 * 1024

# Device ACKs every ACK_WINDOW chunks during a chunked write ("ACK setc <id> <idx>"), which
# resets the stall-detection deadline. This is what keeps ack_timeout_s's "no answer in N
# seconds means the channel is dead" guarantee meaningful at any payload size: without
# it, a multi-megabyte image would only get one confirmation opportunity at the very end,
# and a channel that died halfway through would not be noticed until the whole (pointless)
# transfer finished sending.
ACK_WINDOW = 8

# How long an inbound chunked transfer can sit half-assembled with no further chunks
# arriving before it is dropped. Generous relative to ACK_WINDOW-paced transfers on a
# healthy link; exists only to bound memory/state from an abandoned transfer, since a
# fresh CLIPB for the same node already discards any in-progress one immediately.
_INBOUND_STALE_S = 30.0


@dataclass
class _InboundTransfer:
    """Reassembly state for one device-initiated chunked image (CLIPB/CLIPC)."""

    transfer_id: str
    mime: str
    sensitive: bool
    sha256: str
    total_bytes: int
    nchunks: int
    started_at: float = field(default_factory=time.time)
    last_chunk_at: float = field(default_factory=time.time)
    chunks: list = field(default_factory=list)


class AndroidNode(ClipboardNode):
    def __init__(self, name: str, transport_factory: TransportFactory, *,
                 reconnect_min_s: float = 1.0, reconnect_max_s: float = 30.0,
                 idle_ping_s: float = 60.0, ack_timeout_s: float = 4.0,
                 max_image_bytes: int = 62_914_560, image_chunk_bytes: int = CHUNK_BYTES,
                 allow_sensitive: bool = False,
                 sensitive_wipe_s: float = 10.0,
                 camera_backup: bool = True,
                 on_connect: Callable[["AndroidNode"], None] | None = None,
                 on_lock_change: Callable[["AndroidNode", bool], None] | None = None) -> None:
        super().__init__(name)
        self._factory = transport_factory
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.idle_ping_s = idle_ping_s
        self.ack_timeout_s = ack_timeout_s
        self.max_image_bytes = max_image_bytes
        self.image_chunk_bytes = image_chunk_bytes
        self.allow_sensitive = allow_sensitive
        self.sensitive_wipe_s = float(sensitive_wipe_s)
        self.camera_backup = camera_backup
        self._on_connect = on_connect
        self._on_lock_change = on_lock_change

        self._transport = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._locked = False
        self._connected = False
        self._lock = threading.Lock()
        self._mode = "full"
        self.last_error: str | None = None
        # Correlates an in-flight write() with the ACK/DEFER that answers it, so a write
        # can be confirmed rather than assumed. Only one write is ever in flight at a time
        # (the hub serialises all routing under one lock), so a single pair of fields here
        # is sufficient.
        self._ack_event = threading.Event()
        self._ack_verb: str | None = None
        # Set on every "ACK setc <id> <idx>" during a chunked write: a periodic liveness
        # signal distinct from _ack_event's final-confirmation role (see write()/ _handle()).
        self._chunk_ack_event = threading.Event()
        self._transfer_seq = 0
        # Reassembly state for an in-progress device-initiated image (single slot: only one
        # inbound image transfer at a time per node, matching the outbox's single-slot
        # design for the same reason: there is never a backlog worth keeping).
        self._inbound_transfer: _InboundTransfer | None = None
        self.key_hex: str = ""
        self.key_bytes: bytes = b""
        self.hub_host: str = ""
        self.push_port: int = 8387
        self.device_name: str = name
        self._stream_port: int | None = None

    #: state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._connected

    @property
    def locked(self) -> bool:
        return self._locked

    #: lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name=f"copyclip-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            transport, self._transport = self._transport, None
            self._connected = False
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _supervise(self) -> None:
        backoff = self.reconnect_min_s
        consecutive_failures = 0
        last_heartbeat_time = 0.0
        while not self._stop.is_set():
            try:
                transport = self._factory()
                transport.start()
            except TransportError as exc:
                self.last_error = str(exc)
                consecutive_failures += 1
                now = time.time()
                if consecutive_failures <= 3:
                    log.warning("[%s] connect failed (%d consecutive): %s", self.name, consecutive_failures, exc)
                    log.info("[%s] retrying in %.0fs", self.name, backoff)
                    last_heartbeat_time = now
                elif now - last_heartbeat_time >= 1800:
                    log.info("[%s] device offline for extended period (%d attempts), retrying in %.0fs",
                             self.name, consecutive_failures, backoff)
                    last_heartbeat_time = now
                else:
                    log.debug("[%s] connect failed (%d consecutive): %s", self.name, consecutive_failures, exc)

                effective_max = 120.0 if consecutive_failures > 5 else self.reconnect_max_s
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, effective_max)
                continue
            except Exception as exc:
                self.last_error = str(exc)
                consecutive_failures += 1
                log.exception("[%s] unexpected connect error", self.name)
                effective_max = 120.0 if consecutive_failures > 5 else self.reconnect_max_s
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, effective_max)
                continue

            consecutive_failures = 0
            backoff = self.reconnect_min_s
            self.last_error = None
            with self._lock:
                self._transport = transport
                self._connected = True
                # A fresh ClipBridge always starts in 'full'; mirror that so the domain's
                # next apply_modes() actually re-sends the intended mode.
                self._mode = "full"
            log.info("[%s] connected", self.name)
            self._send_conf()
            if self._on_connect is not None:
                # Deliberately NOT called synchronously here. on_connect (hub.py's
                # on_node_connected) can call write() as part of catching up a queued
                # outbox entry, and write() blocks on self._ack_event: which is only
                # ever set inside _handle(), which is only ever called from _pump()'s
                # loop below. Calling on_connect synchronously, before _pump() starts,
                # was a real deadlock: the ACK line would sit in transport.inbox forever
                # unread, the write would time out, write() would call
                # transport.mark_dead(), forcing a reconnect: which hit the identical
                # deadlock again on the very next connection, forever. Running it on its
                # own thread lets _pump() start reading inbox immediately, so any ACK a
                # catch-up write is waiting on actually gets delivered.
                threading.Thread(target=self._run_on_connect, name=f"copyclip-{self.name}-connect",
                                 daemon=True).start()

            try:
                self._pump(transport)
            finally:
                with self._lock:
                    self._transport = None
                    self._connected = False
                try:
                    transport.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                log.warning("[%s] channel lost; reconnecting in %.0fs", self.name, backoff)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.reconnect_max_s)

    def _run_on_connect(self) -> None:
        try:
            self._on_connect(self)
        except Exception:
            log.exception("[%s] on_connect handler failed", self.name)

    def _send_conf(self) -> None:
        """Send current configuration to ClipBridge via CONF command."""
        try:
            cfg_dict = {
                "allow_sensitive": bool(self.allow_sensitive),
                "sensitive_wipe_s": int(self.sensitive_wipe_s),
                "camera_backup": bool(self.camera_backup),
                "max_image_bytes": int(self.max_image_bytes),
            }
            if self.key_hex:
                cfg_dict["key_hex"] = self.key_hex
            if self.hub_host:
                cfg_dict["hub_host"] = self.hub_host
            if self.push_port:
                cfg_dict["push_port"] = self.push_port
            if self.device_name:
                cfg_dict["device_name"] = self.device_name
            payload = json.dumps(cfg_dict)
            b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
            with self._lock:
                transport = self._transport
                if transport is not None and transport.alive():
                    transport.send_line(f"CONF {b64}")
            log.debug("[%s] sent CONF allow_sensitive=%s wipe_s=%.0f camera_backup=%s hub=%s:%s",
                      self.name, self.allow_sensitive, self.sensitive_wipe_s, self.camera_backup,
                      self.hub_host, self.push_port)
        except Exception as exc:
            log.warning("[%s] failed to send CONF: %s", self.name, exc)

    def set_allow_sensitive(self, allow: bool) -> None:
        """Update allow_sensitive setting and resend CONF to live ClipBridge."""
        self.allow_sensitive = bool(allow)
        self._send_conf()

    def set_sensitive_wipe_s(self, wipe_s: float) -> None:
        """Update sensitive_wipe_s setting and resend CONF to live ClipBridge."""
        self.sensitive_wipe_s = float(wipe_s)
        self._send_conf()

    def set_camera_backup(self, enable: bool) -> None:
        """Update camera_backup setting and resend CONF to live ClipBridge."""
        self.camera_backup = bool(enable)
        self._send_conf()

    def _pump(self, transport) -> None:
        last_seen = time.time()
        pinged = False
        while not self._stop.is_set() and transport.alive():
            try:
                line = transport.inbox.get(timeout=1.0)
            except queue.Empty:
                idle = time.time() - last_seen
                # A half-open channel (device asleep, Tailscale re-routed) never produces
                # EOF, so probe rather than sitting on a dead pipe forever.
                if idle > self.idle_ping_s and not pinged:
                    try:
                        transport.send_line("PING")
                        pinged = True
                    except TransportError:
                        return
                elif idle > self.idle_ping_s + 30:
                    log.warning("[%s] no PING response; treating channel as dead", self.name)
                    return
                t = self._inbound_transfer
                if t is not None and time.time() - t.last_chunk_at > _INBOUND_STALE_S:
                    log.warning("[%s] abandoned inbound image transfer %s (%d/%d chunks); "
                                "dropping", self.name, t.transfer_id, len(t.chunks), t.nchunks)
                    self._inbound_transfer = None
                continue

            last_seen = time.time()
            pinged = False
            if line == "ERR transport-closed":
                return
            try:
                self._handle(line)
            except Exception:
                log.exception("[%s] frame handling failed", self.name)

    #: protocol ----------------------------------------------------------

    def _handle(self, line: str) -> None:
        verb, _, rest = line.partition(" ")

        if verb == "CLIP":
            parts = line.split(" ", 3)
            if len(parts) < 4:
                log.warning("[%s] malformed CLIP", self.name)
                return
            _, mime, sensitive, b64 = parts
            try:
                payload = ClipPayload.from_b64(b64, mime=mime, origin=self.name,
                                               sensitive=sensitive == "1")
            except Exception:
                log.exception("[%s] undecodable CLIP payload", self.name)
                return
            self._emit(payload)

        elif verb == "DESC":
            # "DESC <mime> <sens> <ts> <sha> <charlen>"
            parts = line.split(" ")
            if len(parts) < 5:
                log.warning("[%s] malformed DESC", self.name)
                return
            try:
                descriptor = ClipDescriptor(
                    node=self.name, mime=parts[1], sensitive=parts[2] == "1",
                    ts=int(parts[3]), sha256=parts[4],
                    char_len=int(parts[5]) if len(parts) > 5 else 0)
            except ValueError:
                log.warning("[%s] unparseable DESC: %s", self.name, line[:80])
                return
            self._emit_desc(descriptor)

        elif verb == "CLIPB":
            # "CLIPB <id> <mime> <sens> <sha256> <bytes> <nchunks>": header for a
            # device-initiated chunked image. A fresh id always discards any transfer
            # already in progress: there is never a backlog worth keeping (same reasoning
            # as the outbox's single-slot design).
            parts = line.split(" ")
            if len(parts) < 7:
                log.warning("[%s] malformed CLIPB", self.name)
                return
            _, transfer_id, mime, sensitive, sha, size_s, nchunks_s = parts
            try:
                size, nchunks = int(size_s), int(nchunks_s)
            except ValueError:
                log.warning("[%s] unparseable CLIPB: %s", self.name, line[:80])
                return
            if size > self.max_image_bytes:
                log.warning("[%s] CLIPB %s rejected before buffering: %d bytes > cap %d",
                            self.name, transfer_id, size, self.max_image_bytes)
                self._inbound_transfer = None
                return
            if self._inbound_transfer is not None:
                log.info("[%s] new image transfer %s supersedes in-progress %s",
                         self.name, transfer_id, self._inbound_transfer.transfer_id)
            self._inbound_transfer = _InboundTransfer(
                transfer_id=transfer_id, mime=mime, sensitive=sensitive == "1",
                sha256=sha, total_bytes=size, nchunks=nchunks)

        elif verb == "CLIPC":
            # "CLIPC <id> <idx> <b64chunk>"
            parts = line.split(" ", 3)
            if len(parts) < 4:
                log.warning("[%s] malformed CLIPC", self.name)
                return
            _, transfer_id, idx_s, chunk_b64 = parts
            t = self._inbound_transfer
            if t is None or t.transfer_id != transfer_id:
                log.debug("[%s] CLIPC for unknown/superseded transfer %s, ignoring",
                          self.name, transfer_id)
                return
            try:
                idx = int(idx_s)
            except ValueError:
                log.warning("[%s] unparseable CLIPC index", self.name)
                return
            if idx != len(t.chunks):
                log.warning("[%s] CLIPC gap in %s: expected chunk %d, got %d; aborting",
                            self.name, transfer_id, len(t.chunks), idx)
                self._inbound_transfer = None
                return
            t.chunks.append(chunk_b64)
            t.last_chunk_at = time.time()
            if len(t.chunks) == t.nchunks:
                self._finish_inbound_transfer(t)
                self._inbound_transfer = None

        elif verb == "STAMP":
            # The clipboard changed but content was deliberately not read (standby mode, or
            # the change was our own write). Carries no hash, so it is only evidence that
            # *something* happened here, which is exactly what the Continuity health check
            # needs.
            self._emit_stamp(rest.strip())

        elif verb == "LOCK":
            locked = rest.strip() == "1"
            if locked != self._locked:
                self._locked = locked
                log.info("[%s] %s", self.name,
                         "locked (writes will be queued)" if locked else "unlocked")
                if self._on_lock_change is not None:
                    try:
                        self._on_lock_change(self, locked)
                    except Exception:
                        log.exception("[%s] on_lock_change handler failed", self.name)

        elif verb == "DEFER":
            # Normal whenever the device is in your pocket; ClipBridge replays on unlock.
            log.info("[%s] write deferred (%s); will apply on unlock", self.name, rest.strip())
            # A DEFER is just as much proof the channel is alive as an ACK: the write
            # reached ClipBridge, it just needs replaying later. Confirms a pending write().
            deferred_verb = rest.strip().split(" ", 1)[0]
            if self._ack_verb is not None and deferred_verb == self._ack_verb:
                self._ack_event.set()

        elif verb == "SKIP":
            log.debug("[%s] device skipped clip: %s", self.name, rest.strip())

        elif verb == "ERR":
            reason = rest.strip()
            if reason.startswith("chunk "):
                # "ERR chunk <id> <reason>": the device aborted a chunked write it was
                # receiving (hash mismatch, oversize, gap). No fast-path abort here: the
                # in-flight write() loop's periodic ACK wait already fails within one
                # ack_timeout_s window when no further "ACK setc" arrives, which is a
                # bounded, correct outcome without adding more shared state to race on.
                log.warning("[%s] device rejected chunked write: %s", self.name, reason)
            else:
                log.warning("[%s] bridge error: %s", self.name, reason)

        elif verb == "ACK":
            # Confirms a pending write(): see the timeout-based fallback in write() for
            # why a write can no longer be assumed to have succeeded just because the local
            # pipe write didn't raise.
            acked_verb = rest.strip().split(" ", 1)[0]
            if acked_verb == "setc":
                # "ACK setc <id> <idx>": periodic liveness ping during a chunked write.
                # Resets write()'s stall-detection deadline; it is NOT the transfer's final
                # confirmation, which arrives separately as a plain "ACK set" once the
                # device has reassembled, verified and applied every chunk.
                self._chunk_ack_event.set()
            elif self._ack_verb is not None and acked_verb == self._ack_verb:
                self._ack_event.set()
            log.debug("[%s] %s", self.name, line)

        elif verb == "STREAM-PORT":
            try:
                new_port = int(rest.strip())
                if self._stream_port != new_port:
                    self._stream_port = new_port
                    log.info("[%s] direct reverse stream listener ready on port %d", self.name, self._stream_port)
                else:
                    log.debug("[%s] direct reverse stream listener confirmed on port %d", self.name, self._stream_port)
            except ValueError:
                pass

        elif verb in ("PONG", "READY", "CONF-ACK"):
            log.debug("[%s] %s", self.name, line)

        else:
            log.debug("[%s] unknown frame: %s", self.name, line[:80])

    def _finish_inbound_transfer(self, t: _InboundTransfer) -> None:
        try:
            is_cam = t.transfer_id.startswith("cam-")
            payload = ClipPayload.from_b64("".join(t.chunks), mime=t.mime, origin=self.name,
                                           sensitive=t.sensitive, is_camera=is_cam)
        except Exception:
            log.exception("[%s] undecodable chunked image payload %s", self.name,
                          t.transfer_id)
            return
        # sha256 is verified against the reassembled bytes before anything downstream ever
        # sees this payload: a gap or corruption fails closed rather than applying a
        # truncated or garbled image.
        if payload.sha256 != t.sha256:
            log.warning("[%s] chunked image %s failed sha256 verification "
                        "(expected %s..., got %s...); dropping", self.name, t.transfer_id,
                        t.sha256[:12], payload.sha256[:12])
            return
        log.debug("[%s] reassembled chunked image %s (%d bytes, %d chunks)",
                 self.name, t.transfer_id, payload.size, t.nchunks)
        self._emit(payload)

    #: io ----------------------------------------------------------------

    def write(self, payload: ClipPayload) -> str:
        if payload.is_image:
            return self._write_image(payload)

        if payload.text is None:
            return WriteResult.REJECTED
        with self._lock:
            transport = self._transport
        if transport is None or not self._connected:
            return WriteResult.UNAVAILABLE

        self._ack_event.clear()
        self._ack_verb = "set"
        cmd = "SETS" if payload.sensitive else "SET"
        try:
            transport.send_line(f"{cmd} {payload.to_b64()}")
        except TransportError as exc:
            log.warning("[%s] write failed: %s", self.name, exc)
            self._ack_verb = None
            return WriteResult.FAILED

        # A successful local pipe write proves nothing about whether the phone actually
        # got it: that pipe is to the local adb process, not to the device. Wait for
        # ClipBridge's own ACK/DEFER before calling this delivered; a real round trip over
        # this protocol measures well under a second, so a multi-second silence means the
        # channel is dead, not just slow.
        confirmed = self._ack_event.wait(timeout=self.ack_timeout_s)
        self._ack_verb = None
        if not confirmed:
            log.warning("[%s] no ACK/DEFER for SET within %.1fs; treating channel as dead",
                        self.name, self.ack_timeout_s)
            transport.mark_dead()
            return WriteResult.FAILED

        # ClipBridge answers DEFER rather than ACK when the keyguard is up, and replays it
        # itself on unlock, so a locked device is reported honestly instead of as success.
        return WriteResult.DEFERRED if self._locked else WriteResult.OK

    def _device_ip(self) -> str | None:
        with self._lock:
            transport = self._transport
        if transport is None:
            return None
        dev_str = getattr(transport, "device", None) or getattr(transport, "host", None)
        if not dev_str:
            return None
        return dev_str.split(":")[0]

    def _try_direct_stream(self, payload: ClipPayload) -> str | None:
        target_ip = self._device_ip()
        if not target_ip or not self._stream_port or not self.key_bytes:
            return None
        t0 = time.time()
        sock = None
        f = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((target_ip, self._stream_port))
            sock.settimeout(60.0)

            f = sock.makefile("rwb", buffering=0)
            hello = f.readline().decode("utf-8", "replace").strip()
            if not hello.startswith("HELLO "):
                return None
            srv_nonce = hello.split(" ", 1)[1]

            auth_resp = challenge_response(self.key_bytes, srv_nonce)
            f.write(f"AUTH {auth_resp}\n".encode("utf-8"))

            auth_ok = f.readline().decode("utf-8", "replace").strip()
            if auth_ok != "AUTH-OK":
                return None

            chall = f.readline().decode("utf-8", "replace").strip()
            if not chall.startswith("CHALLENGE "):
                return None
            my_nonce = chall.split(" ", 1)[1]

            resp = challenge_response(self.key_bytes, my_nonce)
            f.write(f"RESPONSE {resp}\n".encode("utf-8"))

            cmd = f"STREAM {self.name} {payload.mime} {1 if payload.sensitive else 0} {payload.sha256} {len(payload.data)}"
            frame = seal(self.key_bytes, 1, cmd)
            f.write(f"{frame}\n".encode("utf-8"))

            f.write(payload.data)

            res = f.readline().decode("utf-8", "replace").strip()
            if res == "STREAM-OK":
                duration = time.time() - t0
                rate_mb = (len(payload.data) / 1048576) / duration if duration > 0 else 0
                log.info("[%s] direct reverse TCP stream of %d bytes completed in %.2fs (%.2f MB/s)",
                         self.name, len(payload.data), duration, rate_mb)
                return WriteResult.DEFERRED if self._locked else WriteResult.OK
        except Exception as exc:
            log.info("[%s] direct reverse TCP stream failed (%s); falling back to ADB chunked stream",
                     self.name, exc)
            return None
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        return None

    def _write_image(self, payload: ClipPayload) -> str:
        if self._stream_port and self.key_bytes and len(payload.data) >= 131072:
            res = self._try_direct_stream(payload)
            if res is not None:
                return res
        return self._write_chunked(payload)

    def _write_chunked(self, payload: ClipPayload) -> str:
        """Image write path: SETB header + SETC chunks. Text's single-line SET path above
        is completely untouched by this: it is a parallel code path, not a shared one.
        """
        with self._lock:
            transport = self._transport
        if transport is None or not self._connected:
            return WriteResult.UNAVAILABLE

        b64 = payload.to_b64()
        chunk_chars = ((self.image_chunk_bytes + 2) // 3) * 4  # base64 chars per raw chunk
        chunks = [b64[i:i + chunk_chars] for i in range(0, len(b64), chunk_chars)] or [""]
        self._transfer_seq += 1
        transfer_id = f"{self.name}-{self._transfer_seq}"

        t0 = time.time()
        self._ack_event.clear()
        self._ack_verb = "set"
        try:
            transport.send_line(
                f"SETB {transfer_id} {payload.mime} {payload.sha256} "
                f"{payload.size} {len(chunks)}")
            for idx, chunk in enumerate(chunks):
                # A periodic wait, not one at the very end: without this, a channel that
                # died partway through a multi-megabyte image would only be noticed after
                # the whole (pointless) transfer finished sending, instead of within
                # ack_timeout_s like every other write.
                if idx > 0 and idx % ACK_WINDOW == 0:
                    chunk_timeout = max(self.ack_timeout_s, 30.0)
                    if not self._chunk_ack_event.wait(timeout=chunk_timeout):
                        log.warning(
                            "[%s] no chunk ACK within %.1fs at chunk %d/%d; "
                            "treating channel as dead", self.name, chunk_timeout,
                            idx, len(chunks))
                        transport.mark_dead()
                        self._ack_verb = None
                        return WriteResult.FAILED
                    self._chunk_ack_event.clear()
                transport.send_line(f"SETC {transfer_id} {idx} {chunk}")
        except TransportError as exc:
            log.warning("[%s] chunked write failed: %s", self.name, exc)
            self._ack_verb = None
            return WriteResult.FAILED

        final_timeout = max(self.ack_timeout_s, 60.0)
        confirmed = self._ack_event.wait(timeout=final_timeout)
        self._ack_verb = None
        duration = time.time() - t0
        mb_rate = (payload.size / 1048576) / duration if duration > 0 else 0
        if not confirmed:
            log.warning("[%s] no final ACK/DEFER for image SET within %.1fs; "
                        "treating channel as dead", self.name, final_timeout)
            transport.mark_dead()
            return WriteResult.FAILED

        log.info("[%s] chunked write of %d bytes completed in %.2fs (%.2f MB/s)",
                 self.name, payload.size, duration, mb_rate)
        return WriteResult.DEFERRED if self._locked else WriteResult.OK

    def request_refresh(self) -> None:
        with self._lock:
            transport = self._transport
        if transport is None:
            return
        try:
            transport.send_line("GET")
        except TransportError:
            pass

    def set_mode(self, mode: str) -> None:
        """Switch this device between 'full' (reads clip content) and 'stamp' (never does).

        Only one member of a Samsung sync domain needs to read a clip, because the devices
        mirror each other. Reading on both doubles the work and, with clipboard access
        alerts on, doubles the toasts for a single copy.
        """
        if mode not in ("full", "stamp"):
            raise ValueError(f"bad mode {mode!r}")
        if self._mode == mode:
            return
        with self._lock:
            transport = self._transport
        if transport is None:
            # Do NOT record the mode while disconnected. A fresh ClipBridge always starts
            # in 'full', so remembering an unsent 'stamp' here would make the next
            # apply_modes() a no-op and silently leave the device reading content forever.
            return
        try:
            transport.send_line(f"MODE {mode}")
            self._mode = mode
            log.info("[%s] read mode -> %s", self.name, mode)
        except TransportError:
            pass

    @property
    def mode(self) -> str:
        return self._mode

    def request_desc(self) -> None:
        """Ask for metadata only. Used by the Continuity health check so a probe never
        drags a 93 KB body across the wire."""
        with self._lock:
            transport = self._transport
        if transport is None:
            return
        try:
            transport.send_line("DESC")
        except TransportError:
            pass

    def send_conf(self, key_hex: str, hub_host: str, push_port: int, device_name: str) -> None:
        """Pushes auto-provisioning configuration to the device over the secure transport."""
        self.key_hex = key_hex
        self.key_bytes = key_hex.encode("utf-8") if key_hex else b""
        self.hub_host = hub_host
        self.push_port = push_port
        self.device_name = device_name
        self._send_conf()

