"""Shows the CopyClip window as a standalone app window, exactly once.

The window is a chromeless app window of a Chromium browser the person already has (their
default one first, then Edge, Brave, Chrome). It runs in its own process with its own profile
directory, so it has its own taskbar entry and icon, costs nothing while closed, and if it
crashes the clipboard sync in the tray process is untouched. This adds no dependency.

Single instance, by construction:
  * if the window already exists it is restored and brought to the front, never duplicated;
  * if one is already being launched (a second click a moment later), the click is ignored;
  * only after a window has failed to appear do we move on to the next browser.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes

log = logging.getLogger("copyclip.launcher")

MAIN_SIZE = (1180, 760)
FLYOUT_SIZE = (380, 580)
MAIN_TITLE = "CopyClip"
FLYOUT_TITLE = "CopyClip quick menu"

#: How long after a launch further requests are treated as "already opening".
LAUNCH_SETTLE_S = 20.0
#: How long we wait for a window to appear before trying the next browser.
WINDOW_WAIT_S = 12.0
POLL_S = 0.3

CHROMIUM_EXES = {"msedge.exe": "edge", "chrome.exe": "chrome", "brave.exe": "brave",
                 "vivaldi.exe": "vivaldi", "opera.exe": "opera", "chromium.exe": "chromium"}
KNOWN_PATHS = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)


# ------------------------------------------------------------------ browser discovery

def find_edge(exists=os.path.isfile) -> str | None:
    for c in KNOWN_PATHS[:2]:
        path = os.path.expandvars(c)
        if "%" not in path and exists(path):
            return path
    return None


def exe_from_command(command: str) -> str | None:
    """First executable path in a registry shell\\open\\command string."""
    m = re.match(r'\s*"([^"]+)"', command) or re.match(r"\s*(\S+\.exe)", command, re.I)
    return m.group(1) if m else None


def default_browser_exe(read=None) -> str | None:
    """The person's default browser executable, from the registry, or None."""
    try:
        import winreg
        if read is None:
            def read():
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                    prog = winreg.QueryValueEx(k, "ProgId")[0]
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\shell\open\command") as k:
                    return winreg.QueryValueEx(k, "")[0]
        return exe_from_command(read())
    except Exception:
        return None


def browser_candidates(default_exe: str | None = None, exists=os.path.isfile) -> list[tuple[str, bool]]:
    """Ways to show an app window, best first, as (exe, use_normal_profile).

    Isolated-profile launches come first (own process tree, own taskbar entry): the person's
    default Chromium browser, then the others. A normal-profile launch of the default browser
    is the last resort, because it runs inside their daily browser process.
    """
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    default = default_exe if default_exe is not None else default_browser_exe()
    default_ok = bool(default and os.path.basename(default).lower() in CHROMIUM_EXES and exists(default))
    if default_ok:
        out.append((default, False))
        seen.add(default.lower())
    for c in KNOWN_PATHS:
        path = os.path.expandvars(c)
        if "%" not in path and path.lower() not in seen and exists(path):
            out.append((path, False))
            seen.add(path.lower())
    if default_ok:
        out.append((default, True))
    return out


