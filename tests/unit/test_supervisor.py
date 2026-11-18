"""AndroidNode connect/reconnect supervision, driven by a scripted in-memory transport."""

import threading
import time
import unittest

from tests import fakes  # noqa: F401
from nodes.android_node import AndroidNode
from transports import Transport, TransportError


class ScriptedTransport(Transport):
    def __init__(self, fail_with=None):
        super().__init__()
        self.fail_with = fail_with
        self.sent = []
        self.closed = False

    def start(self):
        if self.fail_with is not None:
            raise self.fail_with

    def send_line(self, line):
        self.sent.append(line)

    def close(self):
        self.closed = True
        self._mark_dead()


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class SupervisorTests(unittest.TestCase):
    def node(self, factory, **kw):
        n = AndroidNode("phone", factory, reconnect_min_s=0.01, reconnect_max_s=0.05,
                        idle_ping_s=60, **kw)
        self.addCleanup(n.stop)
        return n

    def test_connects_and_sends_conf_first(self):
        made = []

        def factory():
            made.append(ScriptedTransport())
            return made[-1]
        n = self.node(factory)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))
        self.assertIsNone(n.last_error)
        self.assertTrue(wait_for(lambda: made[0].sent))
        self.assertTrue(made[0].sent[0].startswith("CONF "))

    def test_retries_after_transport_errors_then_connects(self):
        attempts = []

        def factory():
            attempts.append(1)
            return ScriptedTransport(TransportError("offline") if len(attempts) < 4 else None)
        n = self.node(factory)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))
        self.assertEqual(len(attempts), 4)
        self.assertIsNone(n.last_error)  # cleared on success

    def test_last_error_visible_while_offline(self):
        n = self.node(lambda: ScriptedTransport(TransportError("device offline")))
        n.start()
        self.assertTrue(wait_for(lambda: n.last_error == "device offline"))
        self.assertFalse(n.available)

    def test_unexpected_exception_does_not_kill_supervisor(self):
        attempts = []

        def factory():
            attempts.append(1)
            return ScriptedTransport(RuntimeError("boom") if len(attempts) < 3 else None)
        n = self.node(factory)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))

    def test_channel_loss_reconnects_with_fresh_transport_and_reruns_on_connect(self):
        made, connects = [], []

        def factory():
            made.append(ScriptedTransport())
            return made[-1]
        n = self.node(factory, on_connect=lambda node: connects.append(1))
        n.start()
        self.assertTrue(wait_for(lambda: n.available and made))
        made[0].mark_dead()
        self.assertTrue(wait_for(lambda: len(made) >= 2 and n.available))
        self.assertTrue(made[0].closed)
        self.assertTrue(wait_for(lambda: len(connects) >= 2))

    def test_stop_ends_supervision_and_closes_transport(self):
        made = []

        def factory():
            made.append(ScriptedTransport())
            return made[-1]
        n = self.node(factory)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))
        n.stop()
        self.assertFalse(n.available)
        self.assertTrue(wait_for(lambda: made[0].closed))
        count = len(made)
        time.sleep(0.2)
        self.assertEqual(len(made), count)  # no reconnect after stop

    def test_stop_while_offline_returns_promptly(self):
        n = self.node(lambda: ScriptedTransport(TransportError("x")))
        n.start()
        time.sleep(0.05)
        t0 = time.time()
        n.stop()
        self.assertTrue(wait_for(lambda: not n._thread.is_alive(), 2))
        self.assertLess(time.time() - t0, 2)

    def test_failing_on_connect_handler_does_not_break_connection(self):
        def bad(node):
            raise RuntimeError("handler bug")
        n = self.node(ScriptedTransport, on_connect=bad)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))
        time.sleep(0.1)
        self.assertTrue(n.available)

    def test_set_mode_not_recorded_while_disconnected(self):
        n = self.node(lambda: ScriptedTransport(TransportError("x")))
        n.set_mode("stamp")
        self.assertEqual(n._mode, "full")

    def test_set_mode_rejects_unknown_mode(self):
        n = self.node(ScriptedTransport)
        n.start()
        self.assertTrue(wait_for(lambda: n.available))
        with self.assertRaises(ValueError):
            n.set_mode("bogus")

    def test_concurrent_start_stop_is_safe(self):
        n = self.node(ScriptedTransport)
        threads = [threading.Thread(target=n.stop) for _ in range(5)]
        n.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
        self.assertFalse(n.available)


if __name__ == "__main__":
    unittest.main()
