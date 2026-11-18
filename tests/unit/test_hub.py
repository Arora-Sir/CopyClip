import os
import tempfile
import unittest

from tests.fakes import FakeNode
from core.hub import Hub
from core.models import ClipPayload, WriteResult
from core.outbox import Outbox
from core.policy import Policy
from domains.sync_domain import SyncDomain


def make_hub(tmp, nodes=None, domains=None, **policy):
    nodes = nodes or [FakeNode("laptop"), FakeNode("phone"), FakeNode("pixel")]
    hub = Hub(nodes=nodes, domains=domains or [], policy=Policy(**policy),
              outbox=Outbox(os.path.join(tmp, "outbox.json")))
    return hub, {n.name: n for n in nodes}


class HubBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class RoutingTests(HubBase):
    def test_laptop_clip_reaches_all_devices_not_back_to_itself(self):
        hub, n = make_hub(self.tmp.name)
        hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        self.assertEqual(len(n["phone"].writes), 1)
        self.assertEqual(len(n["pixel"].writes), 1)
        self.assertEqual(n["laptop"].writes, [])
        self.assertEqual(hub.stats["delivered"], 2)

    def test_echo_is_not_redelivered(self):
        hub, n = make_hub(self.tmp.name)
        hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        hub.on_clip(ClipPayload.from_text("hello", "phone"))  # echo from the device
        self.assertEqual(len(n["phone"].writes), 1)
        self.assertEqual(len(n["laptop"].writes), 0)

    def test_phone_clip_goes_to_laptop_and_pixel(self):
        hub, n = make_hub(self.tmp.name)
        hub.on_clip(ClipPayload.from_text("from phone", "phone"))
        self.assertEqual(len(n["laptop"].writes), 1)
        self.assertEqual(len(n["pixel"].writes), 1)
        self.assertEqual(n["phone"].writes, [])

    def test_policy_rejection_counts_skipped(self):
        hub, n = make_hub(self.tmp.name, max_chars=3)
        hub.on_clip(ClipPayload.from_text("too long", "laptop"))
        self.assertEqual(hub.stats["skipped"], 1)
        self.assertEqual(n["phone"].writes, [])

    def test_unavailable_target_is_queued_then_drained(self):
        hub, n = make_hub(self.tmp.name)
        n["pixel"].result = WriteResult.UNAVAILABLE
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertEqual(hub.outbox.keys(), ["node:pixel"])
        n["pixel"].result = WriteResult.OK
        hub._drain_outbox()
        self.assertTrue(hub.outbox.is_empty())

    def test_failed_write_is_queued_for_retry(self):
        hub, n = make_hub(self.tmp.name)
        n["phone"].result = WriteResult.FAILED
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertIn("node:phone", hub.outbox.keys())

    def test_deferred_counts_as_delivered(self):
        hub, n = make_hub(self.tmp.name)
        n["phone"].result = WriteResult.DEFERRED
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertTrue(hub.outbox.is_empty())

    def test_rejected_write_is_not_queued(self):
        hub, n = make_hub(self.tmp.name)
        n["phone"].result = WriteResult.REJECTED
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertNotIn("node:phone", hub.outbox.keys())

    def test_domain_gets_exactly_one_write(self):
        phone, tab = FakeNode("phone"), FakeNode("tab")
        dom = SyncDomain("samsung", [phone, tab])
        hub, _ = make_hub(self.tmp.name, nodes=[FakeNode("laptop"), phone, tab], domains=[dom])
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertEqual(len(phone.writes) + len(tab.writes), 1)

    def test_clip_originating_inside_domain_not_written_back_to_it(self):
        phone, tab, laptop = FakeNode("phone"), FakeNode("tab"), FakeNode("laptop")
        dom = SyncDomain("samsung", [phone, tab])
        hub, _ = make_hub(self.tmp.name, nodes=[laptop, phone, tab], domains=[dom])
        hub.on_clip(ClipPayload.from_text("x", "phone"))
        self.assertEqual(phone.writes + tab.writes, [])
        self.assertEqual(len(laptop.writes), 1)

    def test_camera_photo_only_goes_to_laptop(self):
        hub, n = make_hub(self.tmp.name)
        hub.on_clip(ClipPayload(mime="image/jpeg", data=b"x", origin="phone", is_camera=True))
        self.assertEqual(len(n["laptop"].writes), 1)
        self.assertEqual(n["pixel"].writes, [])


