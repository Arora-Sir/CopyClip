"""Transport abstraction for CopyClip.

A transport is a full-duplex, line-oriented channel to ClipBridge running on the phone.
It carries the plain ClipBridge protocol (READY / CLIP / SKIP / ACK / PONG / ERR inbound,
SET / GET / PING / QUIT outbound); anything a specific transport needs on the wire, such
as the HMAC framing on the TCP tier, is wrapped and unwrapped inside that transport so
the daemon above never has to care which tier is active.

Tier 1 (AdbTransport)  works today, dies on phone reboot.
Tier 2 (TcpTransport)  Termux + rish/Shizuku, survives reboot.
"""

from __future__ import annotations

import abc
import queue
import threading


class TransportError(RuntimeError):
    """Channel is unusable and should be torn down and rebuilt by the supervisor."""


class Transport(abc.ABC):
    """Full-duplex line channel.

    Inbound lines land on ``inbox`` from a background reader thread. This matters: the
    phone pushes unsolicited CLIP frames whenever the user copies, so the channel is
    asynchronous, not request/response. It is also why a naive
    "write big payload then read reply" loop deadlocks once a payload exceeds the pipe
    buffer, since nothing is draining stdout while the write blocks.
    """

    name = "transport"

    def __init__(self) -> None:
        self.inbox: "queue.Queue[str]" = queue.Queue()
        self._dead = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    def start(self) -> None:
        """Open the channel. Raises TransportError if it cannot be established."""

    @abc.abstractmethod
    def send_line(self, line: str) -> None:
        """Write one protocol line. Raises TransportError on a broken channel."""

    @abc.abstractmethod
    def close(self) -> None:
        """Tear down. Must be safe to call repeatedly and from any thread."""

    def alive(self) -> bool:
        return not self._dead.is_set()

    def _mark_dead(self) -> None:
        self._dead.set()

    # -- convenience -------------------------------------------------------

    def drain(self) -> list[str]:
        """Pop every currently queued inbound line without blocking."""
        out: list[str] = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def __enter__(self) -> "Transport":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
