# BUG-002: `EmptyClipboard failed (err=1418)` silently discards the clip

**Severity:** High — this is **data loss**, not cosmetic
**Status:** CONFIRMED, fixed in real use (2026-09-03)
**Affects:** laptop (`predator-helios-300`), any incoming clip

---

## Symptom

A 93,247-character clip sent from the tablet never reached the laptop clipboard:

```
2026-08-05 14:57:51 INFO  copyclip.hub: tab -> laptop: 93247 chars
2026-08-05 14:58:12 ERROR copyclip.node.windows: [laptop] write failed
  File "winclip.py", line 163, in write_text
    raise OSError(f"EmptyClipboard failed (err={ctypes.get_last_error()})")
OSError: EmptyClipboard failed (err=1418)
2026-08-05 14:58:12 WARNING copyclip.hub: tab -> laptop failed (failed)
```

The clip was logged as failed and then **dropped**. Nothing retried it. From the user's
side, a copy on the tablet simply never arrived, with no visible indication.

## Root cause

`1418` is `ERROR_CLIPBOARD_NOT_OPEN`: `EmptyClipboard` reported the calling thread had no
clipboard open, despite `OpenClipboard` having just returned success.

Four defects, in order of how much damage they did:

**1. The failure discarded the payload (`core/hub.py`).** `_deliver_to_node` queued only
`WriteResult.UNAVAILABLE` into the durable outbox. `WriteResult.FAILED` was logged and
returned, so the clip was gone. This is the actual data loss and would have made *any*
transient local write failure lose a clip.

**2. Only the open was retried (`winclip.py`).** `_Opened` retried `OpenClipboard` up to 12
times, but once inside the `with` block a failure of `EmptyClipboard` or `SetClipboardData`
aborted the whole write with no second attempt.

**3. No serialisation between our own threads.** The Windows clipboard is a single
system-wide exclusive resource. CopyClip touches it from at least two threads: the watcher
thread reading on `WM_CLIPBOARDUPDATE`, and a device node's pump thread writing an incoming
clip. They were free to race each other for no reason.

**4. The clipboard was opened with a NULL owner.** `OpenClipboard(NULL)` associates the
clipboard with the calling task and no window. MSDN states that in this case
`EmptyClipboard` sets the clipboard owner to NULL, which can make subsequent clipboard calls
fail. CopyClip already creates a message-only window for change notifications and had no
reason not to use it as the owner.

### Honest limitation

**The exact interleaving that produced 1418 was never reproduced.** It happened once, 21
seconds after a 93 KB write, likely with Windows Clipboard History (`cbdhsvc`) also touching
the clipboard. Defects 2, 3 and 4 are each individually sufficient to explain a failure of
this shape, and defect 1 explains why the failure cost a clip. All four are genuine and are
fixed, but the causal chain for that specific occurrence is inferred, not proven.

## Fix

`core/hub.py`

- Queue `WriteResult.FAILED` into the durable outbox alongside `UNAVAILABLE`, so the
  existing `_drain_outbox()` tick retries it. This is what turns the bug from data loss into
  a delay.

`winclip.py`

- `write_text` now retries the **entire** open-empty-alloc-set-close sequence (4 attempts,
  escalating backoff), treating `1418`, `ERROR_ACCESS_DENIED` and "no error recorded" as
  transient and re-raising anything else immediately.
- A module-level `RLock` serialises every clipboard operation, so our own threads never
  contend. Other processes are still handled by the retry loops.
- The clipboard is opened with the message-only window as owner instead of `NULL`.

## Verification

- `tests/test_core.py::test_winclip_concurrency` runs two writer threads and a reader thread
  concurrently (30 writes, 30 reads) and asserts nothing raises, every write completes, and
  the clipboard ends on one of the written values. It saves and restores the real clipboard
  so it is not destructive.
- Outbox behaviour is covered by `test_outbox`: a superseding single slot, atomic persistence
  and survival across a restart.
- `tests/test_three_device.py` includes an 84 KB payload across all three devices.

## Rollback

Code-only; `git revert` the commit. No device or system state was changed for this bug.

## Open points

- If `EmptyClipboard failed` ever appears again, it should now be followed by
  `queued for retry` and a successful delivery on a later tick. If it is **not** followed by
  a delivery, reopen this bug: that would mean the retry path itself is failing.
- Worth watching whether Windows Clipboard History is implicated. If failures cluster, the
  next step is testing with clipboard history disabled to confirm or rule out `cbdhsvc`.

## Addendum 2026-11-18: read side of the same 1418 quirk

The write side was hardened in September. A probe on 2026-11-18 showed the read side has the same exposure: right after a write, `OpenClipboard` succeeds and `IsClipboardFormatAvailable` is true, yet `GetClipboardData` returns NULL with error 1418 and `EnumClipboardFormats` lists nothing. The data is present again about 300 ms later and the sequence number does not change in between. `read_text()` treated the NULL handle as "no text", so a clip could be missed silently.

Measured before the change: 400 write then `read_text()` cycles returned `None` 5 and 6 times (about 1.4 percent) with the daemon stopped. `read_text()` now retries up to 4 more times, 30 ms apart, with a fresh open each time, and only when the format is available but the handle is NULL. A missing format still returns `None` at once. Measured after: 1200 cycles, 0 misses. `tests/unit/test_winclip_read_retry.py` pins the behaviour with fakes. The process that briefly holds the clipboard was never identified, so the cause remains inferred. Status: fixed and confirmed by measurement, not yet confirmed in real use.
