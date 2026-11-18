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

import winimage

log = logging.getLogger("copyclip.win")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
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
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int

# Declaring these is load-bearing, not tidiness. Without argtypes, ctypes marshals
# arguments as C int; LPARAM and HWND are pointer-sized on 64-bit, so any message
# carrying a real pointer raised
#   ctypes.ArgumentError: argument 4: OverflowError: int too long to convert
# inside the window procedure, and every non-clipboard message was dropped.
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

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


ERROR_ACCESS_DENIED = 5
ERROR_CLIPBOARD_NOT_OPEN = 1418

# Errors that mean "someone else was mid-operation", i.e. worth retrying rather than
# failing the whole write. 1418 in particular was observed in the wild: EmptyClipboard
# reported ERROR_CLIPBOARD_NOT_OPEN despite OpenClipboard having just succeeded.
_TRANSIENT = {0, ERROR_ACCESS_DENIED, ERROR_CLIPBOARD_NOT_OPEN}

# The Windows clipboard is a single system-wide exclusive resource. Our own watcher thread
# (reading on WM_CLIPBOARDUPDATE) and a device node's pump thread (writing) will otherwise
# contend for it constantly and lose races against each other for no reason. Serialising
# our own access removes that entire class of failure; it does nothing about other
# processes, which is what the retry loops are for.
_ACCESS_LOCK = threading.RLock()

# Set by ClipboardWatcher once its message-only window exists. Owning the clipboard with a
# real HWND is the documented-correct form: with a NULL owner, EmptyClipboard sets the
# clipboard owner to NULL, which MSDN notes can make subsequent calls fail.
_owner_hwnd = None

# Registered clipboard formats for pass-through image bytes. Registered once at import;
# RegisterClipboardFormatW is idempotent and returns the same id on every call once a name
# is registered process-wide, so this is safe to call unconditionally. "JFIF" and "GIF" are
# the actual names Windows and other apps (browsers, Office, Paint) use for these: not
# invented here.
CF_PNG = user32.RegisterClipboardFormatW("PNG")
CF_JFIF = user32.RegisterClipboardFormatW("JFIF")
CF_GIF = user32.RegisterClipboardFormatW("GIF")

_REGISTERED_MIME = {
    CF_PNG: "image/png",
    CF_JFIF: "image/jpeg",
    CF_GIF: "image/gif",
}
_MIME_REGISTERED = {v: k for k, v in _REGISTERED_MIME.items()}

# Set alongside an image in write_image() (DWORD 0) to tell Windows' Clipboard History
# service to skip that item entirely: a large device-synced screenshot was observed
# evicting the user's existing History entries (see ARCHITECTURE.md's "Two entries in
# Windows Clipboard History" note and docs/bugs/BUG-005's Open points for the full
# investigation); this trades away that one image's own History visibility (Ctrl+V still
# pastes it correctly: this only affects History's bookkeeping, never the live clipboard)
# in exchange for not disturbing the rest of the user's history.
CF_CAN_INCLUDE_IN_HISTORY = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
CF_CAN_UPLOAD_TO_CLOUD = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")


class _Opened:
    """RAII clipboard lock.

    OpenClipboard genuinely fails when another app holds the lock, which happens constantly
    on a busy desktop (Office, browsers, clipboard managers all grab it). Without retries a
    copy is silently dropped, so retrying here is load-bearing, not defensive noise.
    """

    def __init__(self, attempts: int = 12, delay: float = 0.02) -> None:
        self.attempts = attempts
        self.delay = delay
        self._held = False

    def __enter__(self) -> None:
        _ACCESS_LOCK.acquire()
        try:
            for i in range(self.attempts):
                if user32.OpenClipboard(_owner_hwnd):
                    self._held = True
                    return
                time.sleep(self.delay * (1 + i * 0.5))
        except BaseException:
            _ACCESS_LOCK.release()
            raise
        _ACCESS_LOCK.release()
        raise ClipboardBusy(
            f"OpenClipboard failed after {self.attempts} attempts "
            f"(err={ctypes.get_last_error()})"
        )

    def __exit__(self, *exc: object) -> None:
        try:
            if self._held:
                user32.CloseClipboard()
        finally:
            _ACCESS_LOCK.release()


def sequence_number() -> int:
    """Cheap change token. Does not require opening the clipboard."""
    return int(user32.GetClipboardSequenceNumber())


_READ_NULL_RETRIES = 4
_READ_NULL_DELAY = 0.03


