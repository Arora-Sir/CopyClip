"""Single source of truth for CopyClip configuration.

One module owns the defaults, validation, migration, key-file reading and the atomic save,
so the daemon, the tray, the control API and the UI cannot disagree about any of them.
Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import tempfile
from typing import Any

log = logging.getLogger("copyclip.config")


class ConfigError(ValueError):
    """The config file is unreadable or holds values the daemon cannot run with."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


DEFAULT_CONFIG: dict[str, Any] = {
    "devices": [],
    "domains": [],

    "adb": "adb",
    "dex": "bin/clipbridge.dex",
    "key_file": "copyclip.key",
    "state_dir": "state",

    "max_chars": 800000,
    "denylist_regex": [],
    "allow_non_text": False,

    "allow_images": False,
    "max_image_bytes": 62_914_560,
    "image_chunk_bytes": 49_152,

    "allow_sensitive": False,
    "sensitive_wipe_s": 10.0,
    "direct_password_sync": False,

    "screenshot_backup_dir": "",
    "screenshot_backup_include_devices": True,
    "screenshot_device_subfolders": True,
    "device_screenshot_paths": {},
    "screenshot_exclude_from_history": True,

    "camera_backup": True,
    "camera_backup_dir": "",
    "camera_device_subfolders": False,

    "allow_recopy": True,
    "adopt_on_connect": False,
    "filter_screenshot_previews": True,

    "hub_ip": "",
    "push_port": 8387,
    "dedup_ttl_s": 90.0,
    "reconnect_min_s": 1.0,
    "reconnect_max_s": 30.0,
    "idle_ping_s": 60.0,
    "ack_timeout_s": 4.0,

    "log_level": "INFO",

    "autostart": True,
    "activity_previews": True,
    "tray_left_click": "window",
}

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TRAY_CLICK_MODES = ("window", "flyout")

#: Keys Hub.reload_config can apply to a running daemon. Everything else needs a restart.
HOT_RELOADABLE = frozenset({
    "allow_images", "max_chars", "max_image_bytes", "allow_sensitive", "sensitive_wipe_s",
    "direct_password_sync", "allow_recopy", "adopt_on_connect", "allow_non_text", "filter_screenshot_previews",
    "denylist_regex", "ack_timeout_s",
    "screenshot_backup_dir", "screenshot_backup_include_devices",
    "screenshot_device_subfolders", "device_screenshot_paths",
    "screenshot_exclude_from_history", "camera_backup", "camera_backup_dir",
    "camera_device_subfolders",
    "autostart", "activity_previews", "tray_left_click",
})


# ---------------------------------------------------------------- loading

def migrate(cfg: dict) -> dict:
    """Accept the v1 single-device config (flat `device`/`transport`) unchanged."""
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
    return cfg


def with_defaults(raw: dict) -> dict:
    cfg = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULT_CONFIG.items()}
    cfg.update(raw)
    return migrate(cfg)


def load_config(path: str, *, validate_values: bool = True) -> dict:
    """Read, default, migrate and validate. Raises ConfigError with every problem found."""
    raw: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError([f"{os.path.basename(path)} is not valid JSON: {exc}"]) from exc
        except OSError as exc:
            raise ConfigError([f"cannot read {path}: {exc}"]) from exc
        if not isinstance(loaded, dict):
            raise ConfigError([f"{os.path.basename(path)} must hold a JSON object"])
        raw = loaded
    cfg = with_defaults(raw)
    if validate_values:
        problems = validate(cfg)
        if problems:
            raise ConfigError(problems)
    return cfg


def save_config(path: str, cfg: dict) -> None:
    """Atomic write: temp file in the same directory, fsync, os.replace.

    Unknown and underscore-prefixed keys are preserved, so hand-written notes survive.
    """
    problems = validate(cfg)
    if problems:
        raise ConfigError(problems)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- validation

def _num(cfg: dict, key: str, lo: float, hi: float, problems: list[str], *, integer=False) -> None:
    v = cfg.get(key)
    ok_type = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok_type:
        problems.append(f"{key} must be {'an integer' if integer else 'a number'}")
    elif not (lo <= v <= hi):
        problems.append(f"{key} must be between {lo:g} and {hi:g} (got {v})")


