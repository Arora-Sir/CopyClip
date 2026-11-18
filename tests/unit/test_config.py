import json
import os
import tempfile
import unittest

from tests import fakes  # noqa: F401
import copyclip


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_missing_file_gives_defaults(self):
        cfg = copyclip.load_config(self.path)
        self.assertEqual(cfg["push_port"], 8387)
        self.assertEqual(cfg["log_level"], "INFO")

    def test_file_overrides_defaults_and_keeps_others(self):
        self.write({"push_port": 9000})
        cfg = copyclip.load_config(self.path)
        self.assertEqual(cfg["push_port"], 9000)
        self.assertEqual(cfg["dedup_ttl_s"], 90.0)

    def test_underscore_comment_keys_are_harmless(self):
        self.write({"_note": "x", "devices": [{"name": "a", "device": "h:1"}]})
        self.assertEqual(copyclip.load_config(self.path)["devices"][0]["name"], "a")

    def test_v1_single_device_is_migrated(self):
        self.write({"devices": [], "device": "10.0.0.1:5555", "transport": "adb"})
        cfg = copyclip.load_config(self.path)
        self.assertEqual(cfg["devices"][0]["device"], "10.0.0.1:5555")
        self.assertEqual(cfg["devices"][0]["name"], "phone")

    def test_modern_devices_not_overwritten_by_legacy(self):
        self.write({"devices": [{"name": "tab", "device": "h:2"}], "device": "old"})
        self.assertEqual(copyclip.load_config(self.path)["devices"][0]["name"], "tab")

    def test_malformed_json_raises_config_error(self):
        with open(self.path, "w") as f:
            f.write("{oops")
        with self.assertRaises(copyclip.ConfigError) as cm:
            copyclip.load_config(self.path)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_transport_factory_rejects_unknown_kind(self):
        cfg = copyclip.load_config(self.path)
        with self.assertRaises(copyclip.TransportError):
            copyclip.make_transport_factory(cfg, {"name": "x", "transport": "carrier-pigeon"})

    def test_transport_factory_adb_returns_callable_without_connecting(self):
        cfg = copyclip.load_config(self.path)
        factory = copyclip.make_transport_factory(cfg, {"name": "x", "device": "h:1"})
        self.assertTrue(callable(factory))

    def test_tcp_factory_requires_key(self):
        cfg = copyclip.load_config(self.path)
        cfg["key_file"] = os.path.join(self.tmp.name, "missing.key")
        with self.assertRaises(copyclip.TransportError):
            copyclip.make_transport_factory(cfg, {"name": "x", "transport": "tcp", "device": "h:1"})


if __name__ == "__main__":
    unittest.main()
