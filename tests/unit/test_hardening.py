"""Regression tests for the Phase 1 hardening fixes."""

import base64
import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from core import single_instance
from core.models import ClipPayload
from nodes.android_node import AndroidNode
from tests.fakes import FakeTransport
from transports import TransportError
from transports.adb_transport import AdbTransport


class AdbTimeoutTests(unittest.TestCase):
    def transport(self):
        return AdbTransport("10.0.0.1:5555", "missing.dex", adb="adb", connect_timeout=1)

    def test_connect_timeout_becomes_transport_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 1)):
            with self.assertRaises(TransportError):
                self.transport()._ensure_connected()

    def test_missing_adb_binary_becomes_transport_error(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("adb")):
            with self.assertRaises(TransportError):
                self.transport()._ensure_connected()

    def test_get_state_timeout_becomes_transport_error(self):
        ok = subprocess.CompletedProcess([], 0, stdout="connected to x", stderr="")
        calls = [ok, subprocess.TimeoutExpired("adb", 1)]

        def fake_run(*a, **k):
            r = calls.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(TransportError):
                self.transport()._ensure_connected()

    def test_device_not_ready_reported(self):
        ok = subprocess.CompletedProcess([], 0, stdout="connected to x", stderr="")
        offline = subprocess.CompletedProcess([], 1, stdout="", stderr="error: device offline")
        with mock.patch("subprocess.run", side_effect=[ok, offline]):
            with self.assertRaises(TransportError) as cm:
                self.transport()._ensure_connected()
        self.assertIn("offline", str(cm.exception))

    def test_popen_failure_becomes_transport_error(self):
        t = self.transport()
        with mock.patch.object(t, "_ensure_connected"), mock.patch.object(t, "_push_dex"), \
                mock.patch.object(t, "_adb"), \
                mock.patch("subprocess.Popen", side_effect=OSError("no adb")):
            with self.assertRaises(TransportError):
                t.start()


def make_node():
    n = AndroidNode("phone", transport_factory=FakeTransport, max_image_bytes=1000)
    n._transport = FakeTransport()
    n._connected = True
    got = []
    n.subscribe(got.append)
    return n, got


class InboundChunkTests(unittest.TestCase):
    def test_zero_or_negative_counts_rejected(self):
        for size, chunks in ((0, 1), (10, 0), (10, -1), (-5, 1)):
            with self.subTest(size=size, chunks=chunks):
                n, _ = make_node()
                n._handle(f"CLIPB x image/png 0 {'0' * 64} {size} {chunks}")
                self.assertIsNone(n._inbound_transfer)

    def test_more_data_than_declared_aborts(self):
        n, got = make_node()
        n._handle(f"CLIPB x image/png 0 {'0' * 64} 6 100")
        n._handle("CLIPC x 0 " + "A" * 5000)
        self.assertIsNone(n._inbound_transfer)
        self.assertEqual(got, [])

    def test_valid_transfer_still_completes(self):
        data = b"\x89PNG" + bytes(20)
        sha = hashlib.sha256(data).hexdigest()
        n, got = make_node()
        n._handle(f"CLIPB x image/png 0 {sha} {len(data)} 1")
        n._handle("CLIPC x 0 " + base64.b64encode(data).decode())
        self.assertEqual(got[0].data, data)


class ChunkAckResetTests(unittest.TestCase):
    def test_leftover_chunk_ack_does_not_satisfy_next_transfer(self):
        n, _ = make_node()
        n.final_ack_floor_s = 0.05
        n.chunk_ack_floor_s = 0.05
        n.ack_timeout_s = 0.05
        n.image_chunk_bytes = 3
        n._chunk_ack_event.set()  # stale ACK from an earlier transfer
        # 40 bytes in 3-byte chunks: the periodic wait at chunk 8 must not see the stale ACK.
        p = ClipPayload(mime="image/png", data=bytes(40), origin="laptop")
        self.assertEqual(n.write(p), "failed")


class HashCacheTests(unittest.TestCase):
    def test_sha256_computed_once(self):
        p = ClipPayload.from_text("x" * 100, "a")
        with mock.patch("hashlib.sha256", wraps=hashlib.sha256) as m:
            first, second, third = p.sha256, p.sha256, p.short
        self.assertEqual(m.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(third, first[:12])

    def test_dict_roundtrip_unaffected(self):
        p = ClipPayload.from_text("x", "a")
        _ = p.sha256
        self.assertEqual(ClipPayload.from_dict(p.to_dict()).sha256, p.sha256)


class SingleInstanceTests(unittest.TestCase):
    def test_own_process_is_recognised(self):
        self.assertTrue(single_instance.looks_like_copyclip(os.getpid()))

    def test_unrelated_and_missing_pids_are_not(self):
        self.assertFalse(single_instance.looks_like_copyclip(0xFFFFFFF0))
        self.assertFalse(single_instance.looks_like_copyclip(4))  # System

    def test_replace_refuses_to_kill_non_python_pid(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "x.pid")
            name = "Local\\CopyClipTestMutex" + str(os.getpid())
            holder = single_instance.SingleInstance(pidfile, mutex_name=name)
            holder.acquire()
            try:
                with open(pidfile, "w") as f:
                    f.write("4")
                other = single_instance.SingleInstance(pidfile, mutex_name=name)
                with mock.patch("os.kill") as kill:
                    with self.assertRaises(single_instance.AlreadyRunning):
                        other.acquire(replace=True)
                kill.assert_not_called()
            finally:
                holder.release()


if __name__ == "__main__":
    unittest.main()
