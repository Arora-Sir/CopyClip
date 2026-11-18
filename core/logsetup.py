"""Logging setup: rotating file, optional console, and folding of identical repeats.

A device that stays offline or a clipboard held by another app used to write the same
multi-line traceback thousands of times, which pushed useful history out of the rotating
files within days. FoldingHandler collapses consecutive identical records into one line
plus a "repeated N more times" summary.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _signature(record: logging.LogRecord) -> tuple:
    exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
    return (record.name, record.levelno, record.getMessage(), exc)


class FoldingHandler(logging.Handler):
    """Wraps another handler and suppresses consecutive identical records.

    The first record always passes through. Repeats within `window_s` of the last emission
    are counted, not written. A different record, or the window expiring, flushes a
    "(repeated N more times)" line first, so nothing is ever hidden for longer than
    `window_s`.
    """

    def __init__(self, inner: logging.Handler, window_s: float = 60.0, clock=time.monotonic) -> None:
        super().__init__(level=inner.level)
        self.inner = inner
        self.window_s = window_s
        self._clock = clock
        self._lock_fold = threading.Lock()
        self._last_sig: tuple | None = None
        self._last_record: logging.LogRecord | None = None
        self._last_emit = 0.0
        self._suppressed = 0

    def setFormatter(self, fmt) -> None:  # noqa: N802 (logging API)
        super().setFormatter(fmt)
        self.inner.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock_fold:
            sig = _signature(record)
            now = self._clock()
            if sig == self._last_sig and now - self._last_emit < self.window_s:
                self._suppressed += 1
                self._last_record = record
                return
            self._flush_summary()
            self._last_sig = sig
            self._last_record = record
            self._last_emit = now
        self.inner.handle(record)

    def _flush_summary(self) -> None:
        """Caller holds _lock_fold."""
        if self._suppressed and self._last_record is not None:
            r = self._last_record
            summary = logging.LogRecord(
                r.name, r.levelno, r.pathname, r.lineno,
                f"(previous message repeated {self._suppressed} more times)", None, None)
            summary.created = time.time()
            self._suppressed = 0
            self.inner.handle(summary)

    def flush(self) -> None:
        with self._lock_fold:
            self._flush_summary()
        self.inner.flush()

    def close(self) -> None:
        self.flush()
        self.inner.close()
        super().close()


def setup_logging(level: str, logs_dir: str) -> None:
    os.makedirs(logs_dir, exist_ok=True)
    fmt = logging.Formatter(FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "copyclip.log"), maxBytes=2_000_000, backupCount=3,
        encoding="utf-8")
    folded = FoldingHandler(file_handler)
    folded.setFormatter(fmt)
    root.addHandler(folded)

    # pythonw.exe has no stdout; a StreamHandler on None fails silently on every record.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(FoldingHandler(console))


_CRASH_FILE = None   # kept open for the whole run: faulthandler writes to it from a signal context


def install_crash_diagnostics(logs_dir: str, state_dir: str, *, replaced_old: bool = False) -> bool:
    """Leave evidence when the process dies. Under pythonw there is no console, so a native crash
    or an unhandled thread exception used to vanish without a trace.

    * faulthandler writes the Python stack of every thread to logs/crash.log on a native fault
    * unhandled exceptions in the main thread, worker threads and finalizers go to the log with a traceback
    * a run marker exists while the process lives and is removed on a clean exit, so the next start
      can say the previous run ended abruptly. Call this only after the single-instance guard succeeded.

    Returns True when the previous run did not exit cleanly."""
    global _CRASH_FILE
    import atexit
    import faulthandler
    import threading

    log = logging.getLogger("copyclip.crash")
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    marker = os.path.join(state_dir, "running.marker")
    # A deliberate --replace ends the old process without its exit handlers, which is not a crash.
    abrupt = os.path.exists(marker) and not replaced_old
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}")
    except OSError:
        pass
    if abrupt:
        log.warning("the previous run ended without a clean shutdown (crash, kill or power loss); "
                    "see logs/crash.log for a native crash trace")

    try:
        _CRASH_FILE = open(os.path.join(logs_dir, "crash.log"), "a", encoding="utf-8", buffering=1)
        _CRASH_FILE.write(f"\n--- run started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} ---\n")
        faulthandler.enable(file=_CRASH_FILE, all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("could not enable the crash trace file: %s", exc)

    def on_exception(exc_type, exc, tb):
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))

    def on_thread_exception(args):
        log.critical("unhandled exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def on_unraisable(args):
        log.error("unraisable exception in %r: %s", args.object, args.err_msg,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception
    sys.unraisablehook = on_unraisable

    def on_exit():
        try:
            os.remove(marker)
        except OSError:
            pass
        log.info("clean shutdown")

    atexit.register(on_exit)
    return abrupt
