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
import logging
import os
import signal
import sys
import time

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("me.mohit.copyclip")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import config as _cfg                 # noqa: E402
from core import logsetup as _logsetup          # noqa: E402
from core.baseline_store import BaselineStore   # noqa: E402
from core.config import ConfigError             # noqa: E402
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

# Config defaults, validation and key handling live in core/config.py. These names stay
# importable from here for the tray, the tests and any external script.
DEFAULT_CONFIG = _cfg.DEFAULT_CONFIG
load_config = _cfg.load_config
_migrate = _cfg.migrate


def setup_logging(level: str) -> None:
    _logsetup.setup_logging(level, os.path.join(HERE, "logs"))


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
        try:
            key = _cfg.read_key_file(key_path)
        except ConfigError as exc:
            raise TransportError(f"{exc}. See termux\\setup_termux.md to create the shared key.") from exc
        host = dev.get("tcp_host") or dev["device"].split(":")[0]
        port = int(dev.get("tcp_port") or 8385)
        return lambda: TcpTransport(host, port, key)

    raise TransportError(f"unknown transport {kind!r} for device {dev.get('name')!r}")


def build_hub(cfg: dict) -> Hub:
    wipe_s = float(cfg.get("sensitive_wipe_s", 10.0))
    laptop = WindowsNode(
        "laptop",
        screenshot_dir=cfg.get("screenshot_backup_dir") or None,
        screenshot_include_devices=cfg.get("screenshot_backup_include_devices", True),
        screenshot_device_subfolders=cfg.get("screenshot_device_subfolders", True),
        device_screenshot_paths=cfg.get("device_screenshot_paths", {}),
        exclude_synced_images_from_history=cfg.get("screenshot_exclude_from_history", True),
        sensitive_wipe_s=wipe_s,
        direct_password_sync=cfg.get("direct_password_sync", False),
        camera_backup=cfg.get("camera_backup", True),
        camera_backup_dir=cfg.get("camera_backup_dir") or None,
        camera_device_subfolders=cfg.get("camera_device_subfolders", False),
    )
    android: dict[str, AndroidNode] = {}
    hub_ref: dict[str, Hub] = {}
    key_path = os.path.join(HERE, cfg.get("key_file", "copyclip.key"))
    try:
        key_hex = _cfg.read_key_file(key_path).decode("ascii", "replace")
    except ConfigError:
        key_hex = ""

    hub_ip = _cfg.resolve_hub_ip(cfg)
    push_port = int(cfg.get("push_port", 8387))

    def on_connect(node: AndroidNode) -> None:
        hub = hub_ref.get("hub")
        if hub is not None:
            hub.on_node_connected(node)
        if key_hex:
            node.send_conf(key_hex, hub_ip, push_port, node.name)

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
            max_image_bytes=cfg["max_image_bytes"],
            image_chunk_bytes=cfg["image_chunk_bytes"],
            allow_sensitive=cfg.get("allow_sensitive", False),
            sensitive_wipe_s=wipe_s,
            camera_backup=cfg.get("camera_backup", True),
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
                      allow_non_text=cfg["allow_non_text"],
                      allow_sensitive=cfg.get("allow_sensitive", False),
                      allow_images=cfg["allow_images"],
                      max_image_bytes=cfg["max_image_bytes"]),
        outbox=Outbox(os.path.join(HERE, cfg["state_dir"], "outbox.json")),
        dedup=RecentClips(ttl=cfg["dedup_ttl_s"]),
        baseline_store=BaselineStore(os.path.join(HERE, cfg["state_dir"], "baseline.json")),
        adopt_on_connect=cfg.get("adopt_on_connect", False),
        filter_previews=cfg.get("filter_screenshot_previews", True),
        direct_password_sync=cfg.get("direct_password_sync", False),
        allow_recopy=cfg.get("allow_recopy", True),
    )
    hub_ref["hub"] = hub
    return hub


def build_push_listener(cfg: dict, hub: Hub):
    """The "Send Clip" inbound tier: see transports/push_listener.py's own docstring for
    why this direction exists at all. Reuses the same key file as the tcp transport tier;
    a device that cannot reach this at all just never uses it, so there is nothing to
    disable per-device.
    """
    from transports.push_listener import PushListener

    key_path = os.path.join(HERE, cfg["key_file"])
    try:
        key = _cfg.read_key_file(key_path)
    except ConfigError as exc:
        log.warning("push listener disabled: %s", exc)
        return None
    return PushListener(int(cfg.get("push_port", 8387)), key, hub)


# ---------------------------------------------------------------- status

def status_from_running_daemon(cfg: dict, out=print) -> int:
    """Report from the live daemon's control API. Starting a second hub here would restart
    each device's ClipBridge underneath the real daemon."""
    import json
    import urllib.request

    path = os.path.join(HERE, cfg["state_dir"], "control.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ctl = json.load(fh)
        req = urllib.request.Request(f"http://127.0.0.1:{int(ctl['port'])}/api/status",
                                     headers={"X-CopyClip-Token": ctl["token"]})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = json.load(resp)
    except Exception as exc:
        out(f"CopyClip is running (pid file present) but its control API did not answer: {exc}")
        return 1
    out(f"daemon  : running, pid {status.get('pid')}, up {status.get('uptime_s', 0)} s, "
        f"v{status.get('version')}")
    out(f"sync    : {'PAUSED' if status.get('paused') else 'active'}")
    bad = 0
    for name, info in status["nodes"].items():
        if name == "laptop":
            continue
        state = "paused" if info["paused"] else "connected" if info["available"] else "UNREACHABLE"
        bad += state == "UNREACHABLE"
        extra = " (locked)" if info.get("locked") else ""
        err = f"  {info['last_error']}" if state == "UNREACHABLE" and info.get("last_error") else ""
        out(f"{name:<8}: {state}{extra}{err}")
    for d in status.get("domains", []):
        out(f"{d['domain']:<8}: continuity={'healthy' if d['healthy'] else 'DEGRADED'}, "
            f"representative={d['representative']}")
    if status.get("outbox"):
        out(f"outbox  : pending {', '.join(status['outbox'])}")
    return 1 if bad else 0


def cmd_status(cfg: dict) -> int:
    from core.single_instance import is_running
    if is_running():
        return status_from_running_daemon(cfg)
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

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("CopyClip cannot start, config.json has problems:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
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
    push_listener = build_push_listener(cfg, hub)

    def handle_signal(signum, frame):
        log.info("signal %s, shutting down", signum)
        if push_listener is not None:
            push_listener.stop()
        hub.stop()
        guard.release()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("CopyClip starting (pid %d, devices: %s)", os.getpid(),
             ", ".join(d["name"] for d in cfg["devices"] if d.get("enabled", True)))
    hub.start()
    if push_listener is not None:
        push_listener.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if push_listener is not None:
            push_listener.stop()
        hub.stop()
        guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
