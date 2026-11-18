import os
import tempfile
import unittest

from tests import fakes  # noqa: F401
from core.baseline_store import BaselineStore
from core.models import ClipPayload
from core.outbox import Outbox


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "outbox.json")

    def test_put_peek_clear_keys(self):
        o = Outbox(self.path)
        self.assertTrue(o.is_empty())
        p = ClipPayload.from_text("a", "laptop")
        o.put("node:phone", p)
        self.assertEqual(o.keys(), ["node:phone"])
        self.assertEqual(o.peek("node:phone").sha256, p.sha256)
        o.clear("node:phone")
        self.assertTrue(o.is_empty())
        o.clear("node:phone")  # idempotent

    def test_single_slot_newer_replaces_older(self):
        o = Outbox(self.path)
        o.put("node:phone", ClipPayload.from_text("old", "laptop"))
        o.put("node:phone", ClipPayload.from_text("new", "laptop"))
        self.assertEqual(o.peek("node:phone").text, "new")
        self.assertEqual(len(o.keys()), 1)

    def test_survives_restart(self):
        Outbox(self.path).put("domain:samsung", ClipPayload.from_text("x", "laptop"))
        self.assertEqual(Outbox(self.path).peek("domain:samsung").text, "x")

    def test_image_roundtrip_through_sidecar(self):
        data = bytes(range(256)) * 10
        Outbox(self.path).put("node:tab", ClipPayload(mime="image/png", data=data, origin="laptop"))
        self.assertEqual(Outbox(self.path).peek("node:tab").data, data)

    def test_sensitive_never_hits_disk_and_drop_sensitive(self):
        o = Outbox(self.path)
        o.put("node:phone", ClipPayload.from_text("123456", "laptop", sensitive=True))
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertNotIn("123456", body)
            self.assertNotIn("MTIzNDU2", body)
        o.drop_sensitive()
        self.assertTrue(o.is_empty())

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertTrue(Outbox(self.path).is_empty())

    def test_identical_put_is_noop(self):
        o = Outbox(self.path)
        p = ClipPayload.from_text("a", "laptop")
        o.put("k", p)
        mtime = os.path.getmtime(self.path)
        o.put("k", p)
        self.assertEqual(os.path.getmtime(self.path), mtime)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "baseline.json")

    def test_set_get_persist(self):
        b = BaselineStore(self.path)
        self.assertIsNone(b.get("phone"))
        b.set("phone", "abc")
        self.assertEqual(BaselineStore(self.path).get("phone"), "abc")

    def test_corrupt_and_non_dict(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for content in ("{bad", "[1,2]", "null"):
            with self.subTest(content=content):
                with open(self.path, "w") as f:
                    f.write(content)
                self.assertIsNone(BaselineStore(self.path).get("phone"))

    def test_no_temp_files_left_behind(self):
        b = BaselineStore(self.path)
        b.set("a", "1")
        b.set("a", "2")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["baseline.json"])


if __name__ == "__main__":
    unittest.main()
