"""Aggressive tests: seeded fuzzing of every parser that sees untrusted input, plus concurrency.

Deterministic (fixed seeds) so a failure reproduces. The invariant everywhere is the same:
hostile or garbage input must be rejected cleanly and must never raise out of a thread that
the daemon depends on, hang, or corrupt state.
"""

import http.client
import json
import os
import random
import string
import tempfile
import threading
import unittest
import urllib.parse

from tests.fakes import FakeNode
from core import config as cfgmod
from core.activity import ActivityLog
from core.dedup import RecentClips
from core.hub import Hub
from core.models import ClipPayload
from core.outbox import Outbox
from core.policy import Policy
from nodes.android_node import AndroidNode
from tests.fakes import FakeTransport
from transports import framing
from transports.framing import AuthError
from windows_app.control_api import ControlServer
from windows_app.service import ControlService, parse_log_text

KEY = b"k" * 32
ALPHABET = string.printable + "éم\U0001F600\x00\xff"


def rnd_text(r, n=None):
    return "".join(r.choice(ALPHABET) for _ in range(n if n is not None else r.randint(0, 80)))


class FuzzTests(unittest.TestCase):
    def test_framing_open_frame_never_raises_anything_but_autherror(self):
        r = random.Random(1)
        for _ in range(3000):
            raw = rnd_text(r)
            try:
                framing.open_frame(KEY, raw, r.randint(-1, 5))
            except AuthError:
                pass

    def test_framing_roundtrips_for_random_payloads(self):
        r = random.Random(2)
        for i in range(1, 500):
            p = rnd_text(r).replace("\n", "")
            self.assertEqual(framing.open_frame(KEY, framing.seal(KEY, i, p), i - 1), (i, p))

    def test_framing_every_single_byte_flip_is_detected(self):
        raw = framing.seal(KEY, 7, "SET aGVsbG8gd29ybGQ=")
        for i in range(len(raw)):
            flipped = raw[:i] + ("X" if raw[i] != "X" else "Y") + raw[i + 1:]
            with self.assertRaises(AuthError, msg=f"flip at {i}"):
                framing.open_frame(KEY, flipped, 0)

    def test_android_node_handle_survives_garbage_lines(self):
        r = random.Random(3)
        node = AndroidNode("phone", FakeTransport, max_image_bytes=1000)
        node._transport = FakeTransport()
        node._connected = True
        got = []
        node.subscribe(got.append, got.append)
        verbs = ["CLIP", "DESC", "CLIPB", "CLIPC", "STAMP", "LOCK", "READY", "ACK", "DEFER",
                 "ERR", "PONG", "STREAM-PORT", "SKIP", "", "SET", "\x00"]
        for _ in range(4000):
            line = " ".join([r.choice(verbs)] + [rnd_text(r, r.randint(0, 20)) for _ in range(r.randint(0, 7))])
            node._handle(line)   # must never raise
        node._handle("CLIPB x image/png 0 " + "0" * 64 + " 5 1")
        node._handle("CLIPC x 0 AAAA")   # still functional afterwards

    def test_log_parser_survives_garbage(self):
        r = random.Random(4)
        parse_log_text("\n".join(rnd_text(r, r.randint(0, 120)) for _ in range(3000)))

    def test_config_validate_never_raises_on_random_json_shapes(self):
        r = random.Random(5)

        def junk(depth=0):
            choice = r.randint(0, 7)
            if choice == 0:
                return None
            if choice == 1:
                return r.choice([True, False])
            if choice == 2:
                return r.randint(-10**9, 10**9)
            if choice == 3:
                return r.random() * 1e6
            if choice == 4:
                return rnd_text(r, 12)
            if depth < 3 and choice == 5:
                return [junk(depth + 1) for _ in range(r.randint(0, 3))]
            if depth < 3 and choice == 6:
                return {rnd_text(r, 5): junk(depth + 1) for _ in range(r.randint(0, 3))}
            return float("nan")
        keys = list(cfgmod.DEFAULT_CONFIG)
        for _ in range(1500):
            cfg = cfgmod.with_defaults({})
            for k in r.sample(keys, r.randint(1, 6)):
                cfg[k] = junk()
            problems = cfgmod.validate(cfg)
            self.assertIsInstance(problems, list)

    def test_payload_dict_loader_rejects_junk_without_crashing_the_outbox(self):
        r = random.Random(6)
        with tempfile.TemporaryDirectory() as d:
            for i in range(60):
                path = os.path.join(d, f"o{i}.json")
                with open(path, "w", encoding="utf-8", errors="replace") as f:
                    f.write(json.dumps({rnd_text(r, 4): rnd_text(r, 10)}) if i % 2 else rnd_text(r, 200))
                Outbox(path)   # corrupt state must start empty, never raise

    def test_dedup_and_policy_with_random_clips(self):
        r = random.Random(7)
        d, pol = RecentClips(max_entries=50), Policy(max_chars=40, allow_images=True, max_image_bytes=64)
        for _ in range(3000):
            mime = r.choice(["text/plain", "text/html", "image/png", "application/x", ""])
            p = ClipPayload(mime=mime, data=bytes(r.getrandbits(8) for _ in range(r.randint(0, 100))),
                            origin=r.choice("abc"), sensitive=r.random() < .2)
            pol.reject_reason(p)
            d.observe(p.sha256, p.origin)
            d.should_deliver(p.sha256, "x")
        self.assertLessEqual(len(d.snapshot()), 50 + 1)


class ApiFuzzTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        cfg = os.path.join(d, "config.json")
        cfgmod.save_config(cfg, cfgmod.with_defaults({"devices": [{"name": "phone", "device": "h:1"}]}))
        os.makedirs(os.path.join(d, "logs"))
        self.hub = Hub(nodes=[FakeNode("laptop"), FakeNode("phone")], domains=[], policy=Policy(),
                       outbox=Outbox(os.path.join(d, "o.json")))
        self.cfg_path = cfg
        svc = ControlService(self.hub, config_path=cfg, logs_dir=os.path.join(d, "logs"),
                             key_path=os.path.join(d, "k"), open_path=lambda p: None)
        self.server = ControlServer(svc, token="T" * 32)
        self.server.start()
        self.addCleanup(self.server.stop)

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        h = {"X-CopyClip-Token": "T" * 32}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_random_bodies_and_routes_never_500(self):
        r = random.Random(8)
        paths = ["/api/config", "/api/pause", "/api/devices", "/api/devices/test", "/api/devices/phone",
                 "/api/devices/phone/pause", "/api/folders/open", "/api/activity", "/api/logs", "/api/key"]
        for _ in range(250):
            method = r.choice(["GET", "POST", "PUT", "DELETE"])
            body = None
            if method in ("POST", "PUT"):
                body = r.choice([
                    {rnd_text(r, 6): rnd_text(r, 10) for _ in range(r.randint(0, 4))},
                    [rnd_text(r, 5)], rnd_text(r, 30).encode("utf-8", "replace"), {"paused": rnd_text(r, 3)},
                    {"minutes": r.choice([None, "x", -5, 1e9, [1]])},
                    {"push_port": r.choice([None, "x", 0, 1.5, [1], {}])},
                    {"which": rnd_text(r, 8)}, {"address": rnd_text(r, 8), "transport": rnd_text(r, 4)},
                ])
            path = r.choice(paths) + r.choice(["", "?limit=" + urllib.parse.quote(rnd_text(r, 4)), "?after=-9"])
            status = self.call(method, path, body)
            self.assertLess(status, 500, f"{method} {path} body={body!r} returned {status}")
        # and the config on disk is still valid after all that abuse
        self.assertEqual(cfgmod.validate(cfgmod.load_config(self.cfg_path, validate_values=False)), [])

    def test_hammering_the_server_from_many_threads(self):
        errors = []

        def worker(seed):
            r = random.Random(seed)
            for _ in range(40):
                try:
                    s = self.call("GET", r.choice(["/api/status", "/api/activity", "/api/logs", "/api/config"]))
                    if s != 200:
                        errors.append(s)
                except Exception as exc:
                    errors.append(repr(exc))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])

    def test_concurrent_config_writers_never_corrupt_the_file(self):
        errors = []

        def writer(i):
            for n in range(25):
                try:
                    self.call("PUT", "/api/config", {"max_chars": 1000 + i * 100 + n})
                except Exception as exc:
                    errors.append(repr(exc))
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        final = cfgmod.load_config(self.cfg_path)          # parses and validates
        self.assertTrue(1000 <= final["max_chars"] < 2000)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith(".config-")], [])


class HubStressTests(unittest.TestCase):
    def test_concurrent_clips_pause_reload_and_drain_stay_consistent(self):
        with tempfile.TemporaryDirectory() as d:
            nodes = [FakeNode(n) for n in ("laptop", "phone", "tab", "pixel")]
            hub = Hub(nodes=nodes, domains=[], policy=Policy(), outbox=Outbox(os.path.join(d, "o.json")),
                      activity=ActivityLog(capacity=100))
            errors = []

            def clips(seed):
                r = random.Random(seed)
                for i in range(150):
                    try:
                        hub.on_clip(ClipPayload.from_text(f"clip {seed} {i} {r.random()}", r.choice(["laptop", "phone", "tab"])))
                    except Exception as exc:
                        errors.append(repr(exc))

            def toggler():
                r = random.Random(99)
                for _ in range(300):
                    try:
                        name = r.choice(["phone", "tab", "pixel"])
                        (hub.pause_device if r.random() < .5 else hub.resume_device)(name)
                        hub.reload_config({"max_chars": r.randint(10, 1000), "allow_images": r.random() < .5})
                        hub.status()
                        hub._drain_outbox()
                    except Exception as exc:
                        errors.append(repr(exc))
            threads = [threading.Thread(target=clips, args=(i,)) for i in range(6)] + [threading.Thread(target=toggler)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
            self.assertEqual(errors, [])
            self.assertLessEqual(len(hub.activity), 100)
            self.assertGreater(hub.stats["delivered"], 0)

    def test_outbox_survives_concurrent_puts_and_clears(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "o.json")
            box = Outbox(path)
            errors = []

            def worker(i):
                for n in range(60):
                    try:
                        box.put(f"node:n{i % 3}", ClipPayload.from_text(f"{i}-{n}", "laptop"))
                        if n % 4 == 0:
                            box.clear(f"node:n{i % 3}")
                    except Exception as exc:
                        errors.append(repr(exc))
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
            self.assertEqual(errors, [])
            Outbox(path)   # the file on disk is still loadable


if __name__ == "__main__":
    unittest.main()
