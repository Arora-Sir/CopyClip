# BUG-004: `CoCreateInstance` fails with `CO_E_NOTINITIALIZED` on some threads, silently dropping an incoming image

**Severity:** High — data loss, same shape as BUG-002
**Status:** CONFIRMED, fixed in real use (2026-09-03)
**Affects:** laptop (`predator-helios-300`); image sync from either Samsung device to the laptop

---

## Symptom as reported

An image sent from the phone to the laptop was rejected outright:

```
2026-08-08 00:05:49 WARNING copyclip.node.windows: [laptop] image write rejected
  (undecodable): CoCreateInstance(WICImagingFactory) failed: HRESULT 0x800401F0
2026-08-08 00:05:49 WARNING copyclip.hub: phone -> laptop rejected (rejected)
```

`WriteResult.REJECTED` is not retried (unlike `FAILED`/`UNAVAILABLE`, see BUG-002), so the
image was gone: no retry, no visible indication beyond the log line.

Found while investigating an unrelated report (a possible screenshot-duplication issue) —
this bug was not the cause of that one, but turned up in the same log while checking for
CopyClip-side image-write failures.

## Root cause

`0x800401F0` is `CO_E_NOTINITIALIZED`: the calling thread has never called
`CoInitializeEx`. `winimage.py` guarded that call with a single process-wide flag:

```python
_CO_INITIALIZED = False

def _ensure_com() -> None:
    global _CO_INITIALIZED
    if _CO_INITIALIZED:
        return
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    ...
    _CO_INITIALIZED = True
```

COM apartment state is inherently **per-thread**, not process-wide. `decode()` runs on
whichever thread calls it — the clipboard watcher thread (`nodes/windows_node.py`) for a
locally-copied image, and each device's own pump thread (`nodes/android_node.py`) for an
incoming image. The first thread to call `decode()` in the process's lifetime sets the
global flag; every other thread that later calls `decode()` sees it already `True`, skips
its own `CoInitializeEx`, and then fails at `CoCreateInstance` because COM was never
initialized on *that* thread specifically.

This is the same class of defect BUG-002 already fixed once for `winclip.py` (a Windows API
with per-thread/serialization requirements, touched from multiple CopyClip threads without
accounting for it) — just in the WIC decode path instead of the raw clipboard path.

## Measurement

Reproduced directly: added a test that spawns 8 fresh OS threads (none of which have ever
called `decode()` before) and has each decode the same PNG.

Against the pre-fix code:
```
decode-from-multiple-threads:
  [FAIL] decode from 8 fresh threads raises nothing  8 error(s): [ImageDecodeError(
    'CoCreateInstance(WICImagingFactory) failed: HRESULT 0x800401F0'), ...]
  [FAIL] every thread decoded the identical, correct pixels  0/8 correct
```
Every one of the 8 fresh threads failed with the exact HRESULT from the production log,
because an earlier test on the main thread had already flipped the module-global flag.

Against the fix:
```
decode-from-multiple-threads:
  [PASS] decode from 8 fresh threads raises nothing  0 error(s): []
  [PASS] every thread decoded the identical, correct pixels  8/8 correct
```

## Fix

`winimage.py` — replaced the module-global `_CO_INITIALIZED: bool` with
`threading.local()`, so each thread tracks and performs its own `CoInitializeEx`
independently:

```python
_com_state = threading.local()

def _ensure_com() -> None:
    if getattr(_com_state, "initialized", False):
        return
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (0, 1):
        raise ImageDecodeError(f"CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08X}")
    _com_state.initialized = True
```

No `CoUninitialize` added: the threads that call this (the watcher thread, each device's
pump thread) live for the daemon's whole process lifetime, so one `CoInitializeEx` per
thread for the life of the process is the correct, simplest lifetime — the same assumption
`winclip.py` already makes about its own message-only window/thread.

## Verification

- `tests/test_imaging.py::test_decode_from_multiple_threads` — spawns 8 fresh threads,
  decodes the same PNG on each, asserts no exceptions and byte-identical output on every
  thread. Fails against the pre-fix code (reproduced above) and passes against the fix.
- Full suite (`tests/test_imaging.py`, `tests/test_core.py`, `tests/test_android_node.py`)
  run clean apart from `test_single_instance` in `test_core.py`, which fails only because a
  live production CopyClip daemon was running at the time and holds the singleton mutex —
  unrelated to this change (that test doesn't touch `winimage.py`), and not a regression.
- Live: needs an actual phone/tablet -> laptop image sync to confirm the fix in real use, per
  this repo's status convention.

## Rollback

Code-only; `git revert` the commit. No device or system state was changed for this bug.

## Open points

- Not yet confirmed live against a real device-to-laptop image transfer (only reproduced via
  the multi-threaded unit test, which uses the exact failure mode seen in production).