def read_text() -> str | None:
    """Current clipboard text, or None when it holds no text (an image, files, nothing)."""
    # NOTE: Windows occasionally reports CF_UNICODETEXT as available while GetClipboardData returns NULL (err 1418) for a few milliseconds after a write.
    # NOTE: Measured 2026-11-18 at about 1.4 percent of write then read cycles, so a NULL handle for an available format is retried after a fresh open.
    for attempt in range(_READ_NULL_RETRIES + 1):
        with _Opened():
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if handle:
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
        if attempt < _READ_NULL_RETRIES:
            time.sleep(_READ_NULL_DELAY)
    return None


def _read_global(fmt: int) -> bytes | None:
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = kernel32.GlobalSize(handle)
        if not size:
            return None
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(handle)


def read_image() -> tuple[bytes, str] | None:
    """Current clipboard image as (bytes, mime), or None when it holds no image.

    Preference order: a registered pass-through format (PNG/JFIF/GIF) first, because that
    is the *original* encoded bytes exactly as whatever app put them there: lossless,
    no re-encode, no bloat. Only falls back to CF_DIBV5/CF_DIB (converted to PNG via
    winimage) when no registered format is present, which happens for apps that only ever
    set the DIB formats (e.g. a plain screenshot tool, or MS Paint on some versions).

    Caller (nodes/windows_node.py) checks CF_UNICODETEXT first and only calls this when
    that returns None, so a text clip carrying an incidental image format is never
    misread as an image.
    """
    with _Opened():
        for fmt, mime in _REGISTERED_MIME.items():
            if fmt and user32.IsClipboardFormatAvailable(fmt):
                data = _read_global(fmt)
                if data:
                    return data, mime
        if user32.IsClipboardFormatAvailable(CF_DIBV5):
            dib = _read_global(CF_DIBV5)
            if dib:
                try:
                    return winimage.dib_to_png(dib), "image/png"
                except winimage.ImageDecodeError as exc:
                    log.warning("CF_DIBV5 present but unreadable: %s", exc)
        if user32.IsClipboardFormatAvailable(CF_DIB):
            dib = _read_global(CF_DIB)
            if dib:
                try:
                    return winimage.dib_to_png(dib), "image/png"
                except winimage.ImageDecodeError as exc:
                    log.warning("CF_DIB present but unreadable: %s", exc)
        return None


def _global_from_bytes(data: bytes) -> wintypes.HGLOBAL:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise MemoryError("GlobalLock failed")
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _write_once(items: list[tuple[int, bytes]]) -> None:
    """One open-empty-alloc-set(-set...)-close cycle. Raises on any failure.

    Accepts multiple (format_id, data) pairs so several formats can be set for one
    clipboard ownership: e.g. CF_DIBV5 plus a registered pass-through format for one
    image: in a single atomic-looking operation, exactly as write_text() already does
    for its single CF_UNICODETEXT item.
    """
    with _Opened():
        if not user32.EmptyClipboard():
            raise OSError(f"EmptyClipboard failed (err={ctypes.get_last_error()})")
        for fmt, data in items:
            handle = _global_from_bytes(data)
            if not user32.SetClipboardData(fmt, handle):
                # Ownership only transfers to the system on success; free it ourselves.
                kernel32.GlobalFree(handle)
                raise OSError(f"SetClipboardData({fmt}) failed (err={ctypes.get_last_error()})")


def _write_retrying(items: list[tuple[int, bytes]], attempts: int) -> None:
    """Retries the **whole** sequence, not just the open. Previously only OpenClipboard was
    retried, so a mid-sequence failure (observed: EmptyClipboard returning
    ERROR_CLIPBOARD_NOT_OPEN after a successful open) abandoned the write outright and the
    clip was lost.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            _write_once(items)
            return
        except (OSError, ClipboardBusy) as exc:
            last = exc
            err = getattr(exc, "winerror", None) or ctypes.get_last_error()
            if isinstance(exc, OSError) and err not in _TRANSIENT:
                raise
            log.warning("clipboard write attempt %d/%d failed (%s); retrying",
                        i + 1, attempts, exc)
            time.sleep(0.05 * (i + 1))
    raise OSError(f"clipboard write failed after {attempts} attempts: {last}")


def write_text(text: str, attempts: int = 4) -> None:
    """Replace the clipboard with `text`."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    _write_retrying([(CF_UNICODETEXT, data)], attempts)