def validate(cfg: dict) -> list[str]:
    """Every problem found, as human-readable strings. Empty means valid."""
    p: list[str] = []

    for key in ("allow_non_text", "allow_images", "allow_sensitive", "direct_password_sync",
                "screenshot_backup_include_devices", "screenshot_device_subfolders",
                "screenshot_exclude_from_history", "camera_backup", "camera_device_subfolders",
                "allow_recopy", "adopt_on_connect", "autostart", "activity_previews", "filter_screenshot_previews"):
        if not isinstance(cfg.get(key), bool):
            p.append(f"{key} must be true or false")

    _num(cfg, "max_chars", 1, 50_000_000, p, integer=True)
    _num(cfg, "max_image_bytes", 1, 1_073_741_824, p, integer=True)
    _num(cfg, "image_chunk_bytes", 1024, 1_048_576, p, integer=True)
    _num(cfg, "sensitive_wipe_s", 1, 3600, p)
    _num(cfg, "push_port", 1024, 65535, p, integer=True)
    _num(cfg, "dedup_ttl_s", 1, 86400, p)
    _num(cfg, "reconnect_min_s", 0.1, 3600, p)
    _num(cfg, "reconnect_max_s", 0.1, 3600, p)
    _num(cfg, "idle_ping_s", 1, 3600, p)
    _num(cfg, "ack_timeout_s", 0.5, 120, p)
    if (isinstance(cfg.get("reconnect_min_s"), (int, float))
            and isinstance(cfg.get("reconnect_max_s"), (int, float))
            and cfg["reconnect_min_s"] > cfg["reconnect_max_s"]):
        p.append("reconnect_min_s must not exceed reconnect_max_s")

    if str(cfg.get("log_level", "")).upper() not in LOG_LEVELS:
        p.append(f"log_level must be one of {', '.join(LOG_LEVELS)}")

    if cfg.get("tray_left_click") not in TRAY_CLICK_MODES:
        p.append(f"tray_left_click must be one of {', '.join(TRAY_CLICK_MODES)}")

    for key in ("adb", "dex", "key_file", "state_dir", "hub_ip",
                "screenshot_backup_dir", "camera_backup_dir"):
        if not isinstance(cfg.get(key), str):
            p.append(f"{key} must be text")

    deny = cfg.get("denylist_regex")
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        p.append("denylist_regex must be a list of patterns")
    else:
        for pattern in deny:
            try:
                re.compile(pattern)
            except re.error as exc:
                p.append(f"denylist_regex pattern {pattern!r} is invalid: {exc}")

    paths = cfg.get("device_screenshot_paths")
    if not isinstance(paths, dict) or not all(isinstance(v, str) for v in paths.values()):
        p.append("device_screenshot_paths must map device names to folders")

    p.extend(_validate_devices(cfg))
    return p


def _validate_devices(cfg: dict) -> list[str]:
    p: list[str] = []
    devices = cfg.get("devices")
    if not isinstance(devices, list):
        return ["devices must be a list"]
    names: list[str] = []
    for i, dev in enumerate(devices):
        label = f"devices[{i}]"
        if not isinstance(dev, dict):
            p.append(f"{label} must be an object")
            continue
        name = dev.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            p.append(f"{label}.name must be 1-32 letters, digits, '-' or '_'")
        else:
            label = f"device {name!r}"
            if name in names:
                p.append(f"duplicate device name {name!r}")
            names.append(name)
            if name == "laptop":
                p.append("device name 'laptop' is reserved for this PC")
        if dev.get("transport", "adb") not in ("adb", "tcp"):
            p.append(f"{label}: transport must be 'adb' or 'tcp'")
        addr = dev.get("device")
        if not isinstance(addr, str) or not addr.strip():
            p.append(f"{label}: device address is required")
        if "enabled" in dev and not isinstance(dev["enabled"], bool):
            p.append(f"{label}: enabled must be true or false")
        if "label" in dev and (not isinstance(dev["label"], str) or len(dev["label"]) > 60):
            p.append(f"{label}: label must be text of at most 60 characters")

    domains = cfg.get("domains")
    if not isinstance(domains, list):
        p.append("domains must be a list")
        return p
    for spec in domains:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            p.append("every domain needs a name")
            continue
        members = spec.get("members")
        if not isinstance(members, list) or not members:
            p.append(f"domain {spec['name']!r} needs a non-empty members list")
            continue
        for m in members:
            if m not in names:
                p.append(f"domain {spec['name']!r} lists unknown device {m!r}")
    return p


# ----------------------------------------------------------- change effect

def classify_changes(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """(applied_live, restart_required) for the keys that differ between two configs."""
    live: list[str] = []
    restart: list[str] = []
    for key in sorted(set(old) | set(new)):
        if key.startswith("_") or old.get(key) == new.get(key):
            continue
        (live if key in HOT_RELOADABLE else restart).append(key)
    return live, restart


# --------------------------------------------------------------- key files

def read_key_file(path: str, *, min_bytes: int = 32) -> bytes:
    """The shared HMAC key as bytes. Raises ConfigError if missing or too short."""
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except OSError as exc:
        raise ConfigError([f"{path} cannot be read: {exc}"]) from exc
    if len(key) < min_bytes:
        raise ConfigError([f"{os.path.basename(path)} is too short; use at least {min_bytes} bytes"])
    return key


def new_key_hex() -> str:
    import secrets
    return secrets.token_hex(32)


# ------------------------------------------------------------ hub address

def detect_tailscale_ip() -> str:
    """This PC's Tailscale IPv4 (100.64.0.0/10), or '' if none is visible."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return ""
    for info in infos:
        ip = info[4][0]
        a, b = (int(x) for x in ip.split(".")[:2])
        if a == 100 and 64 <= b <= 127:
            return ip
    return ""


def resolve_hub_ip(cfg: dict) -> str:
    return cfg.get("hub_ip") or detect_tailscale_ip()
