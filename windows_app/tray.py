r"""CopyClip tray icon: wraps the existing, unchanged daemon in a Windows system-tray app.

Zero third-party dependencies, same as the rest of this project. The tray icon is built from
raw ctypes calls to the Win32 API (Shell_NotifyIconW), reusing the exact hidden-window and
message-loop pattern winclip.py's ClipboardWatcher already established and proved out in this
codebase (same WNDCLASSW/WNDPROCTYPE/user32/kernel32 setup, same explicit argtypes/restype
discipline -- see winclip.py's own comment on why that matters on 64-bit Python).

core/hub.py, nodes/, transports/ are untouched beyond one additive `paused` flag Hub already
exposes for exactly this menu. This file only adds a UI shell around copyclip.py's own
load_config/setup_logging/build_hub -- the daemon itself does not change.

Replaces the AHK-triggered launch: this registers itself in HKCU's Run key on startup, so it
comes back after login without AutoHotkey.

Run:
    python windows_app\tray.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import winclip  # noqa: E402
from copyclip import build_hub, build_push_listener, load_config, setup_logging  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core.single_instance import AlreadyRunning, SingleInstance  # noqa: E402
from windows_app import launcher  # noqa: E402
from windows_app.control_api import ControlServer  # noqa: E402
from windows_app.service import ControlService  # noqa: E402

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.mohitarora.copyclip")
except Exception:
    pass

log = logging.getLogger("copyclip.tray")

user32 = winclip.user32
kernel32 = winclip.kernel32
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
LRESULT = winclip.LRESULT
WNDPROCTYPE = winclip.WNDPROCTYPE
WNDCLASSW = winclip.WNDCLASSW

# -- constants ----------------------------------------------------------------------------
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_NULL = 0x0000
WM_APP = 0x8000
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
WMAPP_TRAY = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NOTIFYICON_VERSION_4 = 4

MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_POPUP = 0x0, 0x1, 0x800, 0x0010
TPM_RIGHTBUTTON = 0x0002

IDM_PAUSE, IDM_CONFIG, IDM_LOGS, IDM_QUIT, IDM_DASHBOARD = 1001, 1002, 1003, 1004, 1005
IDM_PAUSE_15, IDM_PAUSE_60, IDM_SEND, IDM_SHOTS = 1006, 1007, 1008, 1009
IDM_DEVICE_BASE = 2000  # informational device-status labels start here
IDM_DEVICE_PAUSE_BASE = 3000  # per-device pause/resume actions start here

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copyclip.ico")


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# Handle-returning calls winclip.py doesn't already declare. Every one needs an explicit
# restype/argtypes or ctypes truncates a pointer-sized return/argument to a 32-bit int on
# 64-bit Python -- see winclip.py's own comment on this exact failure mode (it hit it first).
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# Explorer restarting (crash, "Restart Windows Explorer") silently drops every tray icon
# that was registered before it came back. Re-adding on this broadcast is the documented
# fix (Microsoft's own NotificationIcon.cpp sample does the same thing).
WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")


def _enable_dark_mode(hwnd: int) -> None:
    """Enables Windows 10/11 dark mode for the window and its native popup menus."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass

    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        set_app_mode = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)((135, uxtheme))
        set_app_mode(2)  # ForceDark
        flush_menu = ctypes.WINFUNCTYPE(None)((136, uxtheme))
        flush_menu()
    except Exception:
        pass


