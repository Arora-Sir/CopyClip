"""The control API over a real loopback socket: auth, CSRF, routing, errors."""

import http.client
import json
import os
import tempfile
import unittest

from tests.fakes import FakeNode
from core import config as cfgmod
from core.hub import Hub
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy
from windows_app.control_api import ControlServer
from windows_app.service import ControlService


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.cfg_path = os.path.join(d, "config.json")
        cfgmod.save_config(self.cfg_path, cfgmod.with_defaults(
            {"devices": [{"name": "phone", "device": "10.0.0.1:5555"}]}))
        os.makedirs(os.path.join(d, "logs"))
        self.hub = Hub(nodes=[FakeNode("laptop"), FakeNode("phone")], domains=[], policy=Policy(),
                       outbox=Outbox(os.path.join(d, "state", "o.json")))
        self.opened = []
        svc = ControlService(self.hub, config_path=self.cfg_path, logs_dir=os.path.join(d, "logs"),
                             key_path=os.path.join(d, "k.key"), open_path=self.opened.append)
        self.server = ControlServer(svc, token="T" * 32)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.port = self.server.port

    def call(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if host:
            h["Host"] = host
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return r.status, payload, r

    @property
    def auth(self):
        return {"X-CopyClip-Token": "T" * 32}

    @property
    def cookie(self):
        return {"Cookie": "cc_token=" + "T" * 32, "Origin": self.server.url}


class AuthTests(ApiBase):
    def test_no_token_is_401(self):
        self.assertEqual(self.call("GET", "/api/status")[0], 401)

    def test_wrong_token_is_401(self):
        self.assertEqual(self.call("GET", "/api/status", headers={"X-CopyClip-Token": "x"})[0], 401)
        self.assertEqual(self.call("GET", "/api/status", headers={"Cookie": "cc_token=nope"})[0], 401)

    def test_header_token_works(self):
        status, body, _ = self.call("GET", "/api/status", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertIn("nodes", body)

    def test_cookie_token_works_for_get(self):
        self.assertEqual(self.call("GET", "/api/status", headers=self.cookie)[0], 200)

    def test_dns_rebinding_host_is_rejected(self):
        self.assertEqual(self.call("GET", "/api/status", headers=self.auth, host="evil.example")[0], 403)
        self.assertEqual(self.call("GET", "/api/status", headers=self.auth,
                                   host=f"127.0.0.1.evil.example:{self.port}")[0], 403)

    def test_localhost_host_header_allowed(self):
        self.assertEqual(self.call("GET", "/api/status", headers=self.auth,
                                   host=f"localhost:{self.port}")[0], 200)

    def test_cookie_post_needs_matching_origin_and_json(self):
        body = {"paused": True}
        no_origin = {"Cookie": self.cookie["Cookie"]}
        self.assertEqual(self.call("POST", "/api/pause", body, no_origin)[0], 401)
        cross = dict(self.cookie, Origin="http://evil.example")
        self.assertEqual(self.call("POST", "/api/pause", body, cross)[0], 401)
        form = dict(self.cookie, **{"Content-Type": "text/plain"})
        self.assertEqual(self.call("POST", "/api/pause", b'{"paused": true}', form)[0], 401)
        self.assertFalse(self.hub.paused)
        self.assertEqual(self.call("POST", "/api/pause", body, self.cookie)[0], 200)
        self.assertTrue(self.hub.paused)

    def test_launch_url_sets_httponly_strict_cookie(self):
        status, _, r = self.call("GET", f"/?t={'T' * 32}")
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_launch_with_bad_token_is_403_and_sets_nothing(self):
        status, _, r = self.call("GET", "/?t=wrong")
        self.assertEqual(status, 403)
        self.assertIsNone(r.getheader("Set-Cookie"))

    def test_security_headers(self):
        _, _, r = self.call("GET", "/api/status", headers=self.auth)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))

    def test_server_only_listens_on_loopback(self):
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")


class StaticTests(ApiBase):
    def test_only_whitelisted_files(self):
        for path in ("/../config.json", "/config.json", "/%2e%2e/config.json", "/copyclip.py",
                     "/ui/index.html", "//etc/passwd"):
            with self.subTest(path=path):
                self.assertIn(self.call("GET", path)[0], (403, 404))

    def test_ui_files_served_without_token_but_api_is_not(self):
        for path in ("/", "/app.css", "/app.js", "/favicon.ico", "/icon.png"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 200)


