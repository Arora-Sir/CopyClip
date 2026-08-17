# BUG-006: an AHK-triggered daemon restart silently drops the next clip as a "baseline"

**Severity:** High — reproduces on ordinary use, no error surfaced anywhere
**Status:** OPEN — fix applied, awaiting real-use confirmation
**Affects:** laptop; symptom appears as phone/tablet → laptop sync "randomly not working",
especially right after a laptop restart

---

## Symptom as reported

After restarting the laptop, phone → laptop clipboard sync sometimes doesn't work. No
error is visible anywhere; it just looks like the copy "didn't take," and copying again a
little later usually works fine. The user wondered whether there was some fixed wait time
needed after boot before sync becomes reliable.

## Measurement

`logs/copyclip.log`, this boot (2026-08-14, boot time 03:02:03 per
`Get-CimInstance Win32_OperatingSystem`):

```
03:06:11  CopyClip starting (pid 26176, devices: phone, tab)
03:07:30  CopyClip starting (pid 25640, devices: phone, tab)     <- killed pid 26176
03:13:53  CopyClip starting (pid 8268,  devices: phone, tab)     <- killed pid 25640
```

Three separate daemon starts in the first ~8 minutes of one boot, each a fresh PID. None
is preceded by the graceful-shutdown log line (`"signal %s, shutting down"`,
`copyclip.py`), confirming each prior instance was hard-killed, not signaled — consistent
with `BasicTasks.ahk`'s `RestartNamedPythonServer()` (see Root cause).

The second restart (03:13:53) is the clearest evidence this is not just "waiting for the
daemon to start": `pid 25640` had been running and syncing correctly since 03:10:17 —

```
03:10:17  [phone] reconnected; catching up (a clip copied while it was away will apply)
03:10:25  phone -> laptop: 91 chars
03:10:40  phone -> laptop: 10 chars
03:11:13  phone -> laptop: 92058 chars
03:11:24  phone -> laptop: 4525 chars
```

— and was killed anyway at 03:13:53, mid-session, by something re-triggering AHK's
restart logic (exact external trigger not identified — possibly a second logon event, a
manual AHK reload while the user was independently troubleshooting, or a Task Scheduler
retry; not necessary to pin down to fix the actual defect, see Root cause). The very next
clip from the phone was silently dropped, twice in this one boot:

```
03:10:04  [phone] connected; adopting its clip as the baseline
03:10:08  baseline from phone (48 chars), not applied
...
03:13:53  CopyClip starting (pid 8268, devices: phone, tab)
03:13:55  [phone] connected; adopting its clip as the baseline
03:13:55  baseline from phone (48 chars), not applied
```

## Root cause

Two things compound:

1. **`BasicTasks.ahk`'s `RestartNamedPythonServer()`** (the function that launches
   CopyClip from AHK) unconditionally kills and relaunches the daemon every time
   `BasicTasks.ahk` loads — not just at boot, but on any AHK reload for any reason. This
   was a deliberate choice (see BUG-003) to catch duplicate daemons from other launch
   paths, but it does not distinguish "there's a stray/duplicate to clean up" from
   "there's already exactly one healthy instance running and nothing to fix."
2. **`core/hub.py`'s baseline tracking is purely in-memory** (`_baselined`,
   `_baseline_pending`). Every fresh process — including a spurious restart of an
   already-healthy daemon — has no memory of what it (or a predecessor process) already
   knew about a device, so it treats that device's current clip as never-seen-before and
   deliberately does **not** apply it (`core/hub.py`, `on_clip()`'s baseline-pending
   branch): "at startup we have no idea whether the device's clip is newer than the
   laptop's, so clobbering is rude." That reasoning is correct for a genuine first-ever
   connection, but wrong for a same-boot restart where the previous process already knew
   exactly what was on the device.

