"""Fast unit tests for AndroidNode.write()'s ACK-confirmation logic. No real devices.

Regression coverage for: write() used to report success the moment the local pipe write
to `adb shell` didn't raise, without ever checking whether the phone actually received and
applied the SET. That let a genuinely unreachable phone be recorded as having delivered
content it never got. These tests exercise the fix with a fake transport that never
auto-replies, so nothing here depends on real hardware or timing outside our control.

    python tests\\test_android_node.py
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logging.disable(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.models import ClipPayload, WriteResult  # noqa: E402
from nodes.android_node import AndroidNode        # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeTransport:
    """Minimal Transport double: records sent lines, never auto-replies."""

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


def _wired_node(ack_timeout_s: float = 4.0) -> tuple[AndroidNode, FakeTransport]:
    node = AndroidNode("phone", transport_factory=lambda: None, ack_timeout_s=ack_timeout_s)
    fake = FakeTransport()
    node._transport = fake
    node._connected = True
    return node, fake


def test_ack_confirms_write():
    node, fake = _wired_node()
    p = ClipPayload.from_text("hello", origin="laptop")

    result_box: list[str] = []

    def do_write():
        result_box.append(node.write(p))

    t = threading.Thread(target=do_write)
    t.start()
    time.sleep(0.05)   # let write() send SET and start waiting on the ack event
    node._handle("ACK set")
    t.join(timeout=2)

    check("SET was actually sent", any(s.startswith("SET ") for s in fake.sent))
    check("write() returns OK once ACK arrives", result_box and result_box[0] == WriteResult.OK)
    check("transport was not marked dead", fake.alive() is True)


def test_defer_confirms_write_on_locked_device():
    node, fake = _wired_node()
    node._locked = True
    p = ClipPayload.from_text("hello while locked", origin="laptop")

    result_box: list[str] = []

    def do_write():
        result_box.append(node.write(p))

    t = threading.Thread(target=do_write)
    t.start()
    time.sleep(0.05)
    node._handle("DEFER set locked")
    t.join(timeout=2)

    check("write() returns DEFERRED once DEFER arrives",
          result_box and result_box[0] == WriteResult.DEFERRED)
    check("transport was not marked dead (DEFER proves the channel is alive)",
          fake.alive() is True)


def test_no_answer_fails_and_marks_transport_dead():
    node, fake = _wired_node(ack_timeout_s=0.05)
    p = ClipPayload.from_text("hello into the void", origin="laptop")

    result = node.write(p)

    check("write() returns FAILED when nothing ever answers", result == WriteResult.FAILED)
    check("transport is marked dead so the supervisor notices promptly",
          fake.alive() is False)


def main() -> int:
    for name, fn in [("ack-confirms-write", test_ack_confirms_write),
                     ("defer-confirms-write", test_defer_confirms_write_on_locked_device),
                     ("no-answer-fails", test_no_answer_fails_and_marks_transport_dead)]:
        print(f"{name}:")
        fn()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all android_node unit tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
