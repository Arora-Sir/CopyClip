"""Shared in-memory test doubles. No clipboard, no sockets, no devices."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.models import ClipPayload, WriteResult  # noqa: E402
from nodes.base import ClipboardNode  # noqa: E402


class FakeClock:
    """Controllable replacement for time.time. Use with unittest.mock.patch."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNode(ClipboardNode):
    """Minimal in-memory ClipboardNode that records every write."""

    def __init__(self, name: str, available: bool = True, locked: bool = False) -> None:
        super().__init__(name)
        self._available = available
        self._locked = locked
        self.writes: list[ClipPayload] = []
        self.result = WriteResult.OK
        self.refreshes = 0
        self.mode = "full"

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def available(self) -> bool:
        return self._available

    @property
    def locked(self) -> bool:
        return self._locked

    def write(self, payload: ClipPayload) -> str:
        self.writes.append(payload)
        return self.result

    def request_refresh(self) -> None:
        self.refreshes += 1


class FakeTransport:
    """Records sent lines, never replies."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._dead = False

    def alive(self) -> bool:
        return not self._dead

    def mark_dead(self) -> None:
        self._dead = True

    def send_line(self, line: str) -> None:
        self.sent.append(line)

    def close(self) -> None:
        pass
