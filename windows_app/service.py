"""UI-facing logic for the running daemon, with no HTTP and no Tk in it.

The control API (control_api.py) is a thin router over this class, which keeps every rule
testable with plain method calls. Anything that touches the outside world (opening folders,
spawning adb, reading the clipboard, restarting) is injected, so tests never reach a real
device, folder or clipboard.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from core import config as cfgmod
from core.config import ConfigError
from core.models import ClipPayload
from core.version import __version__

log = logging.getLogger("copyclip.service")

LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+)\s+([\w.\-]+): (.*)$")
REPEAT_LINE = re.compile(r"^\(previous message repeated (\d+) more times\)$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
LOG_TAIL_BYTES = 768 * 1024
DEVICE_RE = re.compile(r"\[([\w\-]+)\]")


class ServiceError(Exception):
    """A request the UI should show to the user. `status` maps to the HTTP code."""

    def __init__(self, message: str, status: int = 400, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.problems = problems or []


def parse_log_text(text: str) -> list[dict]:
    """Split a log into entries, attach tracebacks to the line above, and fold repeats."""
    entries: list[dict] = []
    current: dict | None = None   # entry that still accepts traceback lines
    for raw in text.splitlines():
        m = LOG_LINE.match(raw)
        if m:
            ts, level, name, msg = m.groups()
            rep = REPEAT_LINE.match(msg)
            if rep and entries:
                entries[-1]["count"] += int(rep.group(1))
                current = None
                continue
            prev = entries[-1] if entries else None
            if prev and (prev["level"], prev["name"], prev["message"]) == (level, name, msg):
                # Same header again: count it and drop its repeated traceback.
                prev["count"] += 1
                prev["ts_last"] = ts
                current = None
                continue
            current = {"ts": ts, "ts_last": ts, "level": level, "name": name,
                       "message": msg, "detail": "", "count": 1}
            entries.append(current)
        elif current is not None and raw.strip():
            current["detail"] += raw + "\n"
    return entries


class ControlService:
    def __init__(self, hub, *, config_path: str, logs_dir: str, key_path: str,
                 push_listener=None, root_dir: str | None = None,
                 open_path: Callable[[str], None] | None = None,
                 run_cmd: Callable[..., Any] | None = None,
                 read_clipboard: Callable[[], str | None] | None = None,
                 restart: Callable[[], None] | None = None,
                 rollover_logs: Callable[[], None] | None = None,
                 pick_folder: Callable[[str], str | None] | None = None,
                 open_window: Callable[[str], None] | None = None,
                 quit_app: Callable[[], None] | None = None) -> None:
        self.hub = hub
        self.config_path = config_path
        self.logs_dir = logs_dir
        self.key_path = key_path
        self.push_listener = push_listener
        self.root_dir = root_dir or os.path.dirname(config_path)
        self._open_path = open_path or self._default_open
        self._run = run_cmd or subprocess.run
        self._read_clipboard = read_clipboard
        self._restart = restart
        self._rollover_logs = rollover_logs
        self._pick_folder = pick_folder
        self._open_window = open_window
        self._quit = quit_app
        self._pause_timer: threading.Timer | None = None
        self.paused_until: float | None = None
        self._ui_last_ping = 0.0
        self._nav = {"route": "", "id": 0}
        self._cfg_lock = threading.Lock()
        # Settings that are applied by something other than Hub.reload_config. The tray adds
        # "autostart"; the others are wired here because the service owns the objects.
        self.side_effects: dict[str, Callable[[Any], None]] = {
            "activity_previews": lambda v: self.hub.activity.set_previews(bool(v)),
            "tray_left_click": lambda v: None,   # read from the config file on each click
        }

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        s = self.hub.status()
        cfg = self._cfg_or_defaults()
        by_name = {d.get("name"): d for d in cfg.get("devices", [])}
        for name, info in s["nodes"].items():
            dev = by_name.get(name, {})
            info["kind"] = "this-pc" if name == "laptop" else "device"
            info["address"] = dev.get("device", "")
            info["transport"] = dev.get("transport", "adb")
            info["label"] = dev.get("label") or ("This PC" if name == "laptop" else name)
            info["enabled"] = dev.get("enabled", True)
        s["outbox_targets"] = [k.partition(":")[2] for k in s.get("outbox", [])]
        s["paused_until"] = self.paused_until
        s["version"] = __version__
        s["pid"] = os.getpid()
        s["ui_connected"] = (time.time() - self._ui_last_ping) < 15
        s["nav"] = dict(self._nav)
        s["server_time"] = time.time()
        return s

    def ping(self) -> dict:
        self._ui_last_ping = time.time()
        return {"ok": True}

    # ------------------------------------------------------------- activity

    def activity(self, *, limit: int = 100, after: int = 0, kind: str | None = None,
                 status: str | None = None, query: str | None = None) -> dict:
        sens = True if kind == "sensitive" else None
        if kind == "sensitive":
            kind = None
        return {"events": self.hub.activity.list(limit=limit, after=after, kind=kind,
                                                 status=status, query=query, sensitive=sens),
                "total": len(self.hub.activity)}

    IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

    def _activity_file(self, event_id: int) -> tuple[str, str]:
        path = self.hub.activity.get_file(event_id)
        mime = self.IMAGE_TYPES.get(os.path.splitext(path or "")[1].lower())
        if not path or not mime or not os.path.isfile(path):
            raise ServiceError("that picture is no longer available", 404)
        return path, mime

    def activity_image(self, event_id: int) -> tuple[bytes, str]:
        path, mime = self._activity_file(event_id)
        with open(path, "rb") as fh:
            return fh.read(), mime

    def copy_activity(self, event_id: int) -> dict:
        """Copy what an Activity row holds, the picture or the full text, back onto this PC's clipboard."""
        if self.hub.activity.get_file(event_id):
            return self.copy_activity_image(event_id)
        text = self.hub.activity.get_text(event_id)
        laptop = self.hub.nodes.get("laptop")
        copy = getattr(laptop, "copy_text_to_clipboard", None)
        if not text:
            raise ServiceError("that clip is no longer available to copy", 404)
        if copy is None:
            raise ServiceError("clipboard access is not available in this mode", 501)
        copy(text)
        return {"copied": True, "chars": len(text)}

    def copy_activity_image(self, event_id: int) -> dict:
        path, mime = self._activity_file(event_id)
        laptop = self.hub.nodes.get("laptop")
        copy = getattr(laptop, "copy_file_to_clipboard", None)
        if copy is None:
            raise ServiceError("clipboard access is not available in this mode", 501)
        copy(path, mime)
        return {"copied": True}

    def clear_activity(self) -> dict:
        self.hub.activity.clear()
        return {"ok": True}

    # ----------------------------------------------------------------- logs

    def logs(self, *, level: str = "ALL", device: str | None = None, query: str | None = None,
             limit: int = 400) -> dict:
        path = os.path.join(self.logs_dir, "copyclip.log")
        text = ""
        size = 0
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > LOG_TAIL_BYTES:
                    fh.seek(size - LOG_TAIL_BYTES)
                    fh.readline()  # drop the partial first line
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            pass
        entries = parse_log_text(text)
        total_lines = sum(e["count"] for e in entries)
        floor = LEVELS.get(level.upper(), 0)
        q = (query or "").lower()
        out = []
        for e in entries:
            if LEVELS.get(e["level"], 20) < floor:
                continue
            if device:
                m = DEVICE_RE.search(e["message"])
                if not m or m.group(1) != device:
                    continue
            if q and q not in f"{e['message']} {e['name']} {e['detail']}".lower():
                continue
            out.append(e)
        return {"entries": out[-limit:], "matched": len(out), "total_lines": total_lines,
                "folded_groups": sum(1 for e in entries if e["count"] > 1), "size": size}

    def clear_logs(self) -> dict:
        if self._rollover_logs is None:
            raise ServiceError("log rotation is not available in this mode", 501)
        self._rollover_logs()
        return {"ok": True}

    # ---------------------------------------------------------------- config

    def _cfg_or_defaults(self) -> dict:
        try:
            return cfgmod.load_config(self.config_path, validate_values=False)
        except ConfigError:
            return cfgmod.with_defaults({})

    def get_config(self) -> dict:
        cfg = self._cfg_or_defaults()
        return {"config": cfg, "restart_keys": sorted(
            k for k in cfgmod.DEFAULT_CONFIG if k not in cfgmod.HOT_RELOADABLE),
            "defaults": cfgmod.DEFAULT_CONFIG}

    def put_config(self, patch: dict) -> dict:
        """Merge `patch` into the saved config, validate, save atomically, hot-apply."""
        if not isinstance(patch, dict) or not patch:
            raise ServiceError("send a JSON object with the settings to change")
        with self._cfg_lock:
            old = self._cfg_or_defaults()
            new = dict(old)
            new.update({k: v for k, v in patch.items() if not k.startswith("_")})
            problems = cfgmod.validate(new)
            if problems:
                raise ServiceError("some settings are not valid", 422, problems)
            live, restart = cfgmod.classify_changes(old, new)
            if not live and not restart:
                return {"saved": False, "applied": [], "restart_required": []}
            cfgmod.save_config(self.config_path, new)
            applied = self.hub.reload_config({k: new[k] for k in live}) if live else []
            for key in live:
                effect = self.side_effects.get(key)
                if effect is not None and key not in applied:
                    try:
                        effect(new[key])
                        applied.append(key)
                    except Exception:
                        log.exception("applying %s failed", key)
                        restart.append(key)
        return {"saved": True, "applied": applied, "restart_required": restart}

    # ---------------------------------------------------------------- pausing

    def set_paused(self, paused: bool, minutes: float | None = None) -> dict:
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        self.paused_until = None
        self.hub.paused = bool(paused)
        if paused and minutes:
            if not (0 < float(minutes) <= 24 * 60):
                raise ServiceError("minutes must be between 0 and 1440")
            self.paused_until = time.time() + float(minutes) * 60
            self._pause_timer = threading.Timer(float(minutes) * 60, self._auto_resume)
            self._pause_timer.daemon = True
            self._pause_timer.start()
        log.info("sync %s%s", "paused" if paused else "resumed",
                 f" for {minutes:g} min" if paused and minutes else "")
        return {"paused": self.hub.paused, "paused_until": self.paused_until}

    def _auto_resume(self) -> None:
        self.hub.paused = False
        self.paused_until = None
        self._pause_timer = None
        log.info("sync resumed (pause timer elapsed)")

    def device_pause(self, name: str, paused: bool) -> dict:
        self._known_node(name)
        (self.hub.pause_device if paused else self.hub.resume_device)(name)
        return {"name": name, "paused": paused}

    def _known_node(self, name: str) -> None:
        if name not in self.hub.nodes or name == "laptop":
            raise ServiceError(f"unknown device {name!r}", 404)

    # ----------------------------------------------------------------- devices

    def reconnect(self, name: str | None = None) -> dict:
        """Best-effort `adb connect` for one device or all; the supervisor does the rest."""
        cfg = self._cfg_or_defaults()
        targets = [d for d in cfg["devices"] if d.get("enabled", True)
                   and d.get("transport", "adb") == "adb" and (name is None or d["name"] == name)]
        if name is not None and not targets:
            raise ServiceError(f"no enabled adb device named {name!r}", 404)

        def go():
            for d in targets:
                try:
                    self._run([cfg["adb"], "connect", d["device"]], capture_output=True,
                              timeout=8, creationflags=_no_window())
                except Exception as exc:
                    log.warning("reconnect %s failed: %s", d["name"], exc)
        threading.Thread(target=go, name="copyclip-reconnect", daemon=True).start()
        return {"started": [d["name"] for d in targets]}

    def test_device(self, address: str, transport: str = "adb") -> dict:
        address = (address or "").strip()
        if not address:
            raise ServiceError("enter an address first")
        cfg = self._cfg_or_defaults()
        steps: list[dict] = []

        def step(label: str, fn) -> bool:
            t0 = time.time()
            try:
                ok, detail = fn()
            except Exception as exc:
                ok, detail = False, str(exc)
            steps.append({"step": label, "ok": bool(ok), "detail": detail,
                          "ms": int((time.time() - t0) * 1000)})
            return bool(ok)

        if transport == "tcp":
            host, _, port = address.partition(":")

            def tcp():
                with socket.create_connection((host, int(port or 8385)), timeout=3):
                    return True, "port open"
            step("Port reachable", tcp)
            return {"ok": all(s["ok"] for s in steps), "steps": steps}

        def connect():
            r = self._run([cfg["adb"], "connect", address], capture_output=True, text=True,
                          timeout=8, creationflags=_no_window())
            blob = (r.stdout + r.stderr).strip()
            return "connected" in blob.lower(), blob or "no output"

        def state():
            r = self._run([cfg["adb"], "-s", address, "get-state"], capture_output=True,
                          text=True, timeout=6, creationflags=_no_window())
            out = (r.stdout or r.stderr).strip()
            return out == "device", out or "no output"
        if step("ADB reachable", connect):
            step("Device authorised and online", state)
        return {"ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps}

    def add_device(self, dev: dict, group: str | None = None) -> dict:
        return self._edit_devices(lambda devices: devices.append(self._clean_device(dev)),
                                  dev.get("name"), group)

    def update_device(self, name: str, dev: dict, group: str | None = None) -> dict:
        def edit(devices):
            for i, d in enumerate(devices):
                if d.get("name") == name:
                    merged = dict(d)
                    merged.update(self._clean_device(dev, partial=True))
                    devices[i] = merged
                    return
            raise ServiceError(f"unknown device {name!r}", 404)
        return self._edit_devices(edit, dev.get("name", name), group, old_name=name)

    def delete_device(self, name: str) -> dict:
        def edit(devices):
            before = len(devices)
            devices[:] = [d for d in devices if d.get("name") != name]
            if len(devices) == before:
                raise ServiceError(f"unknown device {name!r}", 404)
        return self._edit_devices(edit, None, None, old_name=name, removing=True)

    @staticmethod
    def _clean_device(dev: dict, partial: bool = False) -> dict:
        allowed = ("name", "device", "transport", "enabled", "label", "tcp_host", "tcp_port")
        out = {k: v for k, v in dev.items() if k in allowed}
        if isinstance(out.get("name"), str):
            out["name"] = out["name"].strip()
        if isinstance(out.get("device"), str):
            out["device"] = out["device"].strip()
        if not partial:
            out.setdefault("transport", "adb")
            out.setdefault("enabled", True)
        return out

    def _edit_devices(self, mutate, new_name, group, *, old_name=None, removing=False) -> dict:
        with self._cfg_lock:
            old = self._cfg_or_defaults()
            new = dict(old)
            new["devices"] = [dict(d) for d in old["devices"]]
            new["domains"] = [dict(d, members=list(d.get("members", []))) for d in old["domains"]]
            mutate(new["devices"])
            final = new_name or old_name
            if old_name and new_name and old_name != new_name:
                for dom in new["domains"]:
                    dom["members"] = [new_name if m == old_name else m for m in dom["members"]]
            if removing:
                for dom in new["domains"]:
                    dom["members"] = [m for m in dom["members"] if m != old_name]
                new["domains"] = [d for d in new["domains"] if d["members"]]
            elif group is not None:
                self._apply_group(new, final, group)
            problems = cfgmod.validate(new)
            if problems:
                raise ServiceError("that device is not valid", 422, problems)
            cfgmod.save_config(self.config_path, new)
        return {"saved": True, "restart_required": ["devices"]}

    @staticmethod
    def _apply_group(cfg: dict, name: str, group: str) -> None:
        for dom in cfg["domains"]:
            dom["members"] = [m for m in dom["members"] if m != name]
        cfg["domains"] = [d for d in cfg["domains"] if d["members"]]
        if group and group != "none":
            for dom in cfg["domains"]:
                if dom["name"] == group:
                    dom["members"].append(name)
                    return
            cfg["domains"].append({"name": group, "members": [name], "grace_s": 8.0,
                                   "fail_threshold": 3})

    # -------------------------------------------------------------- key / misc

    def key_info(self, reveal: bool = False) -> dict:
        try:
            age_days = int((time.time() - os.path.getmtime(self.key_path)) / 86400)
            exists = True
        except OSError:
            age_days, exists = None, False
        out = {"exists": exists, "age_days": age_days}
        if reveal:
            try:
                out["key"] = cfgmod.read_key_file(self.key_path).decode("ascii", "replace")
            except ConfigError as exc:
                raise ServiceError(str(exc), 404) from exc
        return out

    def rotate_key(self) -> dict:
        new_hex = cfgmod.new_key_hex()
        directory = os.path.dirname(os.path.abspath(self.key_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".key-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_hex)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.key_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        pushed = self.hub.apply_key(new_hex)
        if self.push_listener is not None:
            self.push_listener.key = new_hex.encode("utf-8")
        log.info("shared key rotated; %d connected device(s) updated", len(pushed))
        return {"rotated": True, "updated_devices": pushed}

    def open_folder(self, which: str) -> dict:
        cfg = self._cfg_or_defaults()
        base = cfg["screenshot_backup_dir"] or os.path.join(
            os.path.expanduser("~"), "Pictures", "Screenshots", "Temporary Screenshots")
        if which == "screenshots":
            target = base
        elif which == "camera":
            target = cfg["camera_backup_dir"] or os.path.join(base, "Camera")
        elif which == "logs":
            target = self.logs_dir
        elif which.startswith("device:"):
            name = which.split(":", 1)[1]
            custom = (cfg["device_screenshot_paths"].get(name) or "").strip()
            target = custom or (os.path.join(base, name) if cfg["screenshot_device_subfolders"] else base)
        else:
            raise ServiceError(f"unknown folder {which!r}", 404)
        try:
            os.makedirs(target, exist_ok=True)
            self._open_path(target)
        except OSError as exc:
            raise ServiceError(f"could not open {target}: {exc}", 500) from exc
        return {"opened": target}

    def send_clipboard(self) -> dict:
        if self._read_clipboard is None:
            raise ServiceError("clipboard access is not available in this mode", 501)
        text = self._read_clipboard()
        if not text:
            raise ServiceError("the clipboard holds no text to send")
        self.hub.on_clip(ClipPayload.from_text(text, "laptop"))
        return {"sent": len(text)}

    def pick_folder(self, initial: str = "") -> dict:
        if self._pick_folder is None:
            raise ServiceError("folder picking is not available in this mode", 501)
        path = self._pick_folder(initial or "")
        return {"path": path or ""}

    SCREENS = ("overview", "devices", "activity", "logs", "settings")

    def navigate(self, route: str) -> dict:
        """Ask an already-open window to show `route`. The window polls status and follows."""
        if route not in self.SCREENS:
            raise ServiceError(f"unknown screen {route!r}", 404)
        self._nav = {"route": route, "id": self._nav["id"] + 1}
        return {"route": route}

    def open_window(self, route: str = "overview") -> dict:
        if route not in self.SCREENS:
            raise ServiceError(f"unknown screen {route!r}", 404)
        if self._open_window is None:
            raise ServiceError("opening windows is not available in this mode", 501)
        self._open_window(route)
        return {"opened": route}

    def quit(self) -> dict:
        if self._quit is None:
            raise ServiceError("quit is not available in this mode", 501)
        threading.Timer(0.3, self._quit).start()
        return {"quitting": True}

    def restart(self) -> dict:
        if self._restart is None:
            raise ServiceError("restart is not available in this mode", 501)
        threading.Timer(0.3, self._restart).start()
        return {"restarting": True}

    @staticmethod
    def _default_open(path: str) -> None:
        os.startfile(path)  # type: ignore[attr-defined]


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
