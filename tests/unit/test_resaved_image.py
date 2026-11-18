import os
import struct
import tempfile
import unittest

from core.hub import Hub
from core.imagemeta import dimensions
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy
from tests.fakes import FakeNode


def png(w, h, pad=0):
    ihdr = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0\0\0\0" + b"x" * pad


def jpeg(w, h, pad=0):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9 + sof + b"\xff\xda" + b"x" * pad


class DimensionTests(unittest.TestCase):
    def test_png_and_jpeg(self):
        self.assertEqual(dimensions(png(2960, 1848)), (2960, 1848))
        self.assertEqual(dimensions(jpeg(2960, 1848)), (2960, 1848))

    def test_garbage_is_none(self):
        for blob in (b"", b"abc", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0\x00"):
            self.assertIsNone(dimensions(blob))


class ResavedImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.nodes = {n: FakeNode(n) for n in ("laptop", "tab")}
        self.hub = Hub(nodes=list(self.nodes.values()), domains=[],
                       policy=Policy(max_chars=1000, allow_images=True, max_image_bytes=10_000_000),
                       outbox=Outbox(os.path.join(self.tmp.name, "o.json")))

    def img(self, blob, origin="tab"):
        return ClipPayload(mime="image/jpeg", data=blob, origin=origin)

    def test_reencoded_twin_within_seconds_is_skipped(self):
        self.hub.on_clip(self.img(jpeg(2960, 1848, 705_000)))
        self.hub.on_clip(self.img(jpeg(2960, 1848, 705_500)))
        events = self.hub.activity.list()
        self.assertEqual([e["status"] for e in events], ["skipped", "delivered"])
        self.assertEqual(len(self.nodes["laptop"].writes), 1)

    def test_different_size_or_dimensions_both_delivered(self):
        self.hub.on_clip(self.img(jpeg(2960, 1848, 705_000)))
        self.hub.on_clip(self.img(jpeg(2960, 1848, 300_000)))
        self.hub.on_clip(self.img(jpeg(1000, 800, 300_100)))
        self.assertEqual([e["status"] for e in self.hub.activity.list()], ["delivered"] * 3)


class ExifTests(unittest.TestCase):
    def test_has_exif(self):
        from core.imagemeta import has_exif
        with_exif = b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00MM" + b"\0" * 20
        self.assertTrue(has_exif(with_exif))
        self.assertFalse(has_exif(jpeg(10, 10)))

    def test_original_with_exif_replaces_stripped_copy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []
        laptop = FakeNode("laptop")
        laptop.replace_last_screenshot = calls.append
        hub = Hub(nodes=[laptop, FakeNode("tab")], domains=[],
                  policy=Policy(max_chars=1000, allow_images=True, max_image_bytes=10_000_000),
                  outbox=Outbox(os.path.join(tmp.name, "o.json")))
        plain = jpeg(2960, 1848, 705_000)
        original = plain[:2] + b"\xff\xe1\x00\x0aExif\x00\x00MM\x00\x00" + plain[2:] + b"y" * 400
        hub.on_clip(ClipPayload(mime="image/jpeg", data=plain, origin="tab"))
        hub.on_clip(ClipPayload(mime="image/jpeg", data=original, origin="tab"))
        self.assertEqual(len(calls), 1)


class PreviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.laptop = FakeNode("laptop")
        self.hub = Hub(nodes=[self.laptop, FakeNode("tab")], domains=[],
                       policy=Policy(max_chars=1000, allow_images=True, max_image_bytes=10_000_000),
                       outbox=Outbox(os.path.join(self.tmp.name, "o.json")))
        self.hub.preview_hold_s = 0.05

    def send(self, blob, origin="phone"):
        self.hub.on_clip(ClipPayload(mime="image/jpeg", data=blob, origin=origin))

    def statuses(self):
        return [e["status"] for e in self.hub.activity.list()]

    def test_preview_then_full_drops_the_preview(self):
        self.send(jpeg(270, 560, 17_000))
        self.send(jpeg(1080, 2243, 420_000))
        import time
        time.sleep(0.2)
        self.assertEqual(len(self.laptop.writes), 1)
        self.assertEqual(len(self.laptop.writes[0].data), len(jpeg(1080, 2243, 420_000)))
        events = self.hub.activity.list()
        self.assertEqual(len(events), 1)                 # no row for the preview itself
        self.assertIn("ignored the small preview", events[0]["reason"])

    def test_full_then_preview_drops_the_preview(self):
        self.send(jpeg(1080, 2243, 420_000))
        self.send(jpeg(270, 560, 17_000))
        import time
        time.sleep(0.2)
        self.assertEqual(len(self.laptop.writes), 1)
        events = self.hub.activity.list()
        self.assertEqual(len(events), 1)
        self.assertIn("ignored the small preview", events[0]["reason"])

    def test_filter_can_be_turned_off(self):
        self.hub.filter_previews = False
        self.send(jpeg(270, 560, 17_000))
        self.send(jpeg(1080, 2243, 420_000))
        self.assertEqual(len(self.laptop.writes), 2)

    def test_lone_small_image_is_released_after_the_hold(self):
        self.send(jpeg(64, 64, 2_000))
        self.assertEqual(len(self.laptop.writes), 0)
        import time
        time.sleep(0.3)
        self.assertEqual(len(self.laptop.writes), 1)

    def test_different_shape_is_not_a_preview(self):
        self.send(jpeg(270, 560, 17_000))
        self.send(jpeg(2000, 1000, 420_000))
        import time
        time.sleep(0.2)
        self.assertEqual(len(self.laptop.writes), 2)

    def test_other_device_is_never_dropped(self):
        self.send(jpeg(1080, 2243, 420_000), origin="phone")
        self.send(jpeg(270, 560, 17_000), origin="tab")
        import time
        time.sleep(0.2)
        self.assertEqual(len(self.laptop.writes), 2)
