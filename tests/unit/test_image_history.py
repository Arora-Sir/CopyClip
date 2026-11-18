import os
import tempfile
import unittest

from core.hub import Hub
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy
from tests.fakes import FakeNode
from tests.unit.test_resaved_image import jpeg
from windows_app.service import ControlService, ServiceError


class SavingLaptop(FakeNode):
    def __init__(self, folder):
        super().__init__("laptop")
        self.folder, self.saved, self.copied, self.texts = folder, {}, [], []

    def write(self, payload):
        path = os.path.join(self.folder, f"{payload.sha256[:8]}.jpg")
        with open(path, "wb") as fh:
            fh.write(payload.data)
        self.saved[payload.sha256] = path
        return super().write(payload)

    def saved_path(self, sha):
        return self.saved.get(sha)

    def copy_text_to_clipboard(self, text):
        self.texts.append(text)

    def copy_file_to_clipboard(self, path, mime):
        self.copied.append((path, mime))


class ImageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.laptop = SavingLaptop(self.tmp.name)
        self.hub = Hub(nodes=[self.laptop, FakeNode("phone")], domains=[],
                       policy=Policy(max_chars=100, allow_images=True, max_image_bytes=10_000_000),
                       outbox=Outbox(os.path.join(self.tmp.name, "o.json")))
        self.svc = ControlService(self.hub, config_path=os.path.join(self.tmp.name, "c.json"),
                                  logs_dir=self.tmp.name, key_path=os.path.join(self.tmp.name, "k"))
        self.hub.on_clip(ClipPayload(mime="image/jpeg", data=jpeg(1080, 2243, 400_000), origin="phone"))
        self.event = self.hub.activity.list()[0]

    def test_event_reports_a_picture_but_never_its_path(self):
        self.assertTrue(self.event["has_image"])
        self.assertNotIn("file", self.event)
        self.assertNotIn(self.tmp.name, str(self.event))

    def test_image_bytes_and_copy(self):
        data, mime = self.svc.activity_image(self.event["id"])
        self.assertEqual((mime, data[:2]), ("image/jpeg", bytes((0xFF, 0xD8))))
        self.assertEqual(self.svc.copy_activity_image(self.event["id"]), {"copied": True})
        self.assertEqual(self.laptop.copied[0][1], "image/jpeg")

    def test_unknown_or_deleted_picture_is_a_clean_404(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.activity_image(9999)
        self.assertEqual(cm.exception.status, 404)
        os.remove(self.laptop.saved_path(next(iter(self.laptop.saved))))
        with self.assertRaises(ServiceError):
            self.svc.copy_activity_image(self.event["id"])


class TextCopyTests(ImageHistoryTests):
    def test_text_clip_can_be_copied_back_but_text_is_never_in_the_event(self):
        self.hub.on_clip(ClipPayload.from_text("hello world", "phone"))
        e = self.hub.activity.list()[0]
        self.assertTrue(e["has_text"])
        self.assertNotIn("text", e)
        self.assertEqual(self.svc.copy_activity(e["id"]), {"copied": True, "chars": 11})
        self.assertEqual(self.laptop.texts, ["hello world"])

    def test_sensitive_and_long_text_cannot_be_copied(self):
        self.hub.on_clip(ClipPayload.from_text("123456", "phone", sensitive=True))
        sens = self.hub.activity.list()[0]
        self.assertFalse(sens["has_text"])
        with self.assertRaises(ServiceError):
            self.svc.copy_activity(sens["id"])

    def test_turning_previews_off_forgets_kept_text(self):
        self.hub.on_clip(ClipPayload.from_text("keep me", "phone"))
        eid = self.hub.activity.list()[0]["id"]
        self.hub.activity.set_previews(False)
        with self.assertRaises(ServiceError):
            self.svc.copy_activity(eid)