def profile_dir(exe: str | None = None) -> str:
    """Private profile for the app window. One per browser, so two engines never share one."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    name = CHROMIUM_EXES.get(os.path.basename(exe or "").lower(), "browser")
    return os.path.join(base, "CopyClip", "ui-profile", name)


def prepare_profile(exe: str, directory: str) -> None:
    """Make a brand-new private profile open clean. Brave shows a first-run analytics banner
    across the top of the window; acknowledging it in the (empty) profile's Local State means a
    person never sees it in what should look like a standalone app. Existing profiles are left alone."""
    if CHROMIUM_EXES.get(os.path.basename(exe).lower()) != "brave":
        return
    state = os.path.join(directory, "Local State")
    try:
        if os.path.exists(state):
            return
        os.makedirs(directory, exist_ok=True)
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"brave": {"p3a": {"enabled": False, "notice_acknowledged": True}}}, fh)
    except OSError as exc:
        log.debug("could not prepare the %s profile: %s", os.path.basename(exe), exc)


def edge_args(edge: str, url: str, *, user_data_dir: str | None, size: tuple[int, int],
              position: tuple[int, int] | None = None) -> list[str]:
    """Command line for a chromeless app window. `user_data_dir=None` uses the normal profile."""
    args = [edge, f"--app={url}", f"--window-size={size[0]},{size[1]}"]
    if user_data_dir:
        args += [f"--user-data-dir={user_data_dir}", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--disable-features=Translate,msEdgeSidebarV2"]
    if position is not None:
        args.append(f"--window-position={position[0]},{position[1]}")
    return args


# ----------------------------------------------------------------------- win32 windows

def _user32():
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetForegroundWindow.restype = wintypes.HWND
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    return u


def title_matches(actual: str, wanted: str) -> bool:
    """A browser may add a suffix such as " - Brave"; "CopyClip" must not match "CopyClip quick menu"."""
    return actual == wanted or actual.startswith(wanted + " - ") or actual.startswith(wanted + " – ")


def find_window(title: str = MAIN_TITLE) -> int | None:
    """Handle of the visible Chromium window with this title, or None. No side effects."""
    user32 = _user32()
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if title_matches(buf.value, title) and cls.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True
    user32.EnumWindows(EnumProc(visit), 0)
    return found[0] if found else None


def bring_to_front(hwnd: int) -> None:
    """Restore if minimised and take the foreground even though we are a background process.

    Windows only lets the foreground thread change the foreground window, so we briefly attach
    our input queue to it; that is the documented way around the restriction.
    """
    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)


def focus_existing(title: str = MAIN_TITLE) -> bool:
    """Bring an already-open window with this title to the front. True if there is one."""
    hwnd = find_window(title)
    if hwnd is None:
        return False
    bring_to_front(hwnd)
    return True


def window_exists(title: str = MAIN_TITLE) -> bool:
    return find_window(title) is not None


def flyout_position(cursor: tuple[int, int], work_area: tuple[int, int, int, int],
                    size: tuple[int, int] = FLYOUT_SIZE, margin: int = 8) -> tuple[int, int]:
    """Place the flyout above the tray, clamped inside the monitor's work area."""
    left, top, right, bottom = work_area
    w, h = size
    x = min(max(left + margin, cursor[0] - w // 2), right - w - margin)
    y = max(top + margin, bottom - h - margin)
    return x, y


def work_area_near(point: tuple[int, int]) -> tuple[int, int, int, int]:
    """Work area (screen minus taskbar) of the monitor containing `point`."""
    user32 = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    mon = user32.MonitorFromPoint(wintypes.POINT(*point), 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1) - 48


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ------------------------------------------------------------------ single-instance logic

class WindowSlot:
    """One window (main or flyout) that may exist at most once, however fast the clicks come."""

    def __init__(self, title: str, *, clock=time.monotonic) -> None:
        self.title = title
        self._clock = clock
        self._lock = threading.Lock()
        self._launching_until = 0.0

    def request(self, *, focus, launch) -> str:
        """Decide what one click means. Returns focused | launching | launched | failed."""
        with self._lock:
            if focus(self.title):
                self._launching_until = 0.0
                return "focused"
            if self._clock() < self._launching_until:
                return "launching"     # a launch from a moment ago is still opening its window
            self._launching_until = self._clock() + LAUNCH_SETTLE_S
        ok = launch()
        if not ok:
            self.settled()
            return "failed"
        return "launched"

    def settled(self) -> None:
        with self._lock:
            self._launching_until = 0.0


_SLOTS = {"main": WindowSlot(MAIN_TITLE), "flyout": WindowSlot(FLYOUT_TITLE)}


def open_ui(server, route: str = "overview", *, flyout: bool = False) -> str:
    """Show the main window (focusing it if open) or the tray flyout. Safe to call repeatedly."""
    slot = _SLOTS["flyout" if flyout else "main"]
    url = server.launch_url("flyout" if flyout else route)
    return slot.request(focus=focus_existing, launch=lambda: open_window(url, flyout=flyout, slot=slot))


# ----------------------------------------------------------------------------- launching

def _launch(exe: str, url: str, normal_profile: bool, size, position):
    profile = None if normal_profile else profile_dir(exe)
    if profile:
        prepare_profile(exe, profile)
    return subprocess.Popen(edge_args(exe, url, user_data_dir=profile, size=size, position=position),
                            close_fds=True)


def open_window(url: str, *, flyout: bool = False, candidates=None, slot: WindowSlot | None = None) -> bool:
    """Start a browser app window for `url`. False only if nothing could be launched."""
    cands = browser_candidates() if candidates is None else candidates
    if not cands:
        log.warning("no Chromium browser found; opening the default browser instead")
        return webbrowser.open(url)
    position = None
    size = FLYOUT_SIZE if flyout else MAIN_SIZE
    if flyout:
        cur = cursor_pos()
        position = flyout_position(cur, work_area_near(cur))
    for i, (exe, normal) in enumerate(cands):
        try:
            proc = _launch(exe, url, normal, size, position)
        except OSError as exc:
            log.error("could not start %s: %s", os.path.basename(exe), exc)
            continue
        log.info("opening the CopyClip window with %s%s (pid %s)", os.path.basename(exe),
                 " (normal profile)" if normal else "", proc.pid)
        threading.Thread(target=_ensure_window, args=(proc, cands[i + 1:], url, flyout, size, position, slot),
                         daemon=True, name="copyclip-launch-check").start()
        return True
    return webbrowser.open(url)


def _wait_for_window(proc, title: str, *, timeout_s: float = WINDOW_WAIT_S, sleep=time.sleep,
                     clock=time.monotonic, has_window=None) -> str:
    """Wait until the window shows up. Returns "window", "alive" (still starting) or "gone"."""
    has = has_window or (lambda: window_exists(title))
    deadline = clock() + timeout_s
    while True:
        if has():
            return "window"
        if clock() >= deadline:
            return "alive" if proc.poll() is None else "gone"
        sleep(POLL_S)


def _ensure_window(proc, remaining, url: str, flyout: bool, size, position,
                   slot: WindowSlot | None = None, *, sleep=time.sleep, clock=time.monotonic,
                   has_window=None) -> None:
    """Try the next browser only when this one produced no window. Never opens a second window
    while the first is merely slow to start."""
    title = FLYOUT_TITLE if flyout else MAIN_TITLE
    try:
        state = _wait_for_window(proc, title, sleep=sleep, clock=clock, has_window=has_window)
        if state != "gone":
            return          # window is up, or the browser is still starting: leave it alone
        for exe, normal in remaining:
            log.warning("no window appeared; trying %s%s", os.path.basename(exe), " (normal profile)" if normal else "")
            try:
                proc = _launch(exe, url, normal, size, position)
            except OSError as exc:
                log.error("could not start %s: %s", os.path.basename(exe), exc)
                continue
            if _wait_for_window(proc, title, sleep=sleep, clock=clock, has_window=has_window) != "gone":
                return
        log.warning("no browser produced an app window; opening the default browser")
        webbrowser.open(url)
    finally:
        if slot is not None:
            slot.settled()


def pick_folder(initial: str = "") -> str | None:
    """Native folder dialog, run in a child process so Tk never shares a thread with the daemon."""
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = filedialog.askdirectory(initialdir=sys.argv[1] or None, parent=r, title='Choose a folder')\n"
        "print(p or '')\n"
    )
    exe = os.path.join(os.path.dirname(os.sys.executable), "python.exe")
    exe = exe if os.path.isfile(exe) else os.sys.executable
    try:
        out = subprocess.run([exe, "-I", "-c", code, initial], capture_output=True, text=True,
                             timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("folder picker failed: %s", exc)
        return None
    path = out.stdout.strip()
    return os.path.normpath(path) if path else None
