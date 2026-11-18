"""The one interface the hub is allowed to know about.

Everything device-specific lives behind this: adb, Termux, ctypes, the ClipBridge line
protocol, keyguard detection. The hub never imports a transport or a platform API, which
is what makes adding a fourth device (or an image codec) a new subclass rather than an
edit to the routing logic.
"""

from __future__ import annotations

import abc
import logging
from typing import Callable

from core.models import ClipDescriptor, ClipPayload

log = logging.getLogger("copyclip.node")

ClipCallback = Callable[[ClipPayload], None]
DescCallback = Callable[[ClipDescriptor], None]
StampCallback = Callable[[str, str], None]   # (node_name, timestamp)


class ClipboardNode(abc.ABC):
    """A clipboard we can read from and write to."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._on_clip: ClipCallback | None = None
        self._on_desc: DescCallback | None = None
        self._on_stamp: StampCallback | None = None

    # -- wiring ------------------------------------------------------------

    def subscribe(self, callback: ClipCallback, on_desc: DescCallback | None = None,
                  on_stamp: StampCallback | None = None) -> None:
        """Register the hub's handlers for clips and change announcements."""
        self._on_clip = callback
        self._on_desc = on_desc
        self._on_stamp = on_stamp

    def _emit(self, payload: ClipPayload) -> None:
        if self._on_clip is None:
            return
        try:
            self._on_clip(payload)
        except Exception:
            log.exception("[%s] clip callback failed", self.name)

    def _emit_desc(self, descriptor: ClipDescriptor) -> None:
        if self._on_desc is None:
            return
        try:
            self._on_desc(descriptor)
        except Exception:
            log.exception("[%s] descriptor callback failed", self.name)

    def _emit_stamp(self, stamp: str) -> None:
        """A change happened here but content was not read, so there is no hash."""
        if self._on_stamp is None:
            return
        try:
            self._on_stamp(self.name, stamp)
        except Exception:
            log.exception("[%s] stamp callback failed", self.name)

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    def start(self) -> None:
        """Begin observing. Must not block; must not raise if the device is offline."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Tear down. Safe to call repeatedly and from any thread."""

    # -- state -------------------------------------------------------------

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """True when the node can currently accept a write."""

    @property
    def locked(self) -> bool:
        """True when the device is locked and will silently discard writes.

        Only Android has a meaningful answer here; the default suits desktop nodes.
        """
        return False

    @property
    def writable(self) -> bool:
        return self.available and not self.locked

    # -- io ----------------------------------------------------------------

    @abc.abstractmethod
    def write(self, payload: ClipPayload) -> str:
        """Apply `payload` to this clipboard. Returns a WriteResult constant."""

    def request_refresh(self) -> None:
        """Ask the node to re-emit its current clip.

        Used for catch-up on reconnect. The Android clipboard holds exactly one item, so
        re-reading it *is* the correct catch-up: there is no backlog to replay.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} available={self.available}>"
