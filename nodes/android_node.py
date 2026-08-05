"""An Android device as a ClipboardNode.

Wraps a Transport plus the ClipBridge line protocol, and owns its **own** connect and
reconnect supervisor. That independence is the point: with three devices, the phone
rebooting or the tablet dropping off Wi-Fi must not stall anything else. Each node fails,
backs off and recovers entirely on its own.

Protocol spoken (unchanged from v1, plus DESC):
    in : READY <sdk> | CLIP <mime> <sens> <b64> | SKIP <mime> <why>
         ACK <verb> | DEFER <verb> <why> | LOCK <0|1> | DESC <mime> <sens> <ts> | ERR <msg>
    out: SET <b64> | GET | DESC | PING | QUIT
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from core.models import ClipPayload, WriteResult
from transports import TransportError

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.android")

TransportFactory = Callable[[], object]


class AndroidNode(ClipboardNode):
    def __init__(self, name: str, transport_factory: TransportFactory, *,
                 reconnect_min_s: float = 1.0, reconnect_max_s: float = 30.0,
                 idle_ping_s: float = 60.0,
                 on_connect: Callable[["AndroidNode"], None] | None = None,
                 on_lock_change: Callable[["AndroidNode", bool], None] | None = None) -> None:
        super().__init__(name)
        self._factory = transport_factory
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.idle_ping_s = idle_ping_s
        self._on_connect = on_connect
        self._on_lock_change = on_lock_change

        self._transport = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._locked = False
        self._connected = False
        self._lock = threading.Lock()
        self.last_error: str | None = None

    # -- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._connected

    @property
    def locked(self) -> bool:
        return self._locked

    # -- lifecycle ---------------------------------------------------------

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
        while not self._stop.is_set():
            try:
                transport = self._factory()
                transport.start()
            except TransportError as exc:
                self.last_error = str(exc)
                log.warning("[%s] connect failed: %s", self.name, exc)
                log.info("[%s] retrying in %.0fs", self.name, backoff)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.reconnect_max_s)
                continue
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("[%s] unexpected connect error", self.name)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.reconnect_max_s)
                continue

            backoff = self.reconnect_min_s
            self.last_error = None
            with self._lock:
                self._transport = transport
                self._connected = True
            log.info("[%s] connected", self.name)
            if self._on_connect is not None:
                try:
                    self._on_connect(self)
                except Exception:
                    log.exception("[%s] on_connect handler failed", self.name)

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
                continue

            last_seen = time.time()
            pinged = False
            if line == "ERR transport-closed":
                return
            try:
                self._handle(line)
            except Exception:
                log.exception("[%s] frame handling failed", self.name)

    # -- protocol ----------------------------------------------------------

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

        elif verb == "SKIP":
            log.debug("[%s] device skipped clip: %s", self.name, rest.strip())

        elif verb == "ERR":
            log.warning("[%s] bridge error: %s", self.name, rest.strip())

        elif verb in ("ACK", "PONG", "READY", "DESC"):
            log.debug("[%s] %s", self.name, line)

        else:
            log.debug("[%s] unknown frame: %s", self.name, line[:80])

    # -- io ----------------------------------------------------------------

    def write(self, payload: ClipPayload) -> str:
        if payload.text is None:
            return WriteResult.REJECTED
        with self._lock:
            transport = self._transport
        if transport is None or not self._connected:
            return WriteResult.UNAVAILABLE
        try:
            transport.send_line(f"SET {payload.to_b64()}")
        except TransportError as exc:
            log.warning("[%s] write failed: %s", self.name, exc)
            return WriteResult.FAILED
        # ClipBridge answers DEFER rather than ACK when the keyguard is up, and replays it
        # itself on unlock, so a locked device is reported honestly instead of as success.
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
