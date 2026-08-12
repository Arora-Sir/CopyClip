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
import queue
import sys
import threading
import time

logging.disable(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import base64      # noqa: E402
import hashlib     # noqa: E402

from core.models import ClipPayload, WriteResult          # noqa: E402
from nodes.android_node import AndroidNode, ACK_WINDOW, CHUNK_BYTES  # noqa: E402

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


class AutoAckTransport:
    """A transport double that exercises AndroidNode's REAL threading (start() ->
    _supervise() -> on_connect -> _pump()), unlike FakeTransport above which is wired
    directly into an already-"connected" node and never touches _supervise() at all.

    Has a real inbox Queue (as _pump() expects) and a background responder thread that
    auto-ACKs whatever gets sent, simulating ClipBridge answering over the wire. This is
    what makes it possible to reproduce -- and prove the fix for -- a bug that is purely
    about *thread ordering*, not about ACK-matching logic in isolation.
    """

    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list[str] = []
        self._alive = True
        self._answered_setc = 0

    def start(self) -> None:
        self.inbox.put("READY 36")
        threading.Thread(target=self._respond_loop, daemon=True).start()

    def alive(self) -> bool:
        return self._alive

    def mark_dead(self) -> None:
        self._alive = False

    def send_line(self, line: str) -> None:
        self.sent.append(line)

    def close(self) -> None:
        self._alive = False

    def _respond_loop(self) -> None:
        answered_set = False
        while self._alive:
            if self.sent and not answered_set:
                last = self.sent[-1]
                if last.startswith("SET ") or (
                    last.startswith("SETC ") and self._is_last_chunk(last)):
                    answered_set = True
                    self.inbox.put("ACK set")
            time.sleep(0.01)

    def _is_last_chunk(self, line: str) -> bool:
        # "SETC <id> <idx> <chunk>" -- treat any SETC as the last one for this simple
        # single-write test scenario (only one write is ever exercised per test).
        return True


def test_write_triggered_from_on_connect_does_not_deadlock():
    """Regression for the real bug found in production use: on_connect (which is
    hub.on_node_connected in the real daemon, catching up a queued outbox entry via
    write()) used to be called synchronously BEFORE _pump() started, so write()'s
    self._ack_event.wait() could never be satisfied -- the ACK sat unread in
    transport.inbox until _pump() eventually started, by which point write() had already
    timed out, called transport.mark_dead(), and forced a reconnect that hit the
    identical deadlock again. Infinite loop, 100% reproducible, once anything is queued
    at connect time. Fixed by running on_connect on its own thread so _pump() can start
    draining inbox immediately. This test fails hard (times out) against the pre-fix code.
    """
    result_box: list[str] = []
    connect_event = threading.Event()

    def on_connect(node: AndroidNode) -> None:
        # Mirrors hub.on_node_connected() calling write() to catch up a queued entry.
        p = ClipPayload.from_text("catch-up write triggered at connect time", origin="laptop")
        result_box.append(node.write(p))
        connect_event.set()

    node = AndroidNode("phone", transport_factory=AutoAckTransport,
                       ack_timeout_s=2.0, on_connect=on_connect)
    node.start()
    try:
        completed = connect_event.wait(timeout=5.0)
        check("on_connect's write() completes at all (no deadlock)", completed)
        check("on_connect's write() actually succeeded, not timed out",
              result_box and result_box[0] == WriteResult.OK, result_box)
        check("node is still connected (write() did not spuriously mark the transport dead)",
              node.available)
    finally:
        node.stop()


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


# ---------------------------------------------------------------- chunked images

def test_chunked_write_large_image():
    """The image write path (SETB/SETC) is a parallel code path to text's single-line SET
    above -- this exercises it end to end against a fake transport.
    """
    node, fake = _wired_node()
    data = bytes((i % 256) for i in range(int(CHUNK_BYTES * 3.5)))
    p = ClipPayload(mime="image/png", data=data, origin="laptop")

    result_box: list[str] = []

    def do_write():
        result_box.append(node.write(p))

    t = threading.Thread(target=do_write)
    t.start()
    time.sleep(0.1)

    setb = [s for s in fake.sent if s.startswith("SETB ")]
    setc = [s for s in fake.sent if s.startswith("SETC ")]
    check("exactly one SETB header sent", len(setb) == 1, setb)
    _, transfer_id, mime, sha, size_s, nchunks_s = setb[0].split(" ")
    check("SETB mime correct", mime == "image/png")
    check("SETB sha256 correct", sha == p.sha256)
    check("SETB size correct", int(size_s) == len(data))

    node._handle("ACK set")
    t.join(timeout=2)
    check("write() returns OK once final ACK arrives",
          result_box and result_box[0] == WriteResult.OK)
    check("SETC indices sent in order with no gaps",
          [s.split(" ")[2] for s in setc] == [str(i) for i in range(len(setc))])
    check("chunk count matches the header's nchunks", len(setc) == int(nchunks_s))

    reassembled = base64.b64decode("".join(s.split(" ", 3)[3] for s in setc))
    check("reassembled chunk bytes match the original payload", reassembled == data)


def test_periodic_chunk_ack_resets_stall_deadline():
    """A tiny ack_timeout_s must still succeed on a large image PURELY because periodic
    'ACK setc' resets the deadline each window -- not because the timeout happens to be
    long enough to cover the whole transfer. This is what keeps write()'s dead-channel
    detection meaningful at any payload size (see ARCHITECTURE.md §6.7a).
    """
    node, fake = _wired_node(ack_timeout_s=0.15)
    data = bytes(CHUNK_BYTES * 24)  # -> exactly 24 chunks -> periodic waits at idx 8, 16
    p = ClipPayload(mime="image/png", data=data, origin="laptop")

    result_box: list[str] = []
    stop = threading.Event()

    def do_write():
        result_box.append(node.write(p))

    def auto_respond():
        answered = 0
        while not stop.is_set():
            n_setc = sum(1 for s in fake.sent if s.startswith("SETC "))
            if n_setc > answered:
                answered = n_setc
                node._handle(f"ACK setc x {answered - 1}")
            if any(s.startswith("SETB ") for s in fake.sent) and answered == 24:
                node._handle("ACK set")
                return
            time.sleep(0.005)

    t = threading.Thread(target=do_write)
    r = threading.Thread(target=auto_respond, daemon=True)
    t.start()
    r.start()
    t.join(timeout=5)
    stop.set()
    check("write() survives a tiny timeout purely via periodic ACKs",
          result_box and result_box[0] == WriteResult.OK, result_box)
    check("transport not marked dead", fake.alive() is True)


def test_chunked_write_times_out_like_text():
    node, fake = _wired_node(ack_timeout_s=0.05)
    p = ClipPayload(mime="image/png", data=bytes(CHUNK_BYTES * 2), origin="laptop")
    result = node.write(p)
    check("no answer -> FAILED, same guarantee as the text path", result == WriteResult.FAILED)
    check("transport marked dead so the supervisor notices promptly", fake.alive() is False)


def test_inbound_chunked_reassembly():
    node, fake = _wired_node()
    received: list[ClipPayload] = []
    node.subscribe(lambda payload: received.append(payload))

    data = bytes((i * 7) % 256 for i in range(int(CHUNK_BYTES * 2.3)))
    b64 = base64.b64encode(data).decode("ascii")
    chunk_chars = ((CHUNK_BYTES + 2) // 3) * 4
    chunks = [b64[i:i + chunk_chars] for i in range(0, len(b64), chunk_chars)]
    sha = hashlib.sha256(data).hexdigest()

    node._handle(f"CLIPB xfer1 image/jpeg 0 {sha} {len(data)} {len(chunks)}")
    for idx, c in enumerate(chunks):
        node._handle(f"CLIPC xfer1 {idx} {c}")

    check("payload emitted once the last chunk arrives", len(received) == 1)
    if received:
        check("mime correct", received[0].mime == "image/jpeg")
        check("reassembled bytes match the original", received[0].data == data)
        check("sha256 matches", received[0].sha256 == sha)


def test_inbound_sha_mismatch_dropped():
    node, fake = _wired_node()
    received: list[ClipPayload] = []
    node.subscribe(lambda payload: received.append(payload))
    node._handle("CLIPB xfer2 image/png 0 " + "0" * 64 + " 4 1")
    node._handle("CLIPC xfer2 0 QUJDRA==")  # decodes fine, but hash won't match the header
    check("sha256 mismatch drops the payload rather than applying it", len(received) == 0)


def test_inbound_gap_aborts_transfer():
    node, fake = _wired_node()
    received: list[ClipPayload] = []
    node.subscribe(lambda payload: received.append(payload))
    node._handle("CLIPB xfer3 image/png 0 " + "a" * 64 + " 100 3")
    node._handle("CLIPC xfer3 0 QUJD")
    node._handle("CLIPC xfer3 2 REVG")  # gap: expected idx=1
    check("a chunk-index gap aborts the transfer", node._inbound_transfer is None)
    check("no payload emitted for a gapped transfer", len(received) == 0)


def test_inbound_oversize_rejected_before_buffering():
    node, fake = _wired_node()
    node.max_image_bytes = 1000
    node._handle("CLIPB xfer4 image/png 0 " + "b" * 64 + " 999999999 5000")
    check("an oversize header is rejected before any buffer is allocated",
          node._inbound_transfer is None)


def test_new_inbound_transfer_supersedes_old():
    node, fake = _wired_node()
    node._handle("CLIPB old image/png 0 " + "c" * 64 + " 10 5")
    node._handle("CLIPC old 0 QQ==")
    check("old transfer has one buffered chunk", len(node._inbound_transfer.chunks) == 1)
    node._handle("CLIPB new image/png 0 " + "d" * 64 + " 20 3")
    check("a fresh transfer id replaces the old one, discarding it",
          node._inbound_transfer.transfer_id == "new"
          and len(node._inbound_transfer.chunks) == 0)


def main() -> int:
    for name, fn in [("ack-confirms-write", test_ack_confirms_write),
                     ("defer-confirms-write", test_defer_confirms_write_on_locked_device),
                     ("no-answer-fails", test_no_answer_fails_and_marks_transport_dead),
                     ("on-connect-write-no-deadlock",
                      test_write_triggered_from_on_connect_does_not_deadlock),
                     ("chunked-write-large-image", test_chunked_write_large_image),
                     ("periodic-chunk-ack-resets-deadline",
                      test_periodic_chunk_ack_resets_stall_deadline),
                     ("chunked-write-times-out", test_chunked_write_times_out_like_text),
                     ("inbound-chunked-reassembly", test_inbound_chunked_reassembly),
                     ("inbound-sha-mismatch-dropped", test_inbound_sha_mismatch_dropped),
                     ("inbound-gap-aborts", test_inbound_gap_aborts_transfer),
                     ("inbound-oversize-rejected",
                      test_inbound_oversize_rejected_before_buffering),
                     ("new-transfer-supersedes-old", test_new_inbound_transfer_supersedes_old)]:
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
