# BUG-003: two daemons run at once, duplicating writes and hiding log lines

**Severity:** High — visible wrong behaviour on every copy
**Status:** CONFIRMED, fixed in real use (2026-09-03)
**Affects:** laptop; symptoms appear on the phone

---

## Symptom as reported

1. Copying on the laptop put the clip on the **phone twice** (two clipboard sets, two
   toasts), while the console logged only **one** write. The tablet received it once.
2. Copying on the phone reached the laptop clipboard reliably, but the console **sometimes**
   logged it and sometimes did not.

Both read as flaky sync.

## Root cause

**Two daemon processes were running simultaneously.**

```
PID 32748  pythonw.exe  19:28:41   started via start_silent.vbs
PID 2784   python.exe   19:38:41   started via "Start CopyClip.bat"
```

That single fact accounts for both symptoms exactly:

- **Duplicate writes.** Each daemon independently watches the Windows clipboard and
  independently owns a connection to each device. One copy therefore produced two `SET`
  frames to the phone: two clipboard sets, two toasts. The tablet showed one because
  Samsung relays the settled state rather than each intermediate write.
- **Missing log lines.** Both daemons race to apply an incoming clip to the Windows
  clipboard. Whichever wins logs it *in its own log*. When the silent `pythonw` instance
  won, the visible console showed nothing even though the clipboard had updated.

There are four different ways to start CopyClip (Desktop shortcut, `Start CopyClip.bat`,
`start_silent.vbs`, the scheduled task) and nothing prevented two of them running at once.

## Measurement

A first attempt to measure this counted raw clipboard changes on the phone and reported
**2 changes for 1 copy**, apparently confirming a duplicate even after one daemon was
stopped. That measurement was wrong: the daemon log showed three writes of **6, 21 and 10
characters**, and only the 21-character one belonged to the test. Unrelated copying on the
laptop had inflated the count.

Re-measured by tracking the **content hash** and counting only transitions into the test's
own value:

```
value : DUPE-CHECK-1785940030
  phone  change <-- OUR VALUE
  tablet change <-- OUR VALUE
times OUR value was written to the phone : 1  (expect 1)
RESULT: PASS
```

A separate control confirmed Samsung is not echoing on its own: with the daemon stopped,
one direct write to the phone produced exactly one phone change and one tablet change.

Logging consistency after the fix, three consecutive phone copies:

```
copy 1 : ARRIVED    copy 2 : ARRIVED    copy 3 : ARRIVED
19:58:20 phone -> laptop: 22 chars
19:58:25 phone -> laptop: 22 chars
19:58:30 phone -> laptop: 22 chars
```

3 of 3 arrived, 3 of 3 logged.

## Fix

`core/single_instance.py` (new): a Windows **named mutex** (`Local\CopyClipDaemonSingleton`)
acquired at startup. A second daemon exits with an actionable message naming the running
PID, and `--replace` terminates the incumbent and takes over.

A named mutex rather than a lock file on purpose: the kernel releases a mutex when the
owning process dies, so a crash cannot leave a stale lock that blocks every future start. A
PID file is written alongside purely so the error message can name the other process; it is
never trusted for correctness. If the mutex API itself fails, startup proceeds rather than
being blocked by its own guard.

### Second defect found while tracing this

`core/hub.py` re-armed baseline suppression on **every** reconnect, so the first clip after
any reconnect was adopted silently instead of applied. A clip copied on the phone while the
link was briefly down would therefore be swallowed, which is the exact opposite of the
catch-up behaviour that reconnect is supposed to provide. Baseline now happens only on a
node's **first** connect in a daemon's lifetime; later reconnects apply the clip.

## Verification

- `tests/test_core.py::test_single_instance` asserts the second instance is refused, the
  error names the running PID, and the singleton is reusable after a clean release (so the
  guard cannot leak and permanently block startup).
- Live: starting a second daemon while one runs prints the refusal and exits 2, with the
  daemon count staying at 1.

## Rollback

Code-only; `git revert`. To bypass the guard temporarily, run with `--replace`.

## Open points

- The scheduled task (`install_autostart.ps1`) and a manual double-click can still both fire;
  the guard now makes the second a clean no-op instead of a duplicate, which is the intended
  behaviour rather than something to fix further.
- If `--replace` is ever needed routinely, that suggests something is leaving daemons behind
  and is worth investigating rather than papering over.