class RouteTests(ApiBase):
    def test_unknown_route_404_and_method(self):
        self.assertEqual(self.call("GET", "/api/nope", headers=self.auth)[0], 404)
        self.assertEqual(self.call("POST", "/api/nope", {}, self.auth)[0], 404)
        self.assertEqual(self.call("GET", "/nothing")[0], 404)

    def test_config_roundtrip(self):
        status, body, _ = self.call("PUT", "/api/config", {"allow_images": True}, self.auth)
        self.assertEqual((status, body["applied"]), (200, ["allow_images"]))
        status, body, _ = self.call("GET", "/api/config", headers=self.auth)
        self.assertTrue(body["config"]["allow_images"])

    def test_invalid_config_is_422_with_problems(self):
        status, body, _ = self.call("PUT", "/api/config", {"push_port": 1}, self.auth)
        self.assertEqual(status, 422)
        self.assertTrue(body["problems"])

    def test_bad_json_body_400(self):
        status, body, _ = self.call("PUT", "/api/config", b"{nope", self.auth)
        self.assertEqual(status, 400)

    def test_oversize_body_413(self):
        status, _, _ = self.call("PUT", "/api/config", b'{"a": "' + b"x" * 300000 + b'"}', self.auth)
        self.assertEqual(status, 413)

    def test_device_actions(self):
        self.assertEqual(self.call("POST", "/api/devices/phone/pause", {}, self.auth)[0], 200)
        self.assertTrue(self.hub.is_device_paused("phone"))
        self.assertEqual(self.call("POST", "/api/devices/phone/resume", {}, self.auth)[0], 200)
        self.assertEqual(self.call("POST", "/api/devices/ghost/pause", {}, self.auth)[0], 404)

    def test_device_names_cannot_inject_paths(self):
        for name in ("..", "a%2Fb", "x%20y", "a" * 40):
            with self.subTest(name=name):
                self.assertEqual(self.call("POST", f"/api/devices/{name}/pause", {}, self.auth)[0], 404)

    def test_device_crud(self):
        s, b, _ = self.call("POST", "/api/devices",
                            {"device": {"name": "tab", "device": "10.0.0.2:5555"}}, self.auth)
        self.assertEqual((s, b["restart_required"]), (200, ["devices"]))
        s, _, _ = self.call("PUT", "/api/devices/tab", {"device": {"enabled": False}}, self.auth)
        self.assertEqual(s, 200)
        s, _, _ = self.call("DELETE", "/api/devices/tab", None, self.auth)
        self.assertEqual(s, 200)
        s, _, _ = self.call("DELETE", "/api/devices/tab", None, self.auth)
        self.assertEqual(s, 404)

    def test_activity_query_params_are_clamped_and_safe(self):
        self.hub.on_clip(ClipPayload.from_text("hello", "laptop"))
        s, b, _ = self.call("GET", "/api/activity?limit=99999&after=abc&kind=text", headers=self.auth)
        self.assertEqual(s, 200)
        self.assertEqual(len(b["events"]), 1)
        s, b, _ = self.call("GET", "/api/activity?q=%00%ff", headers=self.auth)
        self.assertEqual(s, 200)

    def test_logs_endpoint_with_no_log(self):
        s, b, _ = self.call("GET", "/api/logs?level=ERROR&limit=abc", headers=self.auth)
        self.assertEqual((s, b["entries"]), (200, []))

    def test_folder_open_by_name_only(self):
        self.assertEqual(self.call("POST", "/api/folders/open", {"which": "logs"}, self.auth)[0], 200)
        self.assertEqual(self.call("POST", "/api/folders/open", {"which": "C:\\Windows"}, self.auth)[0], 404)

    def test_key_reveal_requires_explicit_flag(self):
        self.call("POST", "/api/key/rotate", {}, self.auth)
        _, plain, _ = self.call("GET", "/api/key", headers=self.auth)
        _, shown, _ = self.call("GET", "/api/key?reveal=1", headers=self.auth)
        self.assertNotIn("key", plain)
        self.assertEqual(len(shown["key"]), 64)

    def test_unavailable_features_are_501_not_500(self):
        self.assertEqual(self.call("POST", "/api/restart", {}, self.auth)[0], 501)
        self.assertEqual(self.call("POST", "/api/send-clipboard", {}, self.auth)[0], 501)

    def test_concurrent_requests(self):
        import threading
        codes = []

        def go():
            codes.append(self.call("GET", "/api/status", headers=self.auth)[0])
        threads = [threading.Thread(target=go) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(codes, [200] * 20)

    def test_ping_marks_ui_connected(self):
        self.call("POST", "/api/ping", {}, self.auth)
        self.assertTrue(self.call("GET", "/api/status", headers=self.auth)[1]["ui_connected"])


if __name__ == "__main__":
    unittest.main()
