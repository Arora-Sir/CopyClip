# BUG-008: an AHK-triggered daemon restart silently drops the next clip as a "baseline"

**Severity:** High, reproduces on ordinary use, no error surfaced anywhere
**Status:** OPEN, fix applied, awaiting real-use confirmation
**Affects:** laptop; symptom appears as phone/tablet -> laptop sync "randomly not working",
especially right after a laptop restart

---

## Provenance

Originally investigated and fixed on 2026-08-18 (commit `1ebb7a6`) as part of a session that
also built the `feature/lan-fallback-tls` branch's TLS work. That branch is not part of this
fix and stays separate (see `ARCHITECTURE.md`'s `feature/lan-fallback-tls` note); this bug's
fix has nothing to do with transports and is brought to `main` on its own, 2026-08-23, as
BUG-008 rather than reusing the branch's original BUG-006 number, which `main` had already
assigned to a different, unrelated bug in the meantime.

## Symptom as reported

After restarting the laptop, phone -> laptop clipboard sync sometimes doesn't work. No
error is visible anywhere; it just looks like the copy "didn't take," and copying again a
little later usually works fine. The user wondered whether there was some fixed wait time
needed after boot before sync becomes reliable.

## Measurement

`logs/copyclip.log`, the boot this was originally diagnosed on (2026-08-14, boot time
03:02:03 per `Get-CimInstance Win32_OperatingSystem`):

```
03:06:11  CopyClip starting (pid 26176, devices: phone, tab)
03:07:30  CopyClip starting (pid 25640, devices: phone, tab)     <- killed pid 26176
03:13:53  CopyClip starting (pid 8268,  devices: phone, tab)     <- killed pid 25640
```

Three separate daemon starts in the first ~8 minutes of one boot, each a fresh PID. None
is preceded by the graceful-shutdown log line (`"signal %s, shutting down"`,
`copyclip.py`), confirming each prior instance was hard-killed, not signaled, consistent
with `BasicTasks.ahk`'s `RestartNamedPythonServer()` (see Root cause).

The second restart (03:13:53) is the clearest evidence this is not just "waiting for the
daemon to start": `pid 25640` had been running and syncing correctly since 03:10:17:

```
03:10:17  [phone] reconnected; catching up (a clip copied while it was away will apply)
03:10:25  phone -> laptop: 91 chars
03:10:40  phone -> laptop: 10 chars
03:11:13  phone -> laptop: 92058 chars
03:11:24  phone -> laptop: 4525 chars
```

and was killed anyway at 03:13:53, mid-session, by something re-triggering AHK's restart
logic (exact external trigger not identified, possibly a second logon event, a manual AHK
reload while the user was independently troubleshooting, or a Task Scheduler retry; not
necessary to pin down to fix the actual defect, see Root cause). The very next clip from the
phone was silently dropped, twice in this one boot:

```
03:10:04  [phone] connected; adopting its clip as the baseline
03:10:08  baseline from phone (48 chars), not applied
...
03:13:53  CopyClip starting (pid 8268, devices: phone, tab)
03:13:55  [phone] connected; adopting its clip as the baseline
03:13:55  baseline from phone (48 chars), not applied
```

**Independently reproduced 2026-08-23**, while deploying BUG-007's fix: restarting the
daemon to push a rebuilt `clipbridge.dex` logged the identical pattern,
`baseline from phone (37064 chars), not applied`. Harmless that specific time (the content
was an echo of what the laptop had itself just written), but the same code path, unfixed
until this commit.

## Root cause

Two things compound:

1. **`BasicTasks.ahk`'s `RestartNamedPythonServer()`** (the function that launches CopyClip
   from AHK) unconditionally killed and relaunched the daemon every time `BasicTasks.ahk`
   loaded, not just at boot, but on any AHK reload for any reason. This was a deliberate
   choice (see BUG-003) to catch duplicate daemons from other launch paths, but it did not
   distinguish "there's a stray/duplicate to clean up" from "there's already exactly one
   healthy instance running and nothing to fix."
2. **`core/hub.py`'s baseline tracking is purely in-memory** (`_baselined`,
   `_baseline_pending`). Every fresh process, including a spurious restart of an
   already-healthy daemon, has no memory of what it (or a predecessor process) already
   knew about a device, so it treats that device's current clip as never-seen-before and
   deliberately does **not** apply it (`core/hub.py`, `on_clip()`'s baseline-pending
   branch): "at startup we have no idea whether the device's clip is newer than the
   laptop's, so clobbering is rude." That reasoning is correct for a genuine first-ever
   connection, but wrong for a same-boot restart where the previous process already knew
   exactly what was on the device.

