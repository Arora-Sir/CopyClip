"""Characterisation of the ClipBridge wire protocol as AndroidNode speaks it today.

These pin the exact bytes on the wire so later refactors (plan Phase 3) cannot change
framing by accident. Pure in-memory: a FakeTransport is injected directly.
"""

import base64
import threading
import unittest

from tests.fakes import FakeTransport
from core.models import ClipPayload, WriteResult
from nodes.android_node import AndroidNode


def make_node(**kw):
    node = AndroidNode("phone", transport_factory=FakeTransport, **kw)
    node._transport = FakeTransport()
    node._connected = True
    return node


def write_with_reply(node, payload, reply):
    """Run write() on a thread, then feed `reply` back as the device's answer."""
    box = []
    t = threading.Thread(target=lambda: box.append(node.write(payload)))
    t.start()
    # Wait until the SET line is on the wire, then answer it.
    for _ in range(200):
        if node._transport.sent:
            break
        threading.Event().wait(0.01)
    node._handle(reply)
    t.join(5)
    return box[0]


class OutboundTextTests(unittest.TestCase):
    def test_set_frame_is_verb_space_base64(self):
        node = make_node()
        p = ClipPayload.from_text("hello é", "laptop")
        self.assertEqual(write_with_reply(node, p, "ACK set"), WriteResult.OK)
        self.assertEqual(node._transport.sent,
                         ["SET " + base64.b64encode("hello é".encode()).decode()])

    def test_sensitive_uses_sets_verb(self):
        node = make_node()
        p = ClipPayload.from_text("123456", "laptop", sensitive=True)
        write_with_reply(node, p, "ACK set")
        self.assertTrue(node._transport.sent[0].startswith("SETS "))

    def test_defer_when_locked(self):
        node = make_node()
        node._locked = True
        p = ClipPayload.from_text("x", "laptop")
        self.assertEqual(write_with_reply(node, p, "DEFER set locked"), WriteResult.DEFERRED)

    def test_disconnected_is_unavailable(self):
        node = make_node()
        node._connected = False
        self.assertEqual(node.write(ClipPayload.from_text("x", "laptop")),
                         WriteResult.UNAVAILABLE)

    def test_no_ack_fails_and_marks_channel_dead(self):
        node = make_node(ack_timeout_s=0.05)
        self.assertEqual(node.write(ClipPayload.from_text("x", "laptop")), WriteResult.FAILED)
        self.assertFalse(node._transport.alive())

    def test_send_error_fails(self):
        from transports import TransportError
        node = make_node()

        def boom(line):
            raise TransportError("pipe closed")
        node._transport.send_line = boom
        self.assertEqual(node.write(ClipPayload.from_text("x", "laptop")), WriteResult.FAILED)


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.node = make_node()
        self.clips, self.descs = [], []
        self.node.subscribe(self.clips.append, self.descs.append)

    def test_clip_frame(self):
        b64 = base64.b64encode(b"hi").decode()
        self.node._handle(f"CLIP text/plain 0 {b64}")
        self.assertEqual((self.clips[0].text, self.clips[0].origin, self.clips[0].sensitive),
                         ("hi", "phone", False))

    def test_clip_sensitive_flag(self):
        b64 = base64.b64encode(b"123456").decode()
        self.node._handle(f"CLIP text/plain 1 {b64}")
        self.assertTrue(self.clips[0].sensitive)

    def test_desc_frame(self):
        self.node._handle("DESC text/plain 0 1700000000 " + "a" * 64 + " 42")
        d = self.descs[0]
        self.assertEqual((d.node, d.mime, d.ts, d.char_len, d.sensitive),
                         ("phone", "text/plain", 1700000000, 42, False))

    def test_desc_without_charlen_defaults_zero(self):
        self.node._handle("DESC text/plain 0 5 " + "b" * 64)
        self.assertEqual(self.descs[0].char_len, 0)

    def test_malformed_frames_are_dropped_not_raised(self):
        for line in ("CLIP", "CLIP text/plain 0", "CLIP text/plain 0 !!!notbase64@@@",
                     "DESC text/plain", "DESC text/plain 0 notanint " + "c" * 64,
                     "CLIPB x", "CLIPB x image/png 0 sha NaN 1", "BOGUS verb", ""):
            with self.subTest(line=line):
                self.node._handle(line)  # must not raise
        self.assertEqual(self.descs, [])

    def test_non_ascii_unicode_clip(self):
        raw = "مرحبا \U0001F600"
        b64 = base64.b64encode(raw.encode()).decode()
        self.node._handle(f"CLIP text/plain 0 {b64}")
        self.assertEqual(self.clips[0].text, raw)

    def test_chunk_header_with_zero_chunks_does_not_crash(self):
        # Characterisation: nchunks=0 is accepted today (plan Phase 1 tightens validation).
        self.node._handle("CLIPB x image/png 0 " + "0" * 64 + " 0 0")


if __name__ == "__main__":
    unittest.main()
