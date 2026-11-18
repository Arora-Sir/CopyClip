import json
import os
import tempfile
import unittest

from tests import fakes  # noqa: F401
from tests.fakes import FakeNode
from core import config
from core.config import ConfigError
from core.hub import Hub
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy


def good(**over):
    cfg = config.with_defaults({"devices": [{"name": "phone", "device": "h:1"}]})
    cfg.update(over)
    return cfg


class ValidateTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(config.validate(config.with_defaults({})), [])

    def test_real_world_shape_is_valid(self):
        cfg = good(domains=[{"name": "s", "members": ["phone"]}], _note="hi")
        self.assertEqual(config.validate(cfg), [])

    def test_collects_every_problem_not_just_the_first(self):
        problems = config.validate(good(push_port=80, ack_timeout_s=0, log_level="LOUD"))
        self.assertEqual(len(problems), 3)

    def test_type_confusion(self):
        for key, bad in (("push_port", "8387"), ("push_port", True), ("max_chars", 1.5),
                         ("allow_images", "yes"), ("hub_ip", 5), ("devices", {})):
            with self.subTest(key=key, bad=bad):
                self.assertTrue(config.validate(good(**{key: bad})))

    def test_bounds(self):
        for key, bad in (("push_port", 1023), ("push_port", 65536), ("max_chars", 0),
                         ("sensitive_wipe_s", 0.5), ("dedup_ttl_s", 0)):
            with self.subTest(key=key, bad=bad):
                self.assertTrue(config.validate(good(**{key: bad})))
        self.assertEqual(config.validate(good(push_port=1024)), [])
        self.assertEqual(config.validate(good(push_port=65535)), [])

    def test_reconnect_ordering(self):
        self.assertTrue(config.validate(good(reconnect_min_s=10, reconnect_max_s=5)))

    def test_devices(self):
        dev = {"name": "a", "device": "h:1"}
        cases = {
            "duplicate": [dev, dict(dev)],
            "bad name": [{"name": "has space", "device": "h:1"}],
            "reserved": [{"name": "laptop", "device": "h:1"}],
            "no address": [{"name": "a", "device": " "}],
            "bad transport": [{"name": "a", "device": "h:1", "transport": "ftp"}],
            "bad enabled": [{"name": "a", "device": "h:1", "enabled": "yes"}],
            "not an object": ["phone"],
        }
        for label, devices in cases.items():
            with self.subTest(label):
                self.assertTrue(config.validate(good(devices=devices)))

    def test_domain_must_reference_real_devices(self):
        self.assertTrue(config.validate(good(domains=[{"name": "s", "members": ["ghost"]}])))
        self.assertTrue(config.validate(good(domains=[{"name": "s", "members": []}])))

    def test_bad_regex_reported(self):
        self.assertTrue(config.validate(good(denylist_regex=["("])))


class LoadSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def test_invalid_file_raises_with_all_problems(self):
        with open(self.path, "w") as f:
            json.dump({"push_port": 1, "log_level": "x"}, f)
        with self.assertRaises(ConfigError) as cm:
            config.load_config(self.path)
        self.assertEqual(len(cm.exception.problems), 2)

    def test_non_object_json(self):
        with open(self.path, "w") as f:
            f.write("[1]")
        with self.assertRaises(ConfigError):
            config.load_config(self.path)

    def test_defaults_do_not_alias(self):
        a = config.with_defaults({})
        a["devices"].append("x")
        a["device_screenshot_paths"]["k"] = "v"
        self.assertEqual(config.DEFAULT_CONFIG["devices"], [])
        self.assertEqual(config.DEFAULT_CONFIG["device_screenshot_paths"], {})

    def test_save_roundtrip_preserves_comment_keys(self):
        cfg = good(_note="keep me", push_port=9001)
        config.save_config(self.path, cfg)
        again = config.load_config(self.path)
        self.assertEqual((again["_note"], again["push_port"]), ("keep me", 9001))

    def test_save_rejects_invalid_and_leaves_file_untouched(self):
        config.save_config(self.path, good())
        before = open(self.path, encoding="utf-8").read()
        with self.assertRaises(ConfigError):
            config.save_config(self.path, good(push_port=1))
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)

    def test_save_leaves_no_temp_files(self):
        config.save_config(self.path, good())
        config.save_config(self.path, good(push_port=9100))
        self.assertEqual(os.listdir(self.tmp.name), ["config.json"])

    def test_save_creates_missing_directory(self):
        nested = os.path.join(self.tmp.name, "a", "b", "config.json")
        config.save_config(nested, good())
        self.assertTrue(os.path.isfile(nested))