class PauseTests(HubBase):
    def test_global_pause_drops_inbound(self):
        hub, n = make_hub(self.tmp.name)
        hub.paused = True
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertEqual(n["phone"].writes, [])

    def test_pause_one_device_skips_it_only(self):
        hub, n = make_hub(self.tmp.name)
        hub.pause_device("pixel")
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        self.assertEqual(n["pixel"].writes, [])
        self.assertEqual(len(n["phone"].writes), 1)
        hub.resume_device("pixel")
        self.assertFalse(hub.is_device_paused("pixel"))

    def test_paused_source_device_is_ignored(self):
        hub, n = make_hub(self.tmp.name)
        hub.pause_device("phone")
        hub.on_clip(ClipPayload.from_text("x", "phone"))
        self.assertEqual(n["laptop"].writes, [])

    def test_status_shape(self):
        hub, _ = make_hub(self.tmp.name)
        hub.pause_device("pixel")
        s = hub.status()
        self.assertEqual(set(s["nodes"]), {"laptop", "phone", "pixel"})
        self.assertTrue(s["nodes"]["pixel"]["paused"])
        self.assertFalse(s["nodes"]["phone"]["paused"])
        self.assertEqual(s["paused_devices"], ["pixel"])
        self.assertEqual(s["outbox"], [])
        self.assertEqual(set(s["stats"]), {"observed", "delivered", "skipped", "queued"})

    def test_paused_device_keeps_its_outbox_entry(self):
        # Regression: draining used to clear the entry of a paused device and lose the clip.
        hub, n = make_hub(self.tmp.name)
        n["pixel"].result = WriteResult.UNAVAILABLE
        hub.on_clip(ClipPayload.from_text("x", "laptop"))
        hub.pause_device("pixel")
        hub._drain_outbox()
        self.assertEqual(hub.outbox.keys(), ["node:pixel"])


class ReloadConfigTests(HubBase):
    def test_applies_policy_keys(self):
        hub, _ = make_hub(self.tmp.name)
        hub.reload_config({"allow_images": True, "max_chars": 10, "max_image_bytes": 99,
                           "allow_sensitive": True, "allow_recopy": False,
                           "direct_password_sync": True})
        self.assertTrue(hub.policy.allow_images)
        self.assertEqual(hub.policy.max_chars, 10)
        self.assertEqual(hub.policy.max_image_bytes, 99)
        self.assertTrue(hub.policy.allow_sensitive)
        self.assertFalse(hub.allow_recopy)
        self.assertTrue(hub.direct_password_sync)

    def test_disabling_sensitive_drops_queued_sensitive_clips(self):
        hub, n = make_hub(self.tmp.name, allow_sensitive=True)
        n["pixel"].result = WriteResult.UNAVAILABLE
        hub.on_clip(ClipPayload.from_text("123456", "laptop", sensitive=True))
        self.assertFalse(hub.outbox.is_empty())
        hub.reload_config({"allow_sensitive": False})
        self.assertTrue(hub.outbox.is_empty())

    def test_empty_reload_keeps_startup_defaults(self):
        # Regression: reload used to default allow_images=True and a 24 MB cap.
        hub, _ = make_hub(self.tmp.name)
        before = (hub.policy.allow_images, hub.policy.max_image_bytes)
        hub.reload_config({})
        self.assertEqual((hub.policy.allow_images, hub.policy.max_image_bytes), before)


if __name__ == "__main__":
    unittest.main()
