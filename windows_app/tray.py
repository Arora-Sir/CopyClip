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
import logging
import os
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import winclip  # noqa: E402
from copyclip import build_hub, build_push_listener, load_config, setup_logging  # noqa: E402
from core.single_instance import AlreadyRunning, SingleInstance  # noqa: E402

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
WM_RBUTTONUP = 0x0205
WMAPP_TRAY = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NOTIFYICON_VERSION_4 = 4

MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON = 0x0002

IDM_PAUSE, IDM_CONFIG, IDM_LOGS, IDM_QUIT, IDM_DASHBOARD = 1001, 1002, 1003, 1004, 1005
IDM_DEVICE_BASE = 2000  # disabled, informational device-status labels start here

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
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_uint, wintypes.LPCWSTR]
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


class TrayApp:
    def __init__(self, hub, cfg: dict) -> None:
        self.hub = hub
        self.cfg = cfg
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
        n_up = sum(1 for s in devices.values() if s["available"])
        state = "paused" if self.hub.paused else f"{n_up}/{len(devices)} connected"
        self._nid.szTip = f"CopyClip: {state}"

    def _refresh_tip(self) -> None:
        self._set_tip()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    # -- menu ----------------------------------------------------------------

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_DASHBOARD, "Open dashboard")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        pause_label = "Resume sync" if self.hub.paused else "Pause sync"
        user32.AppendMenuW(menu, MF_STRING, IDM_PAUSE, pause_label)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for i, (name, s) in enumerate(self._device_status().items()):
            state = "connected" if s["available"] else "unreachable"
            if s.get("locked"):
                state += ", locked"
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, IDM_DEVICE_BASE + i,
                              f"{name}: {state}")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_CONFIG, "Open config")
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

    def _handle_command(self, cmd: int) -> None:
        if cmd == IDM_DASHBOARD:
            import dashboard
            dashboard.open_dashboard(self.hub)
        elif cmd == IDM_QUIT:
            user32.DestroyWindow(self._hwnd)
        elif cmd == IDM_PAUSE:
            self.hub.paused = not self.hub.paused
            log.info("sync %s from tray menu", "paused" if self.hub.paused else "resumed")
            self._refresh_tip()
        elif cmd == IDM_CONFIG:
            os.startfile(os.path.join(HERE, "config.json"))
        elif cmd == IDM_LOGS:
            os.startfile(os.path.join(HERE, "logs", "copyclip.log"))

    # -- window procedure --------------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TASKBARCREATED:
            self._add_icon()
        elif msg == WMAPP_TRAY:
            if (lparam & 0xFFFF) in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
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


def main() -> int:
    ap = argparse.ArgumentParser(description="CopyClip tray app")
    ap.add_argument("--replace", action="store_true",
                    help="stop an already-running CopyClip and take over")
    args = ap.parse_args()

    cfg = load_config(os.path.join(HERE, "config.json"))
    setup_logging(cfg["log_level"])

    # Same guard copyclip.py's own main() uses (BUG-003): refuse to run twice rather than
    # duplicate every write to every device.
    guard = SingleInstance(os.path.join(HERE, cfg["state_dir"], "copyclip.pid"))
    try:
        guard.acquire(replace=args.replace)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 2

    try:
        register_autostart()
    except OSError:
        log.exception("could not register autostart (non-fatal, sync still runs this session)")

    hub = build_hub(cfg)
    push_listener = build_push_listener(cfg, hub)
    hub.start()
    if push_listener is not None:
        push_listener.start()
    log.info("CopyClip tray starting (pid %d, devices: %s)", os.getpid(),
             ", ".join(d["name"] for d in cfg["devices"] if d.get("enabled", True)))

    try:
        TrayApp(hub, cfg).run()
    finally:
        if push_listener is not None:
            push_listener.stop()
        hub.stop()
        guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
