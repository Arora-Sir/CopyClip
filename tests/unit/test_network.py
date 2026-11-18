"""Loopback tests for the push listener and the TCP transport. 127.0.0.1 only."""

import base64
import hashlib
import socket
import threading
import unittest

from tests import fakes  # noqa: F401
from core.models import ClipPayload
from transports import TransportError, framing
from transports.push_listener import PushListener
from transports.tcp_transport import TcpTransport

KEY = b"k" * 32


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeHub:
    class policy:
        max_image_bytes = 1000

    def __init__(self):
        self.clips = []

    def on_clip(self, payload):
        self.clips.append(payload)


class Client:
    """Speaks the push protocol far enough to reach the frame stage."""

    def __init__(self, port, key=KEY, answer_challenge=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.sock.makefile("rwb", buffering=0)
        hello = self.f.readline().decode().strip()
        assert hello.startswith("HELLO "), hello
        nonce = hello.split(" ", 1)[1]
        self.f.write(f"AUTH {framing.challenge_response(key, nonce)}\n".encode())
        self.auth_reply = self.f.readline().decode().strip()
        if self.auth_reply != "AUTH-OK":
            return
        challenge = self.f.readline().decode().strip().split(" ", 1)[1]
        resp = framing.challenge_response(key, challenge) if answer_challenge else "0" * 64
        self.f.write(f"RESPONSE {resp}\n".encode())

    def send_frame(self, payload, key=KEY, seq=1):
        self.f.write((framing.seal(key, seq, payload) + "\n").encode())

    def read_reply(self):
        try:
            return self.f.readline().decode().strip()
        except ConnectionError:
            return ""  # server dropped us; Windows reports that as a reset

    def close(self):
        self.f.close()
        self.sock.close()


class PushListenerTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.port = free_port()
        self.listener = PushListener(self.port, KEY, self.hub)
        self.listener.start()
        self.addCleanup(self.listener.stop)

    def test_text_push_delivered(self):
        c = Client(self.port)
        b64 = base64.b64encode("hello".encode()).decode()
        c.send_frame(f"PUSH pixel text/plain 0 {b64}")
        self.assertEqual(c.read_reply(), "PUSH-OK")
        c.close()
        self.assertEqual((self.hub.clips[0].text, self.hub.clips[0].origin), ("hello", "pixel"))

    def test_wrong_key_is_rejected(self):
        c = Client(self.port, key=b"z" * 32)
        self.assertEqual(c.auth_reply, "AUTH-FAIL")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_failed_challenge_delivers_nothing(self):
        c = Client(self.port, answer_challenge=False)
        c.send_frame("PUSH pixel text/plain 0 aGk=")
        self.assertEqual(c.read_reply(), "")  # connection dropped, no PUSH-OK
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_frame_signed_with_other_key_dropped(self):
        c = Client(self.port)
        c.send_frame("PUSH pixel text/plain 0 aGk=", key=b"z" * 32)
        self.assertEqual(c.read_reply(), "")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_replayed_sequence_zero_dropped(self):
        c = Client(self.port)
        c.send_frame("PUSH pixel text/plain 0 aGk=", seq=0)
        self.assertEqual(c.read_reply(), "")
        c.close()

    def test_malformed_and_undecodable_push(self):
        c = Client(self.port)
        c.send_frame("PUSH pixel")
        self.assertEqual(c.read_reply(), "PUSH-FAIL malformed")
        c.close()
        c = Client(self.port)
        c.send_frame("PUSH pixel text/plain 0 abcde")  # bad padding
        self.assertEqual(c.read_reply(), "PUSH-FAIL undecodable")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_junk_base64_is_accepted_as_empty_clip_today(self):
        # Characterisation: b64decode silently drops non-alphabet characters, so this
        # reaches the hub as a zero-byte clip (policy then rejects it as "empty").
        c = Client(self.port)
        c.send_frame("PUSH pixel text/plain 0 !!!@@@")
        self.assertEqual(c.read_reply(), "PUSH-OK")
        c.close()
        self.assertEqual(self.hub.clips[0].size, 0)

    def test_empty_frame_closes_quietly(self):
        c = Client(self.port)
        c.f.write(b"\n")
        self.assertEqual(c.read_reply(), "")
        c.close()

    def _stream(self, data, sha=None, header_bytes=None, send=None):
        c = Client(self.port)
        sha = sha or hashlib.sha256(data).hexdigest()
        n = len(data) if header_bytes is None else header_bytes
        c.send_frame(f"STREAM tab image/png 0 {sha} {n}")
        c.f.write(data if send is None else send)
        return c

    def test_stream_ok(self):
        c = self._stream(b"x" * 500)
        self.assertEqual(c.read_reply(), "STREAM-OK")
        c.close()
        self.assertEqual(self.hub.clips[0].data, b"x" * 500)
        self.assertFalse(self.hub.clips[0].is_camera)

    def test_stream_camera_flag(self):
        c = Client(self.port)
        data = b"y" * 10
        c.send_frame(f"STREAM tab image/jpeg 0 1 {hashlib.sha256(data).hexdigest()} 10")
        c.f.write(data)
        self.assertEqual(c.read_reply(), "STREAM-OK")
        c.close()
        self.assertTrue(self.hub.clips[0].is_camera)

    def test_stream_oversize_rejected_before_reading(self):
        c = self._stream(b"x" * 10, header_bytes=5000, send=b"")
        self.assertEqual(c.read_reply(), "STREAM-FAIL oversize")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_stream_sha_mismatch(self):
        c = self._stream(b"x" * 20, sha="0" * 64)
        self.assertEqual(c.read_reply(), "STREAM-FAIL sha_mismatch")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_stream_truncated(self):
        c = self._stream(b"x" * 20, header_bytes=100)
        c.sock.shutdown(socket.SHUT_WR)
        self.assertEqual(c.read_reply(), "STREAM-FAIL truncated")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_stream_bad_length_and_short_header(self):
        c = Client(self.port)
        c.send_frame("STREAM tab image/png 0 " + "a" * 64 + " notanumber")
        self.assertEqual(c.read_reply(), "STREAM-FAIL bad_length")
        c.close()
        c = Client(self.port)
        c.send_frame("STREAM tab image/png")
        self.assertEqual(c.read_reply(), "STREAM-FAIL malformed")
        c.close()

    def test_reserved_node_name_is_rejected(self):
        for verb, tail in (("PUSH", "text/plain 0 aGk="), ):
            c = Client(self.port)
            c.send_frame(f"{verb} laptop {tail}")
            self.assertEqual(c.read_reply(), "PUSH-FAIL reserved_name")
            c.close()
        c = Client(self.port)
        c.send_frame("STREAM laptop image/png 0 " + "a" * 64 + " 5")
        self.assertEqual(c.read_reply(), "STREAM-FAIL reserved_name")
        c.close()
        self.assertEqual(self.hub.clips, [])

    def test_overlong_handshake_line_is_dropped(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        f = s.makefile("rwb", buffering=0)
        f.readline()  # HELLO
        f.write(b"AUTH " + b"A" * 5000 + b"\n")
        try:
            reply = f.readline().decode().strip()
        except ConnectionError:
            reply = ""
        s.close()
        self.assertIn(reply, ("", "AUTH-FAIL"))
        self.assertEqual(self.hub.clips, [])

    def test_listener_survives_garbage_connections(self):
        for junk in (b"", b"\x00\xff\xfe", b"A" * 5000 + b"\n"):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(junk)
            s.close()
        c = Client(self.port)
        c.send_frame("PUSH pixel text/plain 0 aGk=")
        self.assertEqual(c.read_reply(), "PUSH-OK")
        c.close()


class FakeBridgeServer:
    """One-shot server speaking the TcpTransport handshake with controllable faults."""

    def __init__(self, key=KEY, auth_ok=True, answer_challenge=True, greeting="READY 36",
                 silent=False, hello="HELLO "):
        self.key, self.auth_ok, self.answer_challenge = key, auth_ok, answer_challenge
        self.greeting, self.silent, self.hello = greeting, silent, hello
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn = conn
        if self.silent:
            return
        f = conn.makefile("rwb", buffering=0)
        nonce = framing.new_nonce()
        f.write(f"{self.hello}{nonce}\n".encode())
        auth = f.readline().decode().strip()
        if auth != f"AUTH {framing.challenge_response(self.key, nonce)}" or not self.auth_ok:
            f.write(b"AUTH-FAIL\n")
            return
        f.write(b"AUTH-OK\n")
        challenge = f.readline().decode().strip().split(" ", 1)[1]
        resp = framing.challenge_response(self.key, challenge) if self.answer_challenge else "0" * 64
        f.write(f"RESPONSE {resp}\n".encode())
        if self.greeting:
            f.write((framing.seal(self.key, 1, self.greeting) + "\n").encode())
        self.f = f

    def close(self):
        for s in (self.conn, self.srv):
            try:
                s.close()
            except Exception:
                pass


class TcpTransportTests(unittest.TestCase):
    def serve(self, **kw):
        srv = FakeBridgeServer(**kw)
        self.addCleanup(srv.close)
        return srv

    def test_handshake_and_ready(self):
        srv = self.serve()
        t = TcpTransport("127.0.0.1", srv.port, KEY, timeout=3)
        self.addCleanup(t.close)
        t.start()
        self.assertTrue(t.alive())

    def test_unreachable_host(self):
        t = TcpTransport("127.0.0.1", free_port(), KEY, timeout=1)
        with self.assertRaises(TransportError):
            t.start()

    def test_wrong_key_rejected(self):
        srv = self.serve()
        t = TcpTransport("127.0.0.1", srv.port, b"z" * 32, timeout=3)
        with self.assertRaises(TransportError):
            t.start()
        self.assertFalse(t.alive())

    def test_impostor_server_rejected(self):
        srv = self.serve(answer_challenge=False)
        t = TcpTransport("127.0.0.1", srv.port, KEY, timeout=3)
        with self.assertRaises(TransportError):
            t.start()

    def test_bad_greeting_line(self):
        srv = self.serve(hello="NOPE ")
        t = TcpTransport("127.0.0.1", srv.port, KEY, timeout=3)
        with self.assertRaises(TransportError):
            t.start()

    def test_unexpected_ready_line(self):
        srv = self.serve(greeting="WHAT 1")
        t = TcpTransport("127.0.0.1", srv.port, KEY, timeout=3)
        with self.assertRaises(TransportError):
            t.start()

    def test_send_line_before_start_raises(self):
        with self.assertRaises(TransportError):
            TcpTransport("127.0.0.1", 1, KEY).send_line("PING")

    def test_close_is_idempotent(self):
        t = TcpTransport("127.0.0.1", 1, KEY)
        t.close()
        t.close()
        self.assertFalse(t.alive())

    def test_silent_peer_does_not_hang_start(self):
        # Regression: the socket timeout used to be cleared before the handshake, so a peer
        # that accepts and never speaks blocked start() forever.
        srv = self.serve(silent=True)
        t = TcpTransport("127.0.0.1", srv.port, KEY, timeout=0.5)
        done = threading.Event()

        def go():
            try:
                t.start()
            except Exception:
                pass
            done.set()
        threading.Thread(target=go, daemon=True).start()
        finished = done.wait(3)
        srv.close()  # unblock the stuck reader so the test process can exit cleanly
        self.assertTrue(finished)


if __name__ == "__main__":
    unittest.main()
