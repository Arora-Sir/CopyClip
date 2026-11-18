# BUG-019: One repeated failure writes thousands of identical tracebacks and pushes real history out of the log

## Status
PARTLY FIXED (2026-11-12). Log flooding and the unhandled adb timeouts are fixed in code. The root cause of the clipboard write storm below is **OPEN** and deliberately untouched.

## Severity
Medium. No data is lost by the log noise itself, but four rotated 2 MB files covered only about two weeks, and the useful lines were buried.

## Evidence (measured, from `logs/copyclip.log*`, 2026-10-28 to 2026-11-11)
- 4,590 tracebacks in total.
- 2,330 were `[pixel] unexpected connect error`: a `subprocess.TimeoutExpired` from an `adb` call that was not converted into a `TransportError`, so the supervisor logged a full traceback on every retry for a device that was simply offline.
- 1,955 were `[laptop] write failed` with `OpenClipboard failed ... (err=5)` (access denied), repeating about every 6 seconds. Another application held the clipboard. Cause of the holder not identified.

## Fix
- `core/logsetup.py` `FoldingHandler`: consecutive identical records, including repeated tracebacks, collapse into one line plus "(previous message repeated N more times)", re-emitted at least once a minute. Tests: `tests/unit/test_logging.py`.
- `transports/adb_transport.py`: every `adb` subprocess call converts timeouts and a missing binary into `TransportError`, which the supervisor already treats as a normal "device unreachable" with a one-line warning. Tests: `tests/unit/test_hardening.py::AdbTimeoutTests`.
- The log viewer folds repeats too, and reads the folded summaries, so old logs display sensibly.
- No console handler is created when `sys.stdout` is `None` (pythonw), where every record used to fail silently.

## Open
- What holds the clipboard when `OpenClipboard` returns 5, and whether the retry pacing in `winclip.py` should back off. `winclip.py` was intentionally not changed: the write path is on the production text-sync path.
