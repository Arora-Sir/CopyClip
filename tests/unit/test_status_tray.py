import json
import os
import tempfile
import unittest
from unittest import mock

from tests.fakes import FakeNode
import copyclip
from core import config as cfgmod
from core.hub import Hub
from core.outbox import Outbox
from core.policy import Policy
from windows_app import tray
from windows_app.control_api import ControlServer
from windows_app.service import ControlService


class StatusFromDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        cfg_path = os.path.join(d, "config.json")
        cfgmod.save_config(cfg_path, cfgmod.with_defaults({"devices": [{"name": "phone", "device": "h:1"}]}))
        self.nodes = [FakeNode("laptop"), FakeNode("phone")]
        self.hub = Hub(nodes=self.nodes, domains=[], policy=Policy(),
                       outbox=Outbox(os.path.join(d, "state", "o.json")))
        svc = ControlService(self.hub, config_path=cfg_path, logs_dir=d, key_path=os.path.join(d, "k"))
        self.server = ControlServer(svc, token="T" * 32)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.cfg = {"state_dir": d}
        self.lines = []

    def write_control(self, **over):
        data = {"port": self.server.port, "token": "T" * 32, "pid": 1}
        data.update(over)
        with open(os.path.join(self.tmp.name, "control.json"), "w") as f:
            json.dump(data, f)

    def run_status(self):
        rc = copyclip.status_from_running_daemon(self.cfg, out=self.lines.append)
        return rc, "\n".join(self.lines)

    def test_reports_connected_device(self):
        self.write_control()
        rc, text = self.run_status()
        self.assertEqual(rc, 0)
        self.assertIn("phone   : connected", text)
        self.assertIn("sync    : active", text)

    def test_unreachable_device_gives_nonzero(self):
        self.nodes[1]._available = False
        self.nodes[1].last_error = "device offline"
        self.write_control()
        rc, text = self.run_status()
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE  device offline", text)

    def test_paused_and_outbox_shown(self):
        self.hub.paused = True
        self.write_control()
        _, text = self.run_status()
        self.assertIn("PAUSED", text)

    def test_wrong_token_and_missing_file_and_dead_port(self):
        for case in ("token", "missing", "port"):
            with self.subTest(case=case):
                self.lines.clear()
                path = os.path.join(self.tmp.name, "control.json")
                if os.path.exists(path):
                    os.unlink(path)
                if case == "token":
                    self.write_control(token="x" * 32)
                elif case == "port":
                    self.write_control(port=1)
                rc, text = self.run_status()
                self.assertEqual(rc, 1)
                self.assertIn("did not answer", text)


class FakeService:
    def __init__(self):
        self.calls = []
        self.hub = None

    def set_paused(self, paused, minutes=None):
        self.calls.append(("paused", paused, minutes))

    def device_pause(self, name, paused):
        self.calls.append(("device", name, paused))

    def open_folder(self, which):
        self.calls.append(("folder", which))

    def send_clipboard(self):
        self.calls.append(("send",))

    def navigate(self, route):
        self.calls.append(("navigate", route))


class TrayCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        nodes = [FakeNode("laptop"), FakeNode("phone"), FakeNode("tab")]
        self.hub = Hub(nodes=nodes, domains=[], policy=Policy(),
                       outbox=Outbox(os.path.join(self.tmp.name, "o.json")))
        self.svc = FakeService()

        class Srv:
            def launch_url(self, route=""):
                return "http://x/" + route
        self.app = tray.TrayApp(self.hub, {}, self.svc, Srv())
        self.app._refresh_tip = lambda: None
        self.app.CLICK_DEBOUNCE_S = 0.0   # debounce has its own tests below

    def test_pause_commands_go_through_the_service(self):
        self.app._handle_command(tray.IDM_PAUSE_15)
        self.app._handle_command(tray.IDM_PAUSE_60)
        self.app._handle_command(tray.IDM_PAUSE)
        self.assertEqual(self.svc.calls, [("paused", True, 15), ("paused", True, 60), ("paused", True, None)])

    def test_pause_item_resumes_when_paused(self):
        self.hub.paused = True
        self.app._handle_command(tray.IDM_PAUSE)
        self.assertEqual(self.svc.calls, [("paused", False, None)])

    def test_device_submenu_ids_map_to_devices_in_order(self):
        self.app._handle_command(tray.IDM_DEVICE_PAUSE_BASE + 1)
        self.assertEqual(self.svc.calls, [("device", "tab", True)])
        self.app._handle_command(tray.IDM_DEVICE_PAUSE_BASE + 50)  # out of range: ignored
        self.assertEqual(len(self.svc.calls), 1)

    def test_folder_and_send(self):
        self.app._handle_command(tray.IDM_SHOTS)
        self.app._handle_command(tray.IDM_SEND)
        import time
        time.sleep(0.2)
        self.assertIn(("folder", "screenshots"), self.svc.calls)
        self.assertIn(("send",), self.svc.calls)

    def test_open_commands_pick_the_right_screen(self):
        with mock.patch.object(tray.launcher, "open_ui") as ou:
            self.app._handle_command(tray.IDM_DASHBOARD)
            self.app._handle_command(tray.IDM_CONFIG)
            self.app._handle_command(tray.IDM_LOGS)
        self.assertEqual([c.args[1] for c in ou.call_args_list], ["overview", "settings", "logs"])

    def test_left_click_uses_configured_mode(self):
        for mode, expect_flyout in (("flyout", True), ("window", False)):
            with self.subTest(mode=mode):
                with mock.patch.object(tray.cfgmod, "load_config", return_value={"tray_left_click": mode}), \
                        mock.patch.object(tray.launcher, "open_ui") as ou:
                    self.app._on_tray_click()
                self.assertEqual(ou.call_args.kwargs["flyout"], expect_flyout)

    def test_open_commands_also_navigate_an_open_window(self):
        with mock.patch.object(tray.launcher, "open_ui"):
            self.app._handle_command(tray.IDM_CONFIG)
            self.app._handle_command(tray.IDM_LOGS)
        self.assertEqual([c for c in self.svc.calls if c[0] == "navigate"],
                         [("navigate", "settings"), ("navigate", "logs")])

    def test_rapid_clicks_count_as_one(self):
        self.app.CLICK_DEBOUNCE_S = 0.6
        with mock.patch.object(tray.launcher, "open_ui") as ou, \
                mock.patch.object(tray.cfgmod, "load_config", return_value={"tray_left_click": "window"}):
            for _ in range(10):          # one physical click delivers two messages, and people click twice
                self.app._on_tray_click()
        self.assertEqual(ou.call_count, 1)

    def test_a_later_click_is_accepted_again(self):
        self.app.CLICK_DEBOUNCE_S = 0.6
        with mock.patch.object(tray.launcher, "open_ui") as ou, \
                mock.patch.object(tray.cfgmod, "load_config", return_value={"tray_left_click": "window"}), \
                mock.patch.object(tray.time, "monotonic", side_effect=[100.0, 100.1, 101.0]):
            self.app._on_tray_click()
            self.app._on_tray_click()
            self.app._on_tray_click()
        self.assertEqual(ou.call_count, 2)

    def test_left_click_survives_unreadable_config(self):
        with mock.patch.object(tray.cfgmod, "load_config", side_effect=OSError("locked")), \
                mock.patch.object(tray.launcher, "open_ui") as ou:
            self.app._on_tray_click()
        self.assertFalse(ou.call_args.kwargs["flyout"])

    def test_ui_failure_never_propagates_to_the_message_loop(self):
        with mock.patch.object(tray.launcher, "open_ui", side_effect=RuntimeError("edge exploded")):
            self.app.open_ui()   # must not raise

    def test_legacy_flag_uses_old_dashboard_never_the_launcher(self):
        self.app.legacy_ui = True
        fake = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"dashboard": fake}), \
                mock.patch.object(tray.launcher, "open_ui") as ou:
            self.app.open_ui("settings")
        ou.assert_not_called()
        fake.open_dashboard.assert_called_once()
        self.assertEqual(fake.open_dashboard.call_args.kwargs["initial_tab"], 1)


if __name__ == "__main__":
    unittest.main()