class TrayApp:
    def __init__(self, hub, cfg: dict, service: ControlService, server: ControlServer | None,
                 legacy_ui: bool = False) -> None:
        self.hub = hub
        self.cfg = cfg
        self.service = service
        self.server = server
        self.legacy_ui = legacy_ui
        self._last_click = -10.0   # monotonic time of the last accepted tray click
        self._hwnd = None
        self._wndproc_ref = None  # must outlive the window, same rule as winclip.py's watcher
        self._nid = NOTIFYICONDATAW()

    def run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROCTYPE(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "CopyClipTray"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS -- harmless across a re-run
                raise OSError(f"RegisterClassW failed (err={err})")

        self._hwnd = user32.CreateWindowExW(0, "CopyClipTray", "CopyClip", 0, 0, 0, 0, 0,
                                            None, None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")

        _enable_dark_mode(self._hwnd)
        self._add_icon()
        user32.SetTimer(self._hwnd, 1, 5000, None)

        msg = wintypes.MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # -- icon / tooltip ----------------------------------------------------

    def _device_status(self) -> dict:
        # "laptop" is always present and always trivially "available"; excluded here to
        # match copyclip.py's own cmd_status(), which counts only the Android devices.
        return {k: v for k, v in self.hub.status()["nodes"].items() if k != "laptop"}

    def _load_app_icon(self):
        if os.path.isfile(ICON_PATH):
            handle = user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        # copyclip.ico missing or failed to load (e.g. generate_icon.py never run) --
        # fall back rather than leave the tray icon blank.
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _add_icon(self) -> None:
        nid = self._nid
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = WMAPP_TRAY
        nid.hIcon = self._load_app_icon()
        self._set_tip()
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    def _set_tip(self) -> None:
        devices = self._device_status()
        n_up = sum(1 for s in devices.values() if s["available"] and not s.get("paused"))
        state = "paused" if self.hub.paused else f"{n_up}/{len(devices)} connected"
        self._nid.szTip = f"CopyClip: {state}"

    def _refresh_tip(self) -> None:
        self._set_tip()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    # -- menu ----------------------------------------------------------------

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_DASHBOARD, "Open CopyClip")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        if self.hub.paused:
            user32.AppendMenuW(menu, MF_STRING, IDM_PAUSE, "Resume sync")
        else:
            pause_menu = user32.CreatePopupMenu()
            user32.AppendMenuW(pause_menu, MF_STRING, IDM_PAUSE_15, "For 15 minutes")
            user32.AppendMenuW(pause_menu, MF_STRING, IDM_PAUSE_60, "For 1 hour")
            user32.AppendMenuW(pause_menu, MF_STRING, IDM_PAUSE, "Until I resume")
            user32.AppendMenuW(menu, MF_POPUP, pause_menu, "Pause sync")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        for i, (name, s) in enumerate(self._device_status().items()):
            state = "connected" if s["available"] else "unreachable"
            if s.get("locked"):
                state += ", locked"
            if s.get("paused"):
                state += " (paused)"

            sub = user32.CreatePopupMenu()
            p_label = "Resume sync" if s.get("paused") else "Pause sync"
            user32.AppendMenuW(sub, MF_STRING, IDM_DEVICE_PAUSE_BASE + i, p_label)
            user32.AppendMenuW(menu, MF_POPUP, sub, f"{name}: {state}")

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_SEND, "Send current clipboard to devices")
        user32.AppendMenuW(menu, MF_STRING, IDM_SHOTS, "Open screenshots folder")
        user32.AppendMenuW(menu, MF_STRING, IDM_CONFIG, "Settings")
        user32.AppendMenuW(menu, MF_STRING, IDM_LOGS, "View logs")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "Quit")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Must be foreground before TrackPopupMenu, and poked with WM_NULL right after, or
        # the menu does not dismiss when the user clicks away -- a long-documented Win32
        # quirk (Microsoft's own NotificationIcon.cpp sample does this exact two-step dance).
        user32.SetForegroundWindow(self._hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

    # -- commands --------------------------------------------------------------

    def open_ui(self, route: str = "overview", flyout: bool = False) -> None:
        """Show the CopyClip window. The window is a separate process, so a failure here can
        never touch clipboard sync."""
        try:
            if self.legacy_ui:
                import dashboard
                dashboard.open_dashboard(self.hub, self.cfg, initial_tab=1 if route == "settings" else 0)
            else:
                if not flyout:
                    self.service.navigate(route)   # an already-open window switches screen
                launcher.open_ui(self.server, route, flyout=flyout)
        except Exception:
            log.exception("could not open the CopyClip window")

    #: One physical click can reach us as both WM_LBUTTONUP and NIN_SELECT, and people click
    #: twice when nothing seems to happen. Anything inside this window is the same click.
    CLICK_DEBOUNCE_S = 0.6

    def _on_tray_click(self) -> None:
        now = time.monotonic()
        if now - self._last_click < self.CLICK_DEBOUNCE_S:
            return
        self._last_click = now
        try:
            mode = cfgmod.load_config(os.path.join(HERE, "config.json"), validate_values=False)["tray_left_click"]
        except Exception:
            mode = "window"
        self.open_ui(flyout=(mode == "flyout" and not self.legacy_ui))

    def quit(self) -> None:
        """Thread-safe: asks the tray window to run its normal Quit path."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_COMMAND, IDM_QUIT, 0)

    def _handle_command(self, cmd: int) -> None:
        svc = self.service
        if cmd == IDM_DASHBOARD:
            self.open_ui()
        elif cmd == IDM_QUIT:
            user32.DestroyWindow(self._hwnd)
        elif cmd == IDM_PAUSE:
            svc.set_paused(not self.hub.paused)
            self._refresh_tip()
        elif cmd in (IDM_PAUSE_15, IDM_PAUSE_60):
            svc.set_paused(True, minutes=15 if cmd == IDM_PAUSE_15 else 60)
            self._refresh_tip()
        elif IDM_DEVICE_PAUSE_BASE <= cmd < IDM_DEVICE_PAUSE_BASE + 100:
            idx = cmd - IDM_DEVICE_PAUSE_BASE
            devices = list(self._device_status().keys())
            if idx < len(devices):
                name = devices[idx]
                svc.device_pause(name, not self.hub.is_device_paused(name))
                self._refresh_tip()
        elif cmd == IDM_CONFIG:
            self.open_ui("settings")
        elif cmd == IDM_LOGS:
            self.open_ui("logs")
        elif cmd == IDM_SEND:
            threading.Thread(target=self._send_clipboard, name="copyclip-send", daemon=True).start()
        elif cmd == IDM_SHOTS:
            try:
                svc.open_folder("screenshots")
            except Exception:
                log.exception("could not open the screenshots folder")

    def _send_clipboard(self) -> None:
        try:
            self.service.send_clipboard()
        except Exception as exc:
            log.warning("send clipboard from tray failed: %s", exc)

    # -- window procedure --------------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TASKBARCREATED:
            self._add_icon()
        elif msg == WMAPP_TRAY:
            lp = lparam & 0xFFFF
            if lp in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            elif lp in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
                self._on_tray_click()
            elif lp == WM_LBUTTONDBLCLK:
                self.open_ui()
        elif msg == WM_TIMER:
            self._refresh_tip()
        elif msg == WM_COMMAND:
            self._handle_command(wparam & 0xFFFF)
        elif msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            user32.PostQuitMessage(0)
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0


def _windowless_python() -> str:
    """pythonw.exe next to sys.executable if it exists, else sys.executable unchanged.

    python.exe is the console-subsystem build; running the tray app under it leaves a
    console window (or at least a taskbar-invisible but Alt-Tab-visible console) around for
    no reason. pythonw.exe is the windowless sibling every standard CPython Windows install
    ships alongside it -- the same reason AllScripts\\BackgroundAutomations.ahk's own
    RestartNamedPythonServer() defaults PythonExe to "pythonw.exe".
    """
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def register_autostart() -> None:
    """Adds CopyClip to HKCU's Run key so it starts at login -- replaces the old AHK trigger
    (AllScripts\\BackgroundAutomations.ahk's RestartNamedPythonServer() call, outside this
    repo, now itself repointed at this file -- see AllScripts\\local_paths.ahk's
    WATCHDOG_APPS entry for the always-running crash-recovery side of that).

    HKCU, not HKLM: no admin rights needed. Idempotent: safe to call on every startup. Same
    mainstream pattern Discord/Slack/Spotify use for their own "open at login" toggle.
    """
    import winreg

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{_windowless_python()}" "{os.path.abspath(__file__)}"'

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "CopyClip", 0, winreg.REG_SZ, command)
    log.info("autostart registered: %s", command)


def unregister_autostart() -> None:
    """Removes the HKCU Run entry. Idempotent: a missing entry is not an error."""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "CopyClip")
        log.info("autostart removed")
    except FileNotFoundError:
        pass


def _write_control_file(path: str, server: ControlServer) -> None:
    """Lets `copyclip.py --status` find the running daemon's API. Same-user readable only."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"port": server.port, "token": server.token, "pid": os.getpid()}, fh)


def _restart_self() -> None:
    """Start a replacement tray (which takes over from this one) and let it open the window."""
    subprocess.Popen([_windowless_python(), os.path.abspath(__file__), "--replace", "--open-ui"],
                     close_fds=True, cwd=HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="CopyClip tray app")
    ap.add_argument("--replace", action="store_true",
                    help="stop an already-running CopyClip and take over")
    ap.add_argument("--open-ui", action="store_true", help="open the window once started")
    ap.add_argument("--legacy-ui", action="store_true", help="use the old Tk dashboard")
    args = ap.parse_args()

    config_path = os.path.join(HERE, "config.json")
    try:
        cfg = load_config(config_path)
    except cfgmod.ConfigError as exc:
        # No console under pythonw, so say it where the user will look.
        message = "config.json has problems:\n  - " + "\n  - ".join(exc.problems)
        ctypes.windll.user32.MessageBoxW(None, message, "CopyClip cannot start", 0x10)
        return 2
    setup_logging(cfg["log_level"])

    # Same guard copyclip.py's own main() uses (BUG-003): refuse to run twice rather than
    # duplicate every write to every device.
    guard = SingleInstance(os.path.join(HERE, cfg["state_dir"], "copyclip.pid"))
    try:
        guard.acquire(replace=args.replace)
    except AlreadyRunning:
        log.debug("CopyClip instance is already active; yielding cleanly.")
        return 0

    from core.logsetup import install_crash_diagnostics
    install_crash_diagnostics(os.path.join(HERE, "logs"), os.path.join(HERE, cfg["state_dir"]),
                              replaced_old=bool(args.replace))

    try:
        if cfg.get("autostart", True):
            register_autostart()
        else:
            unregister_autostart()
    except OSError:
        log.exception("could not update autostart (non-fatal, sync still runs this session)")

    hub = build_hub(cfg)
    hub.activity.set_previews(cfg.get("activity_previews", True))
    push_listener = build_push_listener(cfg, hub)
    hub.start()
    if push_listener is not None:
        push_listener.start()
    log.info("CopyClip tray starting (pid %d, devices: %s)", os.getpid(),
             ", ".join(d["name"] for d in cfg["devices"] if d.get("enabled", True)))

    app_ref: dict[str, TrayApp] = {}
    logs_dir = os.path.join(HERE, "logs")
    service = ControlService(
        hub, config_path=config_path, logs_dir=logs_dir,
        key_path=os.path.join(HERE, cfg["key_file"]), push_listener=push_listener, root_dir=HERE,
        read_clipboard=winclip.read_text, restart=_restart_self, pick_folder=launcher.pick_folder,
        rollover_logs=_rollover_logs,
        open_window=lambda route: app_ref["app"].open_ui(route),
        quit_app=lambda: app_ref["app"].quit())
    service.side_effects["autostart"] = lambda v: register_autostart() if v else unregister_autostart()

    server = None
    control_file = os.path.join(HERE, cfg["state_dir"], "control.json")
    try:
        server = ControlServer(service)
        server.start()
        _write_control_file(control_file, server)
    except OSError:
        log.exception("control API could not start; the window and --status are unavailable")

    app = TrayApp(hub, cfg, service, server, legacy_ui=args.legacy_ui or server is None)
    app_ref["app"] = app
    if args.open_ui:
        threading.Timer(1.0, app.open_ui).start()
    try:
        app.run()
    finally:
        if server is not None:
            server.stop()
        try:
            os.unlink(control_file)
        except OSError:
            pass
        if push_listener is not None:
            push_listener.stop()
        hub.stop()
        guard.release()
    return 0


def _rollover_logs() -> None:
    """Archive the current log and start a fresh one, safely, through the active handler."""
    import logging.handlers
    for handler in logging.getLogger().handlers:
        inner = getattr(handler, "inner", handler)
        if isinstance(inner, logging.handlers.RotatingFileHandler):
            inner.doRollover()


if __name__ == "__main__":
    sys.exit(main())
