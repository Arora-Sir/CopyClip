"""Guarantee only one CopyClip daemon runs per Windows session.

Two daemons is not a harmless duplicate: each one independently watches the Windows
clipboard and independently writes to the devices, so a single copy on the laptop produces
**two** writes to the phone (two clipboard sets, two toasts), and an incoming clip is
applied by whichever instance wins the race, so the other instance's console shows nothing
even though the clipboard did update. Both symptoms look like flaky sync rather than what
they are.

It is easy to hit by accident: the Desktop shortcut, `Start CopyClip.bat`,
`start_silent.vbs` and the scheduled task are four different ways to launch the same thing.

Enforced with a **named mutex**, not a lock file, because the kernel releases a mutex when
the owning process dies. A lock file left behind by a crash would block every future start
until someone deleted it. The PID file next to it is only there to make the error message
useful, and is never trusted for correctness.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import time
from ctypes import wintypes

log = logging.getLogger("copyclip.instance")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]

ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

# Executables a CopyClip instance can legitimately run as.
# CopyClip_Python.exe is a renamed pythonw some installs launch the tray with (see start-up scripts).
EXPECTED_IMAGES = ("python.exe", "pythonw.exe", "copyclip.exe", "copyclip_python.exe")


def process_image_name(pid: int) -> str | None:
    """Lower-case executable file name of `pid`, or None if it cannot be determined."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def looks_like_copyclip(pid: int) -> bool:
    """Guard for --replace: a stale or reused PID must never get an unrelated process killed."""
    return process_image_name(pid) in EXPECTED_IMAGES

# "Local\" scopes the mutex to the current logon session, which is the right boundary: the
# clipboard is per-session, so a daemon in another session is not a conflict.
MUTEX_NAME = r"Local\CopyClipDaemonSingleton"


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        where = f" (PID {pid})" if pid else ""
        super().__init__(
            f"CopyClip is already running{where}.\n"
            f"  Running two daemons copies everything twice to your phone and makes the\n"
            f"  logs look inconsistent, so this instance is stopping.\n"
            f"  Use --replace to take over, or close the other window first."
        )


SYNCHRONIZE = 0x00100000
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE


def is_running(mutex_name: str = MUTEX_NAME) -> bool:
    """True if some process in this session currently holds the singleton. Never takes it."""
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


class SingleInstance:
    def __init__(self, pidfile: str, mutex_name: str = MUTEX_NAME) -> None:
        self.pidfile = pidfile
        self.mutex_name = mutex_name
        self._handle = None

    # -- helpers -----------------------------------------------------------

    def _read_pid(self) -> int | None:
        try:
            with open(self.pidfile, "r", encoding="utf-8") as fh:
                return int(fh.read().strip())
        except Exception:
            return None

    def _write_pid(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.pidfile), exist_ok=True)
            with open(self.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except Exception:
            log.debug("could not write pid file", exc_info=True)

    def _try_mutex(self) -> bool:
        """True if we now own the singleton."""
        handle = kernel32.CreateMutexW(None, True, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            log.warning("CreateMutexW failed (err=%s); continuing without a guard", err)
            return True   # never block startup because the guard itself broke
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    # -- api ---------------------------------------------------------------

    def acquire(self, replace: bool = False) -> None:
        if self._try_mutex():
            self._write_pid()
            return

        other = self._read_pid()
        if not replace:
            raise AlreadyRunning(other)

        if other and other != os.getpid():
            if not looks_like_copyclip(other):
                log.error("--replace: PID %s (%s) is not a Python process, so it is not being "
                          "killed. Remove %s if it is stale.", other,
                          process_image_name(other) or "gone", self.pidfile)
                raise AlreadyRunning(other)
            log.warning("--replace: terminating existing CopyClip (PID %s)", other)
            try:
                os.kill(other, signal.SIGTERM)
            except Exception as exc:
                log.warning("could not terminate PID %s: %s", other, exc)

        # The kernel releases the mutex when the old process actually exits, which is not
        # instant, so poll rather than assuming.
        for _ in range(40):
            time.sleep(0.25)
            if self._try_mutex():
                self._write_pid()
                log.info("took over from the previous instance")
                return
        raise AlreadyRunning(other)

    def release(self) -> None:
        if self._handle:
            try:
                kernel32.ReleaseMutex(self._handle)
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        try:
            if self._read_pid() == os.getpid():
                os.unlink(self.pidfile)
        except Exception:
            pass

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
