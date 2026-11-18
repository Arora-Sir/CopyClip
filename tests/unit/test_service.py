import json
import os
import subprocess
import tempfile
import time
import unittest

from tests.fakes import FakeNode
from core import config as cfgmod
from core.hub import Hub
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy
from windows_app.service import ControlService, ServiceError, parse_log_text

LOG = """2026-11-11 10:00:00 INFO    copyclip.hub: routed text from laptop
2026-11-11 10:00:01 ERROR   copyclip.node.windows: [laptop] write failed
Traceback (most recent call last):
  File "x.py", line 1, in f
OSError: denied
2026-11-11 10:00:07 ERROR   copyclip.node.windows: [laptop] write failed
Traceback (most recent call last):
  File "x.py", line 1, in f
OSError: denied
2026-11-11 10:00:07 ERROR   copyclip.node.windows: (previous message repeated 40 more times)
2026-11-11 10:00:09 WARNING copyclip.node.android: [pixel] connect failed: offline
2026-11-11 10:00:10 WARNING copyclip.node.android: [pixel] connect failed: offline
2026-11-11 10:00:11 INFO    copyclip.node.android: [phone] connected
"""


class ParseTests(unittest.TestCase):
    def test_tracebacks_attach_and_repeats_fold(self):
        e = parse_log_text(LOG)
        self.assertEqual(len(e), 4)
        self.assertIn("OSError: denied", e[1]["detail"])
        self.assertEqual(e[1]["count"], 42)       # 2 seen + 40 summarised
        self.assertEqual(e[2]["count"], 2)        # two identical single-line warnings
        self.assertEqual(e[3]["count"], 1)

    def test_garbage_and_empty_input(self):
        self.assertEqual(parse_log_text(""), [])
        self.assertEqual(parse_log_text("no timestamps here\nat all"), [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.cfg_path = os.path.join(d, "config.json")
        self.logs = os.path.join(d, "logs")
        os.makedirs(self.logs)
        cfg = cfgmod.with_defaults({"devices": [
            {"name": "phone", "device": "10.0.0.1:5555"},
            {"name": "tab", "device": "10.0.0.2:5555"},
            {"name": "pixel", "device": "10.0.0.3:5555", "enabled": False}]})
        cfgmod.save_config(self.cfg_path, cfg)
        self.nodes = {n: FakeNode(n) for n in ("laptop", "phone", "tab")}
        self.hub = Hub(nodes=list(self.nodes.values()), domains=[], policy=Policy(),
                       outbox=Outbox(os.path.join(d, "state", "o.json")))
        self.opened, self.calls, self.restarted = [], [], []
        self.clip = "hello"
        self.svc = ControlService(
            self.hub, config_path=self.cfg_path, logs_dir=self.logs,
            key_path=os.path.join(d, "copyclip.key"),
            open_path=self.opened.append, run_cmd=self.fake_run,
            read_clipboard=lambda: self.clip, restart=lambda: self.restarted.append(1),
            rollover_logs=lambda: self.calls.append("rollover"))
        self.run_result = subprocess.CompletedProcess([], 0, stdout="connected to x", stderr="")

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        if isinstance(self.run_result, Exception):
            raise self.run_result
        return self.run_result

    def saved(self):
        return cfgmod.load_config(self.cfg_path)


class StatusActivityTests(Base):
    def test_status_enriched_with_config(self):
        s = self.svc.status()
        self.assertEqual(s["nodes"]["phone"]["address"], "10.0.0.1:5555")
        self.assertEqual(s["nodes"]["laptop"]["kind"], "this-pc")
        self.assertIn("version", s)
        self.assertFalse(s["ui_connected"])
        self.svc.ping()
        self.assertTrue(self.svc.status()["ui_connected"])

    def test_outbox_targets_named(self):
        self.hub.outbox.put("node:tab", ClipPayload.from_text("x", "laptop"))
        self.assertEqual(self.svc.status()["outbox_targets"], ["tab"])

    def test_activity_endpoint_and_sensitive_filter(self):
        self.hub.policy.allow_sensitive = True
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        self.hub.on_clip(ClipPayload.from_text("123456", "laptop", sensitive=True))
        self.assertEqual(len(self.svc.activity()["events"]), 2)
        sens = self.svc.activity(kind="sensitive")["events"]
        self.assertEqual(len(sens), 1)
        self.assertTrue(sens[0]["sensitive"])
        self.svc.clear_activity()
        self.assertEqual(self.svc.activity()["total"], 0)


class LogTests(Base):
    def write_log(self, text=LOG):
        with open(os.path.join(self.logs, "copyclip.log"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_log_is_empty_not_error(self):
        self.assertEqual(self.svc.logs()["entries"], [])

    def test_level_device_and_query_filters(self):
        self.write_log()
        self.assertEqual(len(self.svc.logs()["entries"]), 4)
        self.assertEqual(len(self.svc.logs(level="ERROR")["entries"]), 1)
        self.assertEqual(len(self.svc.logs(level="warning")["entries"]), 2)
        self.assertEqual(len(self.svc.logs(device="pixel")["entries"]), 1)
        self.assertEqual(len(self.svc.logs(query="TRACEBACK")["entries"]), 1)
        self.assertEqual(self.svc.logs(device="ghost")["entries"], [])

    def test_counts_reflect_folding(self):
        self.write_log()
        r = self.svc.logs()
        self.assertEqual(r["total_lines"], 1 + 42 + 2 + 1)
        self.assertEqual(r["folded_groups"], 2)

    def test_limit_keeps_newest(self):
        self.write_log("".join(f"2026-11-11 10:00:{i:02d} INFO    a.b: msg {i}\n" for i in range(50)))
        out = self.svc.logs(limit=5)["entries"]
        self.assertEqual([e["message"] for e in out][-1], "msg 49")
        self.assertEqual(len(out), 5)

    def test_huge_log_only_tails(self):
        self.write_log("".join(f"2026-11-11 10:00:00 INFO    a.b: line {i}\n" for i in range(60000)))
        r = self.svc.logs(limit=10)
        self.assertEqual(r["entries"][-1]["message"], "line 59999")

    def test_clear_uses_rollover(self):
        self.svc.clear_logs()
        self.assertIn("rollover", self.calls)


class ConfigTests(Base):
    def test_put_applies_live_and_saves(self):
        r = self.svc.put_config({"allow_images": True, "max_chars": 1000})
        self.assertTrue(r["saved"])
        self.assertEqual(sorted(r["applied"]), ["allow_images", "max_chars"])
        self.assertEqual(r["restart_required"], [])
        self.assertTrue(self.hub.policy.allow_images)
        self.assertEqual(self.saved()["max_chars"], 1000)

    def test_restart_keys_are_reported_and_saved_not_applied(self):
        r = self.svc.put_config({"push_port": 9100})
        self.assertEqual(r["restart_required"], ["push_port"])
        self.assertEqual(self.saved()["push_port"], 9100)

    def test_noop_does_not_write(self):
        before = os.path.getmtime(self.cfg_path)
        time.sleep(0.02)
        self.assertFalse(self.svc.put_config({"max_chars": 800000})["saved"])
        self.assertEqual(os.path.getmtime(self.cfg_path), before)

    def test_invalid_rejected_with_all_problems_and_file_unchanged(self):
        before = open(self.cfg_path, encoding="utf-8").read()
        with self.assertRaises(ServiceError) as cm:
            self.svc.put_config({"push_port": 80, "ack_timeout_s": 0})
        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(len(cm.exception.problems), 2)
        self.assertEqual(open(self.cfg_path, encoding="utf-8").read(), before)

    def test_empty_and_wrong_type_bodies(self):
        for body in ({}, [], None, "x"):
            with self.subTest(body=body):
                with self.assertRaises(ServiceError):
                    self.svc.put_config(body)

    def test_comment_keys_cannot_be_overwritten_by_ui(self):
        self.svc.put_config({"_note": "hacked", "max_chars": 5})
        self.assertNotIn("_note", self.saved())

    def test_external_edit_between_reads_is_not_clobbered(self):
        raw = json.load(open(self.cfg_path, encoding="utf-8"))
        raw["max_chars"] = 123
        json.dump(raw, open(self.cfg_path, "w", encoding="utf-8"))
        self.svc.put_config({"allow_images": True})
        self.assertEqual(self.saved()["max_chars"], 123)

    def test_get_config_lists_restart_keys(self):
        g = self.svc.get_config()
        self.assertIn("push_port", g["restart_keys"])
        self.assertNotIn("allow_images", g["restart_keys"])

    def test_corrupt_config_file_falls_back_to_defaults_for_reads(self):
        open(self.cfg_path, "w").write("{broken")
        self.assertEqual(self.svc.get_config()["config"]["push_port"], 8387)


class NavigationTests(Base):
    def test_navigate_bumps_a_counter_the_window_can_follow(self):
        first = self.svc.status()["nav"]
        self.svc.navigate("settings")
        second = self.svc.status()["nav"]
        self.assertEqual(second["route"], "settings")
        self.assertEqual(second["id"], first["id"] + 1)
        self.svc.navigate("settings")
        self.assertEqual(self.svc.status()["nav"]["id"], second["id"] + 1)   # same screen again still counts

    def test_unknown_screen_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.navigate("../etc")


class PauseTests(Base):
    def test_pause_resume(self):
        self.svc.set_paused(True)
        self.assertTrue(self.hub.paused)
        self.svc.set_paused(False)
        self.assertFalse(self.hub.paused)

    def test_timed_pause_sets_deadline_and_auto_resumes(self):
        before = time.time()
        r = self.svc.set_paused(True, minutes=0.02)          # 1.2 s
        self.assertGreater(r["paused_until"], before)
        self.assertTrue(self.hub.paused)
        deadline = time.time() + 5
        while self.hub.paused and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.hub.paused)
        self.assertIsNone(self.svc.paused_until)

    def test_manual_resume_cancels_timer(self):
        self.svc.set_paused(True, minutes=5)
        self.svc.set_paused(False)
        self.assertIsNone(self.svc.paused_until)
        self.assertIsNone(self.svc._pause_timer)

    def test_bad_minutes(self):
        for m in (-1, 99999):
            with self.subTest(m=m):
                with self.assertRaises(ServiceError):
                    self.svc.set_paused(True, minutes=m)

    def test_device_pause_validates_name(self):
        self.svc.device_pause("tab", True)
        self.assertTrue(self.hub.is_device_paused("tab"))
        for bad in ("ghost", "laptop"):
            with self.assertRaises(ServiceError):
                self.svc.device_pause(bad, True)

    def test_device_pause_survives_a_restart(self):
        self.svc.device_pause("tab", True)
        self.svc.device_pause("phone", True)
        self.svc.device_pause("phone", False)
        hub2 = Hub(nodes=list(self.nodes.values()), domains=[], policy=Policy(),
                   outbox=Outbox(os.path.join(self.tmp.name, "state", "o2.json")))
        ControlService(hub2, config_path=self.cfg_path, logs_dir=self.logs,
                       key_path=os.path.join(self.tmp.name, "copyclip.key"))
        self.assertTrue(hub2.is_device_paused("tab"))
        self.assertFalse(hub2.is_device_paused("phone"))

    def test_corrupt_paused_list_is_ignored(self):
        path = os.path.join(self.tmp.name, "state", "paused_devices.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        ControlService(self.hub, config_path=self.cfg_path, logs_dir=self.logs,
                       key_path=os.path.join(self.tmp.name, "copyclip.key"))
        self.assertFalse(self.hub.is_device_paused("tab"))


class DeviceTests(Base):
    def test_add_edit_delete_roundtrip_and_restart_flag(self):
        r = self.svc.add_device({"name": "watch", "device": "10.0.0.9:5555"})
        self.assertEqual(r["restart_required"], ["devices"])
        self.assertIn("watch", [d["name"] for d in self.saved()["devices"]])
        self.svc.update_device("watch", {"device": "10.0.0.10:5555", "enabled": False})
        w = [d for d in self.saved()["devices"] if d["name"] == "watch"][0]
        self.assertEqual((w["device"], w["enabled"]), ("10.0.0.10:5555", False))
        self.svc.delete_device("watch")
        self.assertNotIn("watch", [d["name"] for d in self.saved()["devices"]])

    def test_validation_errors(self):
        for dev in ({"name": "phone", "device": "x:1"},          # duplicate
                    {"name": "bad name", "device": "x:1"},
                    {"name": "laptop", "device": "x:1"},
                    {"name": "z", "device": ""},
                    {"name": "z", "device": "x:1", "transport": "ftp"}):
            with self.subTest(dev=dev):
                with self.assertRaises(ServiceError) as cm:
                    self.svc.add_device(dev)
                self.assertEqual(cm.exception.status, 422)
        self.assertEqual(len(self.saved()["devices"]), 3)

    def test_unknown_device_404(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.delete_device("ghost")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(ServiceError):
            self.svc.update_device("ghost", {"device": "x:1"})

    def test_unknown_fields_are_dropped(self):
        self.svc.add_device({"name": "w", "device": "x:1", "evil": "rm -rf"})
        w = [d for d in self.saved()["devices"] if d["name"] == "w"][0]
        self.assertNotIn("evil", w)

    def test_groups(self):
        self.svc.add_device({"name": "w", "device": "x:1"}, group="samsung")
        self.svc.update_device("phone", {}, group="samsung")
        doms = {d["name"]: d["members"] for d in self.saved()["domains"]}
        self.assertEqual(sorted(doms["samsung"]), ["phone", "w"])
        self.svc.update_device("w", {}, group="none")
        self.assertEqual(self.saved()["domains"][0]["members"], ["phone"])

    def test_rename_updates_domains_and_delete_prunes_empty_domains(self):
        self.svc.update_device("tab", {}, group="samsung")
        self.svc.update_device("tab", {"name": "tablet"})
        self.assertEqual(self.saved()["domains"][0]["members"], ["tablet"])
        self.svc.delete_device("tablet")
        self.assertEqual(self.saved()["domains"], [])

    def test_reconnect_all_enabled_adb_only(self):
        r = self.svc.reconnect()
        time.sleep(0.2)
        self.assertEqual(sorted(r["started"]), ["phone", "tab"])  # pixel is disabled
        self.assertTrue(all(c[1] == "connect" for c in self.calls))

    def test_reconnect_unknown(self):
        with self.assertRaises(ServiceError):
            self.svc.reconnect("pixel")  # disabled counts as not reconnectable

    def test_test_device_steps(self):
        self.run_result = subprocess.CompletedProcess([], 0, stdout="connected to 1.2.3.4:5555", stderr="")
        out = self.svc.test_device("1.2.3.4:5555")
        self.assertEqual([s["step"] for s in out["steps"]],
                         ["ADB reachable", "Device authorised and online"])
        self.assertFalse(out["ok"])  # get-state returned "connected..." not "device"

    def test_test_device_failure_and_exception(self):
        self.run_result = subprocess.CompletedProcess([], 0, stdout="", stderr="cannot connect")
        out = self.svc.test_device("1.2.3.4:5555")
        self.assertEqual(len(out["steps"]), 1)
        self.assertFalse(out["ok"])
        self.run_result = subprocess.TimeoutExpired("adb", 8)
        out = self.svc.test_device("1.2.3.4:5555")
        self.assertFalse(out["steps"][0]["ok"])

    def test_test_device_requires_address(self):
        with self.assertRaises(ServiceError):
            self.svc.test_device("  ")


class KeyAndMiscTests(Base):
    def test_rotate_writes_key_updates_nodes_and_listener(self):
        class Conf(FakeNode):
            key_hex = ""
            hub_host = "100.64.0.1"
            push_port = 8387
            device_name = "phone"

            def send_conf(self, key_hex, host, port, name):
                self.key_hex = key_hex
        node = Conf("phone")
        hub = Hub(nodes=[FakeNode("laptop"), node], domains=[], policy=Policy(),
                  outbox=Outbox(os.path.join(self.tmp.name, "o2.json")))

        class Listener:
            key = b"old"
        listener = Listener()
        svc = ControlService(hub, config_path=self.cfg_path, logs_dir=self.logs,
                             key_path=os.path.join(self.tmp.name, "k.key"), push_listener=listener)
        r = svc.rotate_key()
        written = open(os.path.join(self.tmp.name, "k.key"), encoding="utf-8").read()
        self.assertEqual(len(written), 64)
        self.assertEqual(node.key_hex, written)
        self.assertEqual(listener.key, written.encode())
        self.assertEqual(r["updated_devices"], ["phone"])
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith(".key-")], [])

    def test_key_info_hides_value_unless_revealed(self):
        self.assertFalse(self.svc.key_info()["exists"])
        self.svc.rotate_key()
        info = self.svc.key_info()
        self.assertTrue(info["exists"])
        self.assertNotIn("key", info)
        self.assertEqual(len(self.svc.key_info(reveal=True)["key"]), 64)

    def test_reveal_missing_key_404(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.key_info(reveal=True)
        self.assertEqual(cm.exception.status, 404)

    def test_open_folder_targets(self):
        self.svc.put_config({"screenshot_backup_dir": os.path.join(self.tmp.name, "shots"),
                             "screenshot_device_subfolders": True,
                             "device_screenshot_paths": {"tab": os.path.join(self.tmp.name, "tabdir")}})
        base = os.path.join(self.tmp.name, "shots")
        self.svc.open_folder("screenshots")
        self.svc.open_folder("camera")
        self.svc.open_folder("device:phone")
        self.svc.open_folder("device:tab")
        self.svc.open_folder("logs")
        self.assertEqual(self.opened, [base, os.path.join(base, "Camera"),
                                       os.path.join(base, "phone"),
                                       os.path.join(self.tmp.name, "tabdir"), self.logs])

    def test_open_folder_rejects_unknown_and_never_takes_raw_paths(self):
        for bad in ("C:\\Windows", "..", "unknown"):
            with self.subTest(bad=bad):
                with self.assertRaises(ServiceError):
                    self.svc.open_folder(bad)
        self.assertEqual(self.opened, [])

    def test_send_clipboard(self):
        self.assertEqual(self.svc.send_clipboard()["sent"], 5)
        self.assertEqual(len(self.nodes["phone"].writes), 1)
        self.clip = ""
        with self.assertRaises(ServiceError):
            self.svc.send_clipboard()

    def test_restart_and_unavailable_modes(self):
        self.svc.restart()
        time.sleep(0.6)
        self.assertEqual(self.restarted, [1])
        bare = ControlService(self.hub, config_path=self.cfg_path, logs_dir=self.logs,
                              key_path="k")
        for fn in (bare.restart, bare.send_clipboard, bare.clear_logs):
            with self.assertRaises(ServiceError) as cm:
                fn()
            self.assertEqual(cm.exception.status, 501)


if __name__ == "__main__":
    unittest.main()
