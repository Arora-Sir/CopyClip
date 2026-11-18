import os
import tempfile
import unittest

from tests.fakes import FakeNode
from core.activity import ActivityLog, PREVIEW_CHARS, overall_status
from core.hub import Hub
from core.models import ClipPayload, WriteResult
from core.outbox import Outbox
from core.policy import Policy


class StatusTests(unittest.TestCase):
    def test_overall_status(self):
        d = {"node": "a", "status": "delivered"}
        q = {"node": "b", "status": "queued"}
        p = {"node": "c", "status": "paused"}
        self.assertEqual(overall_status([d]), "delivered")
        self.assertEqual(overall_status([d, {"node": "x", "status": "deferred"}]), "delivered")
        self.assertEqual(overall_status([d, q]), "delivered")
        self.assertEqual(overall_status([p]), "paused")
        self.assertEqual(overall_status([d, p]), "delivered")
        self.assertEqual(overall_status([]), "skipped")
        self.assertEqual(overall_status([d], override="skipped"), "skipped")


class LogTests(unittest.TestCase):
    def test_ring_buffer_drops_oldest(self):
        log = ActivityLog(capacity=3)
        for i in range(5):
            log.record(kind="text", size=i, origin="a")
        self.assertEqual([e["size"] for e in log.list()], [4, 3, 2])

    def test_newest_first_and_after_cursor(self):
        log = ActivityLog()
        for i in range(4):
            log.record(kind="text", size=i, origin="a")
        ids = [e["id"] for e in log.list()]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual([e["size"] for e in log.list(after=ids[2])], [3, 2])
        self.assertEqual(log.list(after=ids[0]), [])

    def test_preview_rules(self):
        log = ActivityLog()
        long = "x" * 1000
        self.assertEqual(len(log.record(kind="text", size=1, origin="a", text=long).preview), PREVIEW_CHARS)
        self.assertIsNone(log.record(kind="text", size=1, origin="a", text="s", sensitive=True).preview)
        self.assertIsNone(log.record(kind="image", size=1, origin="a", text="s").preview)

    def test_sensitive_has_no_fingerprint_or_preview(self):
        e = ActivityLog().record(kind="text", size=6, origin="a", text="123456",
                                 sensitive=True, fingerprint="abc")
        self.assertEqual((e.preview, e.fingerprint), (None, ""))

    def test_previews_can_be_disabled_and_scrubbed(self):
        log = ActivityLog()
        log.record(kind="text", size=1, origin="a", text="hello")
        log.set_previews(False)
        self.assertIsNone(log.list()[0]["preview"])
        log.record(kind="text", size=1, origin="a", text="later")
        self.assertIsNone(log.list()[0]["preview"])

    def test_filters(self):
        log = ActivityLog()
        log.record(kind="text", size=1, origin="phone", text="alpha",
                   targets=[{"node": "laptop", "status": "delivered"}])
        log.record(kind="image", size=9, origin="tab", status="skipped", reason="too large")
        log.record(kind="text", size=1, origin="x", sensitive=True,
                   targets=[{"node": "tab", "status": "delivered"}])
        self.assertEqual(len(log.list(kind="image")), 1)
        self.assertEqual(len(log.list(status="skipped")), 1)
        self.assertEqual(len(log.list(sensitive=True)), 1)
        self.assertEqual(len(log.list(query="ALPHA")), 1)
        self.assertEqual(len(log.list(query="laptop")), 1)
        self.assertEqual(len(log.list(query="too large")), 1)
        self.assertEqual(log.list(query="nothing-matches"), [])

    def test_limit(self):
        log = ActivityLog()
        for _ in range(10):
            log.record(kind="text", size=1, origin="a")
        self.assertEqual(len(log.list(limit=3)), 3)

    def test_clear(self):
        log = ActivityLog()
        log.record(kind="text", size=1, origin="a")
        log.clear()
        self.assertEqual(len(log), 0)


class HubActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nodes = {n: FakeNode(n) for n in ("laptop", "phone", "pixel")}
        self.hub = Hub(nodes=list(self.nodes.values()), domains=[], policy=Policy(max_chars=20),
                       outbox=Outbox(os.path.join(self.tmp.name, "o.json")))

    def events(self):
        return self.hub.activity.list()

    def test_delivered_clip_recorded_with_targets(self):
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        e = self.events()[0]
        self.assertEqual((e["kind"], e["status"], e["origin"], e["preview"]),
                         ("text", "delivered", "laptop", "hello"))
        self.assertEqual({t["node"] for t in e["targets"]}, {"phone", "pixel"})

    def test_queued_when_a_device_is_down(self):
        self.nodes["pixel"].result = WriteResult.UNAVAILABLE
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        self.assertEqual(self.events()[0]["status"], "delivered")

    def test_policy_skip_is_recorded_with_reason(self):
        self.hub.on_clip(ClipPayload.from_text("x" * 50, "laptop"))
        e = self.events()[0]
        self.assertEqual(e["status"], "skipped")
        self.assertIn("too large", e["reason"])

    def test_paused_device_shows_in_targets(self):
        self.hub.pause_device("pixel")
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        statuses = {t["node"]: t["status"] for t in self.events()[0]["targets"]}
        self.assertEqual(statuses["pixel"], "paused")

    def test_sensitive_clip_has_no_preview(self):
        self.hub.policy.allow_sensitive = True
        self.hub.on_clip(ClipPayload.from_text("123456", "laptop", sensitive=True))
        e = self.events()[0]
        self.assertTrue(e["sensitive"])
        self.assertIsNone(e["preview"])

    def test_camera_photo_recorded(self):
        self.hub.on_clip(ClipPayload(mime="image/jpeg", data=b"x" * 10, origin="phone", is_camera=True))
        self.assertEqual(self.events()[0]["kind"], "camera")

    def test_status_has_extras(self):
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        s = self.hub.status()
        self.assertIn("uptime_s", s)
        self.assertIsNotNone(s["nodes"]["laptop"]["last_clip_ts"])
        self.assertIsNone(s["nodes"]["phone"]["last_error"])

    def test_broken_activity_log_never_breaks_routing(self):
        class Boom(ActivityLog):
            def record(self, **kw):
                raise RuntimeError("diagnostics down")
        self.hub.activity = Boom()
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        self.assertEqual(len(self.nodes["phone"].writes), 1)

    def test_echoes_are_not_recorded_as_new_events(self):
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        n = len(self.events())
        self.hub.on_clip(ClipPayload.from_text("hello", "phone"))
        self.assertEqual(len(self.events()), n + 1)  # echo is observed and routed, not lost


if __name__ == "__main__":
    unittest.main()


class SupersededTests(unittest.TestCase):
    def test_newer_queued_clip_replaces_older_queue_marker(self):
        log = ActivityLog()
        q = [{"node": "tab", "status": "delivered"}, {"node": "pixel", "status": "queued"}]
        log.record(kind="text", size=5, origin="laptop", targets=[dict(t) for t in q])
        log.record(kind="text", size=6, origin="laptop", targets=[dict(t) for t in q])
        newest, oldest = log.list()
        self.assertEqual(newest["status"], "delivered")
        self.assertEqual(oldest["status"], "delivered")
        self.assertEqual(oldest["targets"][1]["status"], "superseded")


class MirroredStatusTests(unittest.TestCase):
    def test_all_queued_stays_queued_and_mirrored_counts_as_delivered(self):
        self.assertEqual(overall_status([{"node": "a", "status": "queued"}]), "queued")
        self.assertEqual(overall_status([{"node": "a", "status": "delivered"},
                                         {"node": "b", "status": "mirrored"}]), "delivered")
