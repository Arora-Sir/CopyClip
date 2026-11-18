"""Loopback HTTP control API plus the static UI, served from inside the tray process.

Security model (the UI is a browser, so it has to be treated like one):
  * bound to 127.0.0.1 only, on an ephemeral port;
  * every API call needs a per-run random token, sent as an HttpOnly SameSite=Strict cookie
    (set once from the launch URL) or as the X-CopyClip-Token header;
  * the Host header must be this server's own loopback address, which defeats DNS rebinding;
  * state-changing calls must send JSON and, when they ride on the cookie, a matching Origin,
    which defeats cross-site form posts;
  * only a fixed list of static files is served, never a path taken from the request.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import ControlService, ServiceError

log = logging.getLogger("copyclip.api")

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/favicon.ico": ("../copyclip.ico", "image/x-icon"),
    "/icon.png": ("icon.png", "image/png"),
}
MAX_BODY = 256 * 1024
COOKIE = "cc_token"

DEVICE_ACTION = re.compile(r"^/api/devices/([\w\-]{1,32})/(pause|resume|reconnect)$")
ACTIVITY_IMAGE = re.compile(r"^/api/activity/(\d{1,12})/(image|copy)$")
DEVICE_ITEM = re.compile(r"^/api/devices/([\w\-]{1,32})$")


class RawReply:
    """A binary reply, for the saved pictures shown in Activity."""

    def __init__(self, data: bytes, mime: str) -> None:
        self.data, self.mime = data, mime


class _BacklogHTTPServer(ThreadingHTTPServer):
    # The stdlib default backlog is 5, which Windows answers with connection refused when a burst of requests arrives together.
    request_queue_size = 128


class ControlServer:
    def __init__(self, service: ControlService, *, port: int = 0, token: str | None = None) -> None:
        self.service = service
        self.token = token or secrets.token_urlsafe(24)
        self._httpd = _BacklogHTTPServer(("127.0.0.1", port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def launch_url(self, route: str = "") -> str:
        """URL that signs the browser in (sets the cookie) and lands on `route`."""
        return f"{self.url}/?t={self.token}{('#/' + route) if route else ''}"

    def start(self) -> None:
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.05),
                                        name="copyclip-api", daemon=True)
        self._thread.start()
        log.info("control API on %s", self.url)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CopyClip"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet: the daemon log is not an access log
                log.debug("%s %s", self.address_string(), fmt % args)

            # -- plumbing -------------------------------------------------
            def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; frame-ancestors 'none'")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, obj) -> None:
                self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

            def _error(self, status: int, message: str, problems=None) -> None:
                self._json(status, {"error": message, "problems": problems or []})

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def _cookie_token(self) -> str:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE:
                        return v
                return ""

            def _authed(self, mutating: bool) -> bool:
                header = self.headers.get("X-CopyClip-Token") or ""
                if header and hmac.compare_digest(header, server.token):
                    return True
                cookie = self._cookie_token()
                if not (cookie and hmac.compare_digest(cookie, server.token)):
                    return False
                if mutating:
                    origin = self.headers.get("Origin") or ""
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                    return origin in (server.url, f"http://localhost:{server.port}") \
                        and ctype == "application/json"
                return True

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ServiceError("request body too large", 413)
                if length == 0:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ServiceError(f"body is not valid JSON: {exc}") from exc

            # -- dispatch -------------------------------------------------
            def _handle(self, method: str) -> None:
                if not self._host_ok():
                    return self._error(403, "bad host")
                url = urlparse(self.path)
                path, query = url.path, parse_qs(url.query)

                if method == "GET" and path in STATIC:
                    return self._static(path, query)
                if not path.startswith("/api/"):
                    return self._error(404, "not found")
                if not self._authed(mutating=method != "GET"):
                    return self._error(401, "unauthorised")
                try:
                    result = self._route(method, path, query)
                except ServiceError as exc:
                    return self._error(exc.status, str(exc), exc.problems)
                except ConfigErrorTypes as exc:
                    return self._error(422, "invalid configuration", getattr(exc, "problems", []))
                except Exception:
                    log.exception("API error on %s %s", method, path)
                    return self._error(500, "internal error; see the log")
                if result is None:
                    return self._error(404, "not found")
                if isinstance(result, RawReply):
                    return self._send(200, result.data, result.mime, {"Cache-Control": "private, max-age=300"})
                self._json(200, result)

            def _static(self, path: str, query: dict) -> None:
                name, ctype = STATIC[path]
                extra = {}
                if path == "/" and "t" in query:
                    token = query["t"][0]
                    if not hmac.compare_digest(token, server.token):
                        return self._error(403, "bad token")
                    extra["Set-Cookie"] = f"{COOKIE}={server.token}; HttpOnly; SameSite=Strict; Path=/"
                full = os.path.normpath(os.path.join(UI_DIR, name))
                try:
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    return self._error(404, "ui file missing")
                self._send(200, data, ctype, extra)

            def _route(self, method: str, path: str, q: dict):
                s = server.service
                first = lambda k, d=None: q.get(k, [d])[0]  # noqa: E731

                if method == "GET":
                    m = ACTIVITY_IMAGE.match(path)
                    if m and m.group(2) == "image":
                        data, mime = s.activity_image(int(m.group(1)))
                        return RawReply(data, mime)
                    if path == "/api/status":
                        return s.status()
                    if path == "/api/activity":
                        return s.activity(limit=_int(first("limit"), 100, 1, 500),
                                          after=_int(first("after"), 0, 0, 10**12),
                                          kind=first("kind"), status=first("status"),
                                          query=first("q"))
                    if path == "/api/logs":
                        return s.logs(level=first("level", "ALL"), device=first("device"),
                                      query=first("q"), limit=_int(first("limit"), 400, 1, 2000))
                    if path == "/api/config":
                        return s.get_config()
                    if path == "/api/key":
                        return s.key_info(reveal=first("reveal") == "1")
                    return None

                body = self._body()
                if not isinstance(body, dict):
                    raise ServiceError("the request body must be a JSON object")
                if method == "PUT":
                    if path == "/api/config":
                        return s.put_config(body)
                    m = DEVICE_ITEM.match(path)
                    if m:
                        return s.update_device(m.group(1), body.get("device", body),
                                               group=body.get("group"))
                    return None
                if method == "DELETE":
                    m = DEVICE_ITEM.match(path)
                    return s.delete_device(m.group(1)) if m else None

                # POST
                m = ACTIVITY_IMAGE.match(path)
                if m and m.group(2) == "copy":
                    return s.copy_activity(int(m.group(1)))
                if path == "/api/ping":
                    return s.ping()
                if path == "/api/pause":
                    return s.set_paused(bool(body.get("paused")), body.get("minutes"))
                if path == "/api/devices":
                    return s.add_device(body.get("device", body), group=body.get("group"))
                if path == "/api/devices/test":
                    return s.test_device(body.get("address", ""), body.get("transport", "adb"))
                m = DEVICE_ACTION.match(path)
                if m:
                    name, action = m.groups()
                    if action == "reconnect":
                        return s.reconnect(None if name == "all" else name)
                    return s.device_pause(name, action == "pause")
                if path == "/api/key/rotate":
                    return s.rotate_key()
                if path == "/api/folders/open":
                    return s.open_folder(str(body.get("which", "")))
                if path == "/api/send-clipboard":
                    return s.send_clipboard()
                if path == "/api/logs/clear":
                    return s.clear_logs()
                if path == "/api/activity/clear":
                    return s.clear_activity()
                if path == "/api/restart":
                    return s.restart()
                if path == "/api/quit":
                    return s.quit()
                if path == "/api/pick-folder":
                    return s.pick_folder(str(body.get("initial", "")))
                if path == "/api/ui/open":
                    return s.open_window(str(body.get("route", "overview")))
                return None

            def do_GET(self): self._handle("GET")          # noqa: E704
            def do_POST(self): self._handle("POST")        # noqa: E704
            def do_PUT(self): self._handle("PUT")          # noqa: E704
            def do_DELETE(self): self._handle("DELETE")    # noqa: E704
            def do_HEAD(self): self._handle("GET")         # noqa: E704

        return Handler


from core.config import ConfigError as ConfigErrorTypes  # noqa: E402


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default
