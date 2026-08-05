"""Windows clipboard access and change notification via raw ctypes.

Deliberately no pywin32. It is not installed on this machine, and binding user32 directly
keeps CopyClip dependency-free and immune to a broken pywin32 build after a Python upgrade.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("copyclip.win")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)

LRESULT = ctypes.c_ssize_t
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t


class ClipboardBusy(RuntimeError):
    """Another process held the clipboard for the whole retry window."""


class _Opened:
    """RAII clipboard lock.

    OpenClipboard genuinely fails when another app holds the lock, which happens constantly
    on a busy desktop (Office, browsers, clipboard managers all grab it). Without retries a
    copy is silently dropped, so retrying here is load-bearing, not defensive noise.
    """

    def __init__(self, attempts: int = 12, delay: float = 0.02) -> None:
        self.attempts = attempts
        self.delay = delay

    def __enter__(self) -> None:
        for i in range(self.attempts):
            if user32.OpenClipboard(None):
                return
            time.sleep(self.delay * (1 + i * 0.5))
        raise ClipboardBusy(
            f"OpenClipboard failed after {self.attempts} attempts "
            f"(err={ctypes.get_last_error()})"
        )

    def __exit__(self, *exc: object) -> None:
        user32.CloseClipboard()


def sequence_number() -> int:
    """Cheap change token. Does not require opening the clipboard."""
    return int(user32.GetClipboardSequenceNumber())


def read_text() -> str | None:
    """Current clipboard text, or None when it holds no text (an image, files, nothing)."""
    with _Opened():
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            size = kernel32.GlobalSize(handle)
            if not size:
                return ""
            # UTF-16LE, NUL terminated. Read the whole block then cut at the first NUL so a
            # miscounted size can never truncate mid-character.
            raw = ctypes.string_at(ptr, size)
            text = raw.decode("utf-16-le", "replace")
            return text.split("\x00", 1)[0]
        finally:
            kernel32.GlobalUnlock(handle)


def write_text(text: str) -> None:
    """Replace the clipboard with `text`."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    with _Opened():
        if not user32.EmptyClipboard():
            raise OSError(f"EmptyClipboard failed (err={ctypes.get_last_error()})")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise MemoryError("GlobalLock failed")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            # Ownership only transfers to the system on success; free it ourselves otherwise.
            kernel32.GlobalFree(handle)
            raise OSError(f"SetClipboardData failed (err={ctypes.get_last_error()})")


class ClipboardWatcher:
    """Fires `on_change()` whenever the Windows clipboard changes.

    Uses AddClipboardFormatListener on a message-only window, which is event driven with no
    polling. Falls back to sequence-number polling if the window cannot be created, so the
    daemon degrades instead of dying.
    """

    def __init__(self, on_change, poll_interval: float = 0.15) -> None:
        self.on_change = on_change
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._wndproc_ref = None  # must outlive the window or Windows calls freed memory
        self.mode = "listener"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="copyclip-winclip", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    def _run(self) -> None:
        try:
            self._run_listener()
        except Exception as exc:
            log.warning("clipboard listener unavailable (%s), falling back to polling", exc)
            self.mode = "poll"
            self._run_poller()

    def _run_listener(self) -> None:
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                try:
                    self.on_change()
                except Exception:
                    log.exception("clipboard callback failed")
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROCTYPE(wndproc)
        hinst = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "CopyClipListener"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS is fine on restart
                raise OSError(f"RegisterClassW failed (err={err})")

        self._hwnd = user32.CreateWindowExW(0, "CopyClipListener", "CopyClip", 0, 0, 0, 0, 0,
                                            HWND_MESSAGE, None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")
        if not user32.AddClipboardFormatListener(self._hwnd):
            raise OSError(f"AddClipboardFormatListener failed (err={ctypes.get_last_error()})")

        log.info("clipboard listener active (event driven)")
        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.RemoveClipboardFormatListener(self._hwnd)

    def _run_poller(self) -> None:
        last = sequence_number()
        while not self._stop.is_set():
            time.sleep(self.poll_interval)
            try:
                current = sequence_number()
            except Exception:
                continue
            if current != last:
                last = current
                try:
                    self.on_change()
                except Exception:
                    log.exception("clipboard callback failed")
