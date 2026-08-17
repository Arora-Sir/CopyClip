"""SPIKE, not production code -- see bugs/SPIKE-WINDOWS-CLIPBOARD-LISTENER.md.

Measures whether winclip.ClipboardWatcher's AddClipboardFormatListener mechanism
(winclip.py:402-434) actually goes silent across a real Windows sleep/resume cycle, per
CopyClip's "spike unproven platform behavior before writing production code" rule (CLAUDE.md).

Duplicates the listener mechanism directly (not by importing winclip.ClipboardWatcher) so
this can log more aggressively without touching production code, and so a bug in the probe
itself can never affect the real daemon. Three independent instruments run at once:

  1. The listener itself: on every real WM_CLIPBOARDUPDATE, log wall-clock time +
     GetClipboardSequenceNumber(). This is "did the event-driven path notice."
  2. An independent poll thread: reads GetClipboardSequenceNumber() every 1s regardless of
     the listener. This is ground truth for "did anything actually happen," so a comparison
     against (1) proves or disproves listener silence rather than assuming it.
  3. The same window's wndproc also logs any WM_POWERBROADCAST (0x0218) it receives, with
     wParam -- measures whether a message-only (HWND_MESSAGE) window gets power-event
     broadcasts at all, since that's undocumented for this window type and determines
     whether a resume-triggered proactive fix is buildable.

Run this visibly (not pythonw/hidden) so it's obvious if it dies. Log lines are flushed and
fsynced immediately after every write, because the very event under test (sleep) suspends
this process mid-run -- a buffered logger could lose the evidence.

Usage:
    python spike\\win_clipboard_listener_probe.py [--log PATH]

Then: copy something now (baseline), sleep the laptop the way you normally do for at least
10-15 minutes, log back in, copy something new, and check the log for what fired and what
didn't. Repeat across a few real sleep cycles before concluding anything either way.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import os
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_CLIPBOARDUPDATE = 0x031D
WM_POWERBROADCAST = 0x0218
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


user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
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
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_log_fh = None
_log_lock = threading.Lock()


def _log(line: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    text = f"{stamp}  {line}"
    print(text, flush=True)
    with _log_lock:
        _log_fh.write(text + "\n")
        _log_fh.flush()
        os.fsync(_log_fh.fileno())


def sequence_number() -> int:
    return int(user32.GetClipboardSequenceNumber())


def _poll_thread(interval: float, stop: threading.Event) -> None:
    last = sequence_number()
    _log(f"[poll] baseline sequence_number()={last}")
    while not stop.wait(interval):
        try:
            current = sequence_number()
        except Exception as exc:
            _log(f"[poll] GetClipboardSequenceNumber failed: {exc}")
            continue
        if current != last:
            _log(f"[poll] sequence_number changed {last} -> {current} "
                 f"(ground truth: the clipboard changed, independent of the listener)")
            last = current


def _run_listener(stop: threading.Event) -> None:
    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            _log(f"[listener] WM_CLIPBOARDUPDATE fired, sequence_number()={sequence_number()}")
            return 0
        if msg == WM_POWERBROADCAST:
            _log(f"[power] WM_POWERBROADCAST received, wParam=0x{wparam:X}")
            return 1  # TRUE: we processed it, per WM_POWERBROADCAST's documented contract
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc_ref = WNDPROCTYPE(wndproc)  # must outlive the window
    hinst = kernel32.GetModuleHandleW(None)

    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc_ref
    wc.hInstance = hinst
    wc.lpszClassName = "CopyClipListenerProbe"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS is fine on a re-run
            raise OSError(f"RegisterClassW failed (err={err})")

    hwnd = user32.CreateWindowExW(0, "CopyClipListenerProbe", "CopyClipProbe", 0,
                                   0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")

    if not user32.AddClipboardFormatListener(hwnd):
        raise OSError(f"AddClipboardFormatListener failed (err={ctypes.get_last_error()})")

    _log(f"[listener] active on HWND_MESSAGE window, hwnd={hwnd}, "
         f"baseline sequence_number()={sequence_number()}")

    msg = wintypes.MSG()
    while not stop.is_set():
        got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if got in (0, -1):
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    user32.RemoveClipboardFormatListener(hwnd)


def main() -> None:
    global _log_fh
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=os.path.join(
        os.path.dirname(__file__), "..", "logs", "win_clipboard_listener_probe.log"))
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()

    log_path = os.path.abspath(args.log)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _log_fh = open(log_path, "a", encoding="utf-8")

    _log("=" * 70)
    _log(f"probe starting, pid={os.getpid()}, log={log_path}")
    _log("Copy something now for a baseline, then sleep the laptop for 10-15+ "
         "minutes, log back in, and copy something new. Ctrl+C to stop.")

    stop = threading.Event()
    threading.Thread(target=_poll_thread, args=(args.poll_interval, stop),
                      daemon=True, name="probe-poll").start()

    try:
        _run_listener(stop)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _log(f"[listener] FATAL: {exc}")
        raise
    finally:
        stop.set()
        _log("probe stopping")
        _log_fh.close()


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("This spike only runs on Windows.")
    main()