BUG-003 already found and partially fixed a close relative of this (re-arming baseline
suppression on every *reconnect*, narrowed to only the daemon's first connect). This bug is
the same mechanism recurring at the *process* level: a full restart is indistinguishable
from a true first-ever run once the in-memory state is gone.

## Fix

Two independent layers, matching the two contributing causes:

**`BasicTasks.ahk`, `RestartNamedPythonServer()` (already applied, confirmed present in the
current script, outside this repo):** before killing anything, count processes matching the
correctly-named exe vs. a stray/duplicate. If exactly one correctly-named instance is
running and no stray exists, skip the kill+relaunch entirely and return. Falls through to
the existing (unchanged) kill-everything-then-relaunch-fresh logic in every other case,
nothing running, a stray duplicate present, or more than one correctly-named instance, so
BUG-003's original defense is fully preserved.

**`core/baseline_store.py` (new, landing on `main` in this commit):** a tiny durable
last-known-content-hash-per-device store, following `core/outbox.py`'s existing
atomic-write pattern (temp file + `os.replace`, JSON, self-healing on a corrupt/missing
file). Wired into `copyclip.py`'s `main()` alongside the existing `Outbox` construction
(`state/baseline.json`), passed into `Hub.__init__` as `baseline_store`.

`core/hub.py`'s `on_clip()` now records the source device's current hash on every observed
clip (regardless of routing outcome), and on a node's first connect *of this process*,
consults the persisted record instead of unconditionally treating it as unknown:

- No persisted record at all: unchanged behavior, adopt as baseline, log it, don't apply
  (a genuine first-ever run).
- Persisted record matches the device's current clip: correctly skip; nothing changed while
  the daemon was down.
- Persisted record **differs**: real content arrived while the daemon was down or
  mid-restart; apply it as a normal clip instead of silently dropping it.

`_deliver_to_node()` also updates the record on every successful delivery *to* a device, so
the store reflects both directions.

## Verification

- `tests/test_core.py::test_hub_restart_survives_baseline_drop`: three separate `Hub`
  instances sharing one on-disk `BaselineStore` file simulate three consecutive daemon
  restarts: (1) a genuine first-ever connect still drops the baseline as before, (2) a
  restart where the device's content is unchanged is correctly skipped, and (3) a restart
  where the device's content changed while the daemon was down is applied, not dropped. All
  three, plus the full existing `tests/test_core.py` suite, pass.
- The AHK-side guard's own effect is separately confirmed: no repeated `"CopyClip starting"`
  churn has been observed in `logs/copyclip.log` since 2026-08-18, consistent with it
  actually suppressing unnecessary restarts.
- **Not yet confirmed live on `main`** (this repo's convention: stays OPEN until real use
  confirms it, not just passing tests). Needs: with CopyClip running healthily, copy
  something on the phone, force-kill the daemon (matching how it was deployed today), let it
  restart, and confirm the clip lands on the laptop instead of being logged as
  `"baseline ..., not applied"`.

## Rollback

Code-only; `git revert`. `core/hub.py` degrades to its exact pre-fix behavior if
`baseline_store=None` is passed (the parameter is optional). The AHK change is a single
function in `BasicTasks.ahk` outside this repo
(`<software>\Programming\AutoHotKey\AllScripts\BasicTasks.ahk`); it predates and is
independent of this commit either way.

## Open points

- **The exact trigger for the 03:07:30 and 03:13:53 restarts on the originally-measured
  boot was not identified**: plausible candidates (a second logon event, manual AHK reload
  during the user's own troubleshooting, a Task Scheduler retry) were not distinguished from
  each other, and doing so was not necessary to fix the actual defect (both layers of the
  fix are correct regardless of why a restart happens).
- **Not yet confirmed in real use** on `main` specifically, per this repo's status
  convention, stays OPEN until confirmed over a few ordinary restarts, not just the unit
  tests above.
- A related, secondary finding from the same investigation: the first daemon start of a boot
  does not happen until several minutes post-logon (other Startup-folder/Run-key apps and
  Windows Defender's own service reinitializing compete for CPU/disk I/O in the same
  window). Lowering the "AHK Startup Script" scheduled task's `Priority` value was
  recommended as a mitigation but requires an elevated session to apply and was not
  independently re-checked here; separate from, and smaller than, the fix above regardless.