def write_text_sensitive(text: str, attempts: int = 4) -> None:
    """Replace the clipboard with sensitive `text`, explicitly excluding it from
    both Windows Clipboard History (Win+V) and Microsoft Cloud Clipboard sync.
    """
    data = text.encode("utf-16-le") + b"\x00\x00"
    zero = (0).to_bytes(4, "little")
    items = [
        (CF_UNICODETEXT, data),
        (CF_CAN_INCLUDE_IN_HISTORY, zero),
        (CF_CAN_UPLOAD_TO_CLOUD, zero),
    ]
    _write_retrying(items, attempts)


def clear_clipboard(attempts: int = 4) -> int:
    """Empty the clipboard without setting any format data.

    Calls EmptyClipboard() inside an open/close cycle, then immediately closes
    without calling SetClipboardData() for any format. Windows Clipboard History
    (Win+V) only creates new entries when SetClipboardData() is called with an
    actual format payload; a bare EmptyClipboard() call is invisible to the
    history, so no blank tile is added.

    Returns the new clipboard sequence number so callers can update their own
    write stamp and suppress the echo from ClipboardWatcher._on_change().
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            with _Opened():
                if not user32.EmptyClipboard():
                    raise OSError(
                        f"EmptyClipboard failed (err={ctypes.get_last_error()})"
                    )
            # Sequence number advances when EmptyClipboard succeeds. Read it while
            # the lock is released: no clipboard ownership needed for this call.
            return sequence_number()
        except (OSError, ClipboardBusy) as exc:
            last = exc
            err = getattr(exc, "winerror", None) or ctypes.get_last_error()
            if isinstance(exc, OSError) and err not in _TRANSIENT:
                raise
            log.warning(
                "clear_clipboard attempt %d/%d failed (%s); retrying",
                i + 1, attempts, exc,
            )
            time.sleep(0.05 * (i + 1))
    raise OSError(f"clear_clipboard failed after {attempts} attempts: {last}")


def write_image(data: bytes, mime: str, attempts: int = 4, *,
                 exclude_from_history: bool = True) -> None:
    """Replace the clipboard with an image, encoded as `data`/`mime`.

    Sets, in one open/empty/close cycle: CF_DIBV5 (decoded via winimage, alpha
    preserved) so *any* app that only understands the DIB formats can paste it, plus
    the original bytes under the matching registered format (PNG/JFIF/GIF) when one
    exists, for a byte-identical, no-re-encode round trip in apps that prefer it, plus
: when `exclude_from_history` is true (the default): CF_CAN_INCLUDE_IN_HISTORY=0
    so this image doesn't show up in (or evict entries from) Windows Clipboard History.
    Ctrl+V always pastes it normally either way; the flag only affects History's own
    bookkeeping. See config.json's `screenshot_exclude_from_history` and
    docs/bugs/BUG-005-duplicate-screenshot-push.md's Open points for why this exists.

    Raises winimage.ImageDecodeError if `data` cannot be decoded (corrupt bytes, or a
    format WIC has no codec for): callers must treat that as a failed write, not crash.
    """
    bgra, width, height = winimage.decode(data)
    dibv5 = winimage.png_to_dibv5(bgra, width, height)
    dib = winimage.png_to_dib(bgra, width, height)

    items: list[tuple[int, bytes]] = [(CF_DIBV5, dibv5), (CF_DIB, dib)]
    registered_fmt = _MIME_REGISTERED.get(mime)
    if registered_fmt:
        items.append((registered_fmt, data))
    if exclude_from_history:
        items.append((CF_CAN_INCLUDE_IN_HISTORY, (0).to_bytes(4, "little")))

    _write_retrying(items, attempts)


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
        self._last_seen_seq = 0
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
        global _owner_hwnd
        self._last_seen_seq = sequence_number()

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                self._last_seen_seq = sequence_number()
                try:
                    self.on_change()
                except Exception:
                    log.exception("clipboard callback failed")
                return 0
            if msg == WM_TIMER:
                curr = sequence_number()
                if curr != self._last_seen_seq:
                    self._last_seen_seq = curr
                    try:
                        self.on_change()
                    except Exception:
                        log.exception("clipboard timer poll failed")
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
                                            None, None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")

        # Publish it as the clipboard owner for every open from now on.
        _owner_hwnd = self._hwnd
        if not user32.AddClipboardFormatListener(self._hwnd):
            raise OSError(f"AddClipboardFormatListener failed (err={ctypes.get_last_error()})")

        user32.SetTimer(self._hwnd, 1, 1000, None)
        log.info("clipboard listener active (event driven with timer watchdog)")
        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.KillTimer(self._hwnd, 1)
        user32.RemoveClipboardFormatListener(self._hwnd)
        if _owner_hwnd == self._hwnd:
            _owner_hwnd = None

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
