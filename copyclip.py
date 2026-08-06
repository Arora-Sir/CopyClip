r"""CopyClip: instant clipboard sync between Windows and Samsung devices over Tailscale.

Entry point only. All behaviour lives in core/ (routing, dedup, durability, policy),
nodes/ (per-device I/O) and domains/ (Samsung Continuity awareness).

The devices run no installed app: ClipBridge executes under the shell UID via app_process,
the only non-root identity Android grants background clipboard access to.

Run:
    python copyclip.py            # foreground, logs to console and logs\copyclip.log
    python copyclip.py --status   # one-shot connectivity and state check
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.dedup import RecentClips              # noqa: E402
from core.hub import Hub                        # noqa: E402
from core.outbox import Outbox                  # noqa: E402
from core.policy import Policy                  # noqa: E402
from core.single_instance import AlreadyRunning, SingleInstance  # noqa: E402
from domains.sync_domain import SyncDomain      # noqa: E402
from nodes.android_node import AndroidNode      # noqa: E402
from nodes.windows_node import WindowsNode      # noqa: E402
from transports import TransportError           # noqa: E402
from transports.adb_transport import AdbTransport  # noqa: E402

log = logging.getLogger("copyclip")

DEFAULT_CONFIG = {
    "devices": [
        {"name": "phone", "transport": "adb", "device": "100.64.0.11:5555",
         "enabled": True},
    ],
    "domains": [],

    "adb": "adb",
    "dex": "bin/clipbridge.dex",
    "key_file": "copyclip.key",
    "state_dir": "state",

    "max_chars": 800000,
    "denylist_regex": [],
    "allow_non_text": False,

    "adopt_on_connect": False,
    "dedup_ttl_s": 90.0,
    "reconnect_min_s": 1.0,
    "reconnect_max_s": 30.0,
    "idle_ping_s": 60.0,
    "ack_timeout_s": 4.0,

    "log_level": "INFO",
}


# ---------------------------------------------------------------- config

def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return _migrate(cfg)


def _migrate(cfg: dict) -> dict:
    """Accept the v1 single-device config unchanged.

    v1 had a flat `device`/`transport` pair. Rather than force a config rewrite, fold it
    into the v2 device list so an old config keeps working exactly as before.
    """
    if cfg.get("devices"):
        return cfg
    legacy = cfg.get("device")
    if not legacy:
        return cfg
    cfg["devices"] = [{
        "name": "phone",
        "transport": cfg.get("transport", "adb"),
        "device": legacy,
        "tcp_host": cfg.get("tcp_host"),
        "tcp_port": cfg.get("tcp_port"),
        "enabled": True,
    }]
    log.debug("migrated v1 single-device config")
    return cfg


def setup_logging(level: str) -> None:
    logs = os.path.join(HERE, "logs")
    os.makedirs(logs, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logs, "copyclip.log"), maxBytes=2_000_000, backupCount=3,
        encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


# ------------------------------------------------------------- factories

def make_transport_factory(cfg: dict, dev: dict):
    """Build a zero-arg transport factory for one device.

    Returns a factory rather than a transport so each reconnect gets a fresh instance,
    and so the node never has to know which tier it is on.
    """
    kind = dev.get("transport", "adb")

    if kind == "adb":
        target = dev["device"]
        dex = os.path.join(HERE, cfg["dex"])
        adb = cfg.get("adb", "adb")
        return lambda: AdbTransport(target, dex, adb=adb)

    if kind == "tcp":
        from transports.tcp_transport import TcpTransport
        key_path = os.path.join(HERE, cfg["key_file"])
        if not os.path.isfile(key_path):
            raise TransportError(
                f"{key_path} missing. See termux\\setup_termux.md to create the shared key.")
        with open(key_path, "rb") as fh:
            key = fh.read().strip()
        if len(key) < 32:
            raise TransportError("key file is too short; use at least 32 bytes")
        host = dev.get("tcp_host") or dev["device"].split(":")[0]
        port = int(dev.get("tcp_port") or 8385)
        return lambda: TcpTransport(host, port, key)

    raise TransportError(f"unknown transport {kind!r} for device {dev.get('name')!r}")


def build_hub(cfg: dict) -> Hub:
    laptop = WindowsNode("laptop")
    android: dict[str, AndroidNode] = {}
    hub_ref: dict[str, Hub] = {}

    def on_connect(node: AndroidNode) -> None:
        hub = hub_ref.get("hub")
        if hub is not None:
            hub.on_node_connected(node)

    def on_lock_change(node: AndroidNode, locked: bool) -> None:
        hub = hub_ref.get("hub")
        if hub is not None:
            hub.on_node_unlocked(node, locked)

    for dev in cfg["devices"]:
        if not dev.get("enabled", True):
            log.info("device %s disabled in config, skipping", dev.get("name"))
            continue
        node = AndroidNode(
            dev["name"],
            make_transport_factory(cfg, dev),
            reconnect_min_s=cfg["reconnect_min_s"],
            reconnect_max_s=cfg["reconnect_max_s"],
            idle_ping_s=cfg["idle_ping_s"],
            ack_timeout_s=cfg["ack_timeout_s"],
            on_connect=on_connect,
            on_lock_change=on_lock_change,
        )
        android[dev["name"]] = node

    domains = []
    for spec in cfg.get("domains", []):
        members = [android[n] for n in spec["members"] if n in android]
        if len(members) < 1:
            log.warning("domain %s has no enabled members, skipping", spec.get("name"))
            continue
        domains.append(SyncDomain(
            spec["name"], members,
            grace_s=spec.get("grace_s", 8.0),
            fail_threshold=spec.get("fail_threshold", 3),
        ))

    hub = Hub(
        nodes=[laptop, *android.values()],
        domains=domains,
        policy=Policy(max_chars=cfg["max_chars"],
                      denylist_regex=cfg["denylist_regex"],
                      allow_non_text=cfg["allow_non_text"]),
        outbox=Outbox(os.path.join(HERE, cfg["state_dir"], "outbox.json")),
        dedup=RecentClips(ttl=cfg["dedup_ttl_s"]),
        adopt_on_connect=cfg.get("adopt_on_connect", False),
    )
    hub_ref["hub"] = hub
    return hub


# ---------------------------------------------------------------- status

def cmd_status(cfg: dict) -> int:
    print(f"devices : {', '.join(d['name'] for d in cfg['devices'])}")
    for spec in cfg.get("domains", []):
        print(f"domain  : {spec['name']} = {' + '.join(spec['members'])}")
    dex = os.path.join(HERE, cfg["dex"])
    print(f"dex     : {'OK' if os.path.isfile(dex) else 'MISSING (run build_dex.ps1)'}")

    import winclip
    try:
        text = winclip.read_text()
        print(f"laptop  : {len(text)} chars on clipboard" if text is not None
              else "laptop  : clipboard holds no text")
    except Exception as exc:
        print(f"laptop  : FAILED ({exc})")

    hub = build_hub(cfg)
    hub.start()
    try:
        deadline = time.time() + 15
        pending = {n for n in hub.nodes if n != "laptop"}
        while time.time() < deadline and pending:
            for name in list(pending):
                if hub.nodes[name].available:
                    pending.discard(name)
            time.sleep(0.3)
        for name, node in hub.nodes.items():
            if name == "laptop":
                continue
            state = ("connected" if node.available else "UNREACHABLE")
            extra = " (locked)" if node.locked else ""
            err = f"  {node.last_error}" if not node.available and node.last_error else ""
            print(f"{name:<8}: {state}{extra}{err}")
        for domain in hub.domains:
            s = domain.status()
            print(f"{s['domain']:<8}: continuity={'healthy' if s['healthy'] else 'DEGRADED'}"
                  f", representative={s['representative']}")
        if not hub.outbox.is_empty():
            print(f"outbox  : pending {', '.join(hub.outbox.keys())}")
        return 0 if not pending else 1
    finally:
        hub.stop()


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="CopyClip clipboard sync daemon")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--status", action="store_true", help="one-shot connectivity check")
    ap.add_argument("--log-level", default=None,
                    help="override config log_level (DEBUG shows per-frame routing)")
    ap.add_argument("--replace", action="store_true",
                    help="stop an already-running CopyClip and take over")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(args.log_level or cfg["log_level"])

    if args.status:
        return cmd_status(cfg)

    # Refuse to run twice. Two daemons duplicate every write to the devices and make the
    # logs look unreliable, which is far more confusing than a clear refusal here.
    guard = SingleInstance(os.path.join(HERE, cfg["state_dir"], "copyclip.pid"))
    try:
        guard.acquire(replace=args.replace)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 2

    hub = build_hub(cfg)

    def handle_signal(signum, frame):
        log.info("signal %s, shutting down", signum)
        hub.stop()
        guard.release()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("CopyClip starting (pid %d, devices: %s)", os.getpid(),
             ", ".join(d["name"] for d in cfg["devices"] if d.get("enabled", True)))
    hub.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