BUG-003 already found and partially fixed a close relative of this (re-arming baseline
suppression on every *reconnect*, narrowed to only the daemon's first connect). This bug
is the same mechanism recurring at the *process* level: a full restart is indistinguishable
from a true first-ever run once the in-memory state is gone.

## Fix

Two independent layers, matching the two contributing causes:

**`BasicTasks.ahk`, `RestartNamedPythonServer()`:** before killing anything, count
processes matching the correctly-named exe vs. a stray/duplicate. If exactly one
correctly-named instance is running and no stray exists, skip the kill+relaunch entirely
and return. Falls through to the existing (unchanged) kill-everything-then-relaunch-fresh
logic in every other case — nothing running, a stray duplicate present, or more than one
correctly-named instance — so BUG-003's original defense is fully preserved.

**`core/baseline_store.py`** (new): a tiny durable last-known-content-hash-per-device
store, following `core/outbox.py`'s existing atomic-write pattern (temp file +
`os.replace`, JSON, self-healing on a corrupt/missing file). Wired into `copyclip.py`'s
`main()` alongside the existing `Outbox` construction (`state/baseline.json`), passed into
`Hub.__init__` as `baseline_store`.

`core/hub.py`'s `on_clip()` now records the source device's current hash on every
observed clip (regardless of routing outcome), and on a node's first connect *of this
process*, consults the persisted record instead of unconditionally treating it as unknown:

- No persisted record at all → unchanged behavior: adopt as baseline, log it, don't apply
  (a genuine first-ever run).
- Persisted record matches the device's current clip → correctly skip; nothing changed
  while the daemon was down.
- Persisted record **differs** → real content arrived while the daemon was down or
  mid-restart; apply it as a normal clip instead of silently dropping it.

`_deliver_to_node()` also updates the record on every successful delivery *to* a device,
so the store reflects both directions.

## Verification

- `tests/test_core.py::test_hub_restart_survives_baseline_drop` — three separate `Hub`
  instances sharing one on-disk `BaselineStore` file simulate three consecutive daemon
  restarts: (1) a genuine first-ever connect still drops the baseline as before, (2) a
  restart where the device's content is unchanged is correctly skipped, and (3) a restart
  where the device's content changed while the daemon was down is applied, not dropped.
  All three, plus the full existing `tests/test_core.py` suite, pass.
- Live, still needed: with CopyClip running healthily, trigger an AHK reload
  (`Win+Ctrl+Alt+R`) and confirm via `logs/copyclip.log` that no new `"CopyClip starting"`
  line appears (previously every reload restarted it); then kill the daemon by hand and
  reload again, confirming it *does* relaunch (proves the BUG-003 guard still works).
  Separately, copy something on the phone, force-kill the daemon, let it restart, and
  confirm the clip lands on the laptop instead of being logged as
  `"baseline ..., not applied"`.

## Rollback

Code-only; `git revert`. `core/hub.py` degrades to its exact pre-fix behavior if
`baseline_store=None` is passed (the parameter is optional). The AHK change is a single
function in `BasicTasks.ahk` outside this repo (`<software>\Programming\AutoHotKey\
AllScripts\BasicTasks.ahk`); reverting it restores the original unconditional
kill+relaunch.

## Open points

- **The exact trigger for the 03:07:30 and 03:13:53 restarts on the measured boot was not
  identified** — plausible candidates (a second logon event, manual AHK reload during the
  user's own troubleshooting, a Task Scheduler retry) were not distinguished from each
  other, and doing so was not necessary to fix the actual defect (both layers of the fix
  are correct regardless of why a restart happens). Worth another look if restarts keep
  recurring after this fix lands, since the AHK-side guard only stops the *unnecessary*
  kill, not whatever is re-triggering `BasicTasks.ahk` in the first place.
- **Not yet confirmed in real use** across a normal restart, per this repo's status
  convention — stays OPEN until confirmed over a few ordinary boots, not just the unit
  tests above.
- A related, secondary finding from the same investigation: the first daemon start of a
  boot does not happen until several minutes post-logon (14+ other Startup-folder/Run-key
  apps and Windows Defender's own service reinitializing compete for CPU/disk I/O in the
  same window). Lowering the "AHK Startup Script" scheduled task's `Priority` value from
  its default (7) to around 4-5 was recommended as a mitigation, but requires an elevated
  session to apply (`Set-ScheduledTask` on this task returned Access Denied from a
  non-admin session) and was not yet done. This is separate from — and smaller than — the
  fix above regardless: issue #1 is fixed by the two layers above no matter how long that
  gap is.
