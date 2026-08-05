r"""CopyClip: instant bidirectional clipboard sync between Windows and Android over Tailscale.

Laptop side. Watches the Windows clipboard, mirrors it to the phone, and applies clips the
phone sends back. The phone runs no installed app: ClipBridge executes under the shell UID,
which is the only non-root identity Android grants background clipboard access to.

Run:
    python copyclip.py            # foreground, logs to console and logs\copyclip.log
    python copyclip.py --status   # one-shot connectivity and state check
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import winclip  # noqa: E402
from transports import TransportError  # noqa: E402
from transports.adb_transport import AdbTransport  # noqa: E402

log = logging.getLogger("copyclip")

DEFAULT_CONFIG = {
    "transport": "adb",           # "adb" (Tier 1, tested) or "tcp" (Tier 2, Termux+Shizuku)
    "device": "100.64.0.11:5555",
    "adb": "adb",
    "dex": "bin/clipbridge.dex",

    "tcp_host": "100.64.0.11",
    "tcp_port": 8385,
    "key_file": "copyclip.key",
    "max_chars": 800000,
    "sync_laptop_to_phone": True,
    "sync_phone_to_laptop": True,
    "adopt_phone_clip_on_connect": False,
    "denylist_regex": [],
    "reconnect_min_s": 1.0,
    "reconnect_max_s": 30.0,
    "idle_ping_s": 60.0,
    "log_level": "INFO",
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def make_transport(cfg: dict):
    """Build the configured transport. Both tiers speak the identical ClipBridge protocol."""
    kind = cfg.get("transport", "adb")
    if kind == "adb":
        return AdbTransport(cfg["device"], os.path.join(HERE, cfg["dex"]), adb=cfg["adb"])
    if kind == "tcp":
        from transports.tcp_transport import TcpTransport
        key_path = os.path.join(HERE, cfg["key_file"])
        if not os.path.isfile(key_path):
            raise TransportError(
                f"{key_path} missing. See termux\\setup_termux.md to create the shared key."
            )
        with open(key_path, "rb") as fh:
            key = fh.read().strip()
        if len(key) < 32:
            raise TransportError("key file is too short; use at least 32 bytes")
        return TcpTransport(cfg["tcp_host"], int(cfg["tcp_port"]), key)
    raise TransportError(f"unknown transport {kind!r}; use 'adb' or 'tcp'")


def setup_logging(level: str) -> None:
    logs = os.path.join(HERE, "logs")
    os.makedirs(logs, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logs, "copyclip.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


class CopyClip:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self.transport = None
        self.lock = threading.Lock()

        # Loop guard. Every clip that crosses in either direction is recorded here; a clip is
        # never sent back towards the side it came from. Without this, one copy ping-pongs
        # forever, and it matters doubly here because Samsung Continuity is independently
        # syncing this phone with the Tab S10.
        self.last_hash: str | None = None

        self.phone_locked = False
        self.baseline_pending = not cfg["adopt_phone_clip_on_connect"]
        self.denylist = [re.compile(p) for p in cfg["denylist_regex"]]
        self.stats = {"to_phone": 0, "to_laptop": 0, "skipped": 0, "reconnects": 0}

    # -- filtering ---------------------------------------------------------

    def _rejected(self, text: str, direction: str) -> str | None:
        if not text:
            return "empty"
        if len(text) > self.cfg["max_chars"]:
            # Skipped whole rather than truncated: a half clip pasted into a shell or an
            # editor is worse than no clip, because it looks like it worked.
            return f"too-large ({len(text)} chars > {self.cfg['max_chars']})"
        for pat in self.denylist:
            if pat.search(text):
                return f"denylist:{pat.pattern}"
        return None

    # -- windows -> phone --------------------------------------------------

    def on_windows_change(self) -> None:
        if not self.cfg["sync_laptop_to_phone"]:
            return
        try:
            text = winclip.read_text()
        except winclip.ClipboardBusy as exc:
            log.warning("windows clipboard busy: %s", exc)
            return
        except Exception:
            log.exception("reading windows clipboard failed")
            return

        if text is None:
            return  # image or files: v1 is text only

        h = sha256(text)
        with self.lock:
            if h == self.last_hash:
                return  # our own write coming back, or a no-op change event
            reason = self._rejected(text, "to_phone")
            if reason:
                self.stats["skipped"] += 1
                log.info("skip laptop->phone: %s", reason)
                return
            self.last_hash = h
            transport = self.transport

        if transport is None or not transport.alive():
            log.debug("no channel; laptop clip not forwarded")
            return
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            transport.send_line(f"SET {payload}")
            self.stats["to_phone"] += 1
            log.info("laptop -> phone: %d chars", len(text))
        except TransportError as exc:
            log.warning("send failed: %s", exc)

    # -- phone -> windows --------------------------------------------------

    def handle_frame(self, line: str) -> None:
        verb, _, rest = line.partition(" ")

        if verb == "CLIP":
            parts = line.split(" ", 3)
            if len(parts) < 4:
                log.warning("malformed CLIP frame")
                return
            _, mime, sensitive, payload = parts
            if sensitive == "1":
                self.stats["skipped"] += 1
                log.info("skip phone->laptop: flagged sensitive by source app")
                return
            try:
                text = base64.b64decode(payload).decode("utf-8")
            except Exception:
                log.exception("undecodable CLIP payload")
                return
            self._apply_from_phone(text, mime)

        elif verb == "LOCK":
            locked = rest.strip() == "1"
            if locked != self.phone_locked:
                self.phone_locked = locked
                log.info("phone %s", "locked (writes will be queued)" if locked else "unlocked")

        elif verb == "DEFER":
            # Expected whenever the phone is in your pocket. ClipBridge replays on unlock.
            log.info("phone deferred write (%s); will apply on unlock", rest.strip())

        elif verb == "SKIP":
            log.debug("phone skipped clip: %s", rest.strip())

        elif verb == "ERR":
            log.warning("bridge error: %s", rest.strip())

        elif verb in ("ACK", "PONG", "READY"):
            log.debug("bridge: %s", line)

        else:
            log.debug("unknown frame: %s", line[:80])

    def _apply_from_phone(self, text: str, mime: str) -> None:
        if not self.cfg["sync_phone_to_laptop"]:
            return
        h = sha256(text)
        with self.lock:
            if h == self.last_hash:
                return  # echo of what we just sent
            reason = self._rejected(text, "to_laptop")
            if reason:
                self.stats["skipped"] += 1
                log.info("skip phone->laptop: %s", reason)
                return
            if self.baseline_pending:
                # First clip after connecting is adopted as the baseline only. Starting the
                # daemon should not silently blow away whatever is on the laptop clipboard.
                self.baseline_pending = False
                self.last_hash = h
                log.info("baseline from phone (%d chars), not applied", len(text))
                return
            self.last_hash = h

        try:
            winclip.write_text(text)
            self.stats["to_laptop"] += 1
            log.info("phone -> laptop: %d chars (%s)", len(text), mime)
        except Exception:
            log.exception("writing windows clipboard failed")

    # -- supervisor --------------------------------------------------------

    def run(self) -> int:
        watcher = winclip.ClipboardWatcher(self.on_windows_change)
        watcher.start()

        backoff = self.cfg["reconnect_min_s"]
        while not self.stop.is_set():
            try:
                transport = make_transport(self.cfg)
                transport.start()
            except TransportError as exc:
                log.warning("connect failed: %s", exc)
                log.info("retrying in %.0fs", backoff)
                if self.stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self.cfg["reconnect_max_s"])
                continue

            backoff = self.cfg["reconnect_min_s"]
            with self.lock:
                self.transport = transport
                self.baseline_pending = not self.cfg["adopt_phone_clip_on_connect"]
            log.info("connected to %s", self.cfg["device"])

            try:
                self._pump(transport)
            finally:
                with self.lock:
                    self.transport = None
                transport.close()

            if not self.stop.is_set():
                self.stats["reconnects"] += 1
                log.warning("channel lost; reconnecting in %.0fs", backoff)
                if self.stop.wait(backoff):
                    break
                backoff = min(backoff * 2, self.cfg["reconnect_max_s"])

        watcher.stop()
        log.info("stopped. stats=%s", self.stats)
        return 0

    def _pump(self, transport) -> None:
        """Drain inbound frames until the channel dies."""
        last_seen = time.time()
        pinged = False
        while not self.stop.is_set() and transport.alive():
            try:
                line = transport.inbox.get(timeout=1.0)
            except queue.Empty:
                idle = time.time() - last_seen
                # A half-open channel (phone asleep, Tailscale re-routed) produces no EOF, so
                # probe it rather than sitting on a dead pipe forever.
                if idle > self.cfg["idle_ping_s"] and not pinged:
                    try:
                        transport.send_line("PING")
                        pinged = True
                    except TransportError:
                        return
                elif idle > self.cfg["idle_ping_s"] + 30:
                    log.warning("no response to PING; assuming channel is dead")
                    return
                continue

            last_seen = time.time()
            pinged = False
            if line == "ERR transport-closed":
                return
            try:
                self.handle_frame(line)
            except Exception:
                log.exception("frame handling failed")


def cmd_status(cfg: dict) -> int:
    kind = cfg.get("transport", "adb")
    target = cfg["device"] if kind == "adb" else f"{cfg['tcp_host']}:{cfg['tcp_port']}"
    print(f"tier   : {kind}")
    print(f"target : {target}")
    dex = os.path.join(HERE, cfg["dex"])
    print(f"dex    : {dex} {'OK' if os.path.isfile(dex) else 'MISSING (run build_dex.ps1)'}")
    try:
        text = winclip.read_text()
        print(f"windows: {len(text)} chars on clipboard" if text is not None
              else "windows: clipboard holds no text")
    except Exception as exc:
        print(f"windows: FAILED ({exc})")

    try:
        t = make_transport(cfg)
        t.start()
    except TransportError as exc:
        print(f"phone  : FAILED ({exc})")
        return 1
    try:
        t.send_line("GET")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                line = t.inbox.get(timeout=deadline - time.time())
            except queue.Empty:
                break
            head = line.split(" ", 1)[0]
            if head == "CLIP":
                n = len(base64.b64decode(line.split(" ", 3)[3]).decode("utf-8"))
                print(f"phone  : OK, {n} chars on clipboard")
                return 0
            if head in ("SKIP", "ERR", "LOCK"):
                print(f"phone  : {line}")
                if head == "SKIP":
                    return 0
        print("phone  : connected but no reply to GET")
        return 1
    finally:
        t.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="CopyClip clipboard sync daemon")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--status", action="store_true", help="one-shot connectivity check")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["log_level"])

    if args.status:
        return cmd_status(cfg)

    app = CopyClip(cfg)

    def handle_signal(signum, frame):
        log.info("signal %s, shutting down", signum)
        app.stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("CopyClip starting (device=%s)", cfg["device"])
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