class ClassifyTests(unittest.TestCase):
    def test_live_vs_restart(self):
        old = good()
        new = dict(old, allow_images=True, push_port=9999, _note="x", log_level="DEBUG")
        live, restart = config.classify_changes(old, new)
        self.assertEqual(live, ["allow_images"])
        self.assertEqual(restart, ["log_level", "push_port"])

    def test_no_change(self):
        self.assertEqual(config.classify_changes(good(), good()), ([], []))

    def test_devices_need_restart(self):
        old = good()
        new = good(devices=[{"name": "tab", "device": "h:2"}])
        self.assertEqual(config.classify_changes(old, new)[1], ["devices"])


class KeyTests(unittest.TestCase):
    def test_key_file_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "k")
            with self.assertRaises(ConfigError):
                config.read_key_file(p)  # missing
            with open(p, "wb") as f:
                f.write(b"short")
            with self.assertRaises(ConfigError):
                config.read_key_file(p)
            with open(p, "wb") as f:
                f.write(b"a" * 64 + b"\n")
            self.assertEqual(config.read_key_file(p), b"a" * 64)

    def test_new_key_is_long_and_unique(self):
        a, b = config.new_key_hex(), config.new_key_hex()
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, b)

    def test_hub_ip_prefers_explicit_config(self):
        self.assertEqual(config.resolve_hub_ip({"hub_ip": "100.64.0.9"}), "100.64.0.9")


class HubReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = Hub(nodes=[FakeNode("laptop"), FakeNode("phone")], domains=[],
                       policy=Policy(), outbox=Outbox(os.path.join(self.tmp.name, "o.json")))

    def test_returns_only_keys_it_applied(self):
        applied = self.hub.reload_config({"allow_images": True, "push_port": 1234, "_x": 1})
        self.assertEqual(applied, ["allow_images"])

    def test_denylist_applies_live(self):
        self.hub.reload_config({"denylist_regex": ["^secret"]})
        self.assertIn("denylist", self.hub.policy.reject_reason(ClipPayload.from_text("secret1", "a")))

    def test_invalid_denylist_leaves_old_one(self):
        self.hub.reload_config({"denylist_regex": ["^a"]})
        with self.assertRaises(Exception):
            self.hub.reload_config({"denylist_regex": ["("]})
        self.assertEqual(len(self.hub.policy.denylist), 1)

    def test_node_limits_propagate(self):
        class Limits(FakeNode):
            def set_max_image_bytes(self, n):
                self.cap = n

            def set_ack_timeout_s(self, s):
                self.ack = s
        node = Limits("phone")
        hub = Hub(nodes=[FakeNode("laptop"), node], domains=[], policy=Policy(),
                  outbox=Outbox(os.path.join(self.tmp.name, "o2.json")))
        hub.reload_config({"max_image_bytes": 1234, "ack_timeout_s": 2.5})
        self.assertEqual((node.cap, node.ack), (1234, 2.5))

    def test_adopt_on_connect_applies(self):
        self.hub.reload_config({"adopt_on_connect": True})
        self.assertTrue(self.hub.adopt_on_connect)

    def test_laptop_storage_update_forwarded_and_empty_camera_dir_clears(self):
        seen = {}

        class Laptop(FakeNode):
            _screenshot_dir = "old"

            def update_config(self, **kw):
                seen.update(kw)
        hub = Hub(nodes=[Laptop("laptop")], domains=[], policy=Policy(),
                  outbox=Outbox(os.path.join(self.tmp.name, "o3.json")))
        hub.reload_config({"screenshot_backup_dir": "C:\\shots", "camera_backup_dir": ""})
        self.assertEqual(seen["screenshot_dir"], "C:\\shots")
        self.assertEqual(seen["camera_backup_dir"], "")


if __name__ == "__main__":
    unittest.main()
