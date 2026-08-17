# SPIKE: does `winclip.ClipboardWatcher` go silent after Windows sleep/resume?

Not a bug. A throwaway pre-implementation spike, following the `bugs/` conventions
(measured evidence, never recollection; say when something is inferred rather than proven).
Source: `spike/win_clipboard_listener_probe.py`. Not referenced by production code and safe
to delete once this file exists. Ran against the **laptop** (`predator-helios-300`) across a
real sleep/resume cycle on **2026-08-14/15**.

## Why this exists

The user reported that after the Windows laptop slept and they logged back in, copying text
on Windows stopped syncing to the phone until the daemon was manually restarted. Three
independent codebase explorations converged on a plausible, well-reasoned hypothesis:
`winclip.ClipboardWatcher`'s `AddClipboardFormatListener` mechanism (`winclip.py:368-463`)
has no ongoing liveness check — the only fallback to polling happens once, at startup, only
if listener *registration* fails. If Windows silently stopped delivering
`WM_CLIPBOARDUPDATE` after a sleep/resume (a real-world quirk this class of Win32 API is
known for), the listener thread would just block forever in `GetMessageW` with nothing to
report, matching the symptom exactly. This had never been measured in this codebase before —
per this project's spike-first rule, it had to be confirmed before writing a fix.

## The probe

`spike/win_clipboard_listener_probe.py` duplicates the `AddClipboardFormatListener`
mechanism directly (not by importing `winclip.py`, so it can log more aggressively without
touching production code) with three simultaneous instruments:

1. The listener itself: logs wall-clock time + `GetClipboardSequenceNumber()` on every real
   `WM_CLIPBOARDUPDATE`.
2. An independent poll thread: reads `GetClipboardSequenceNumber()` every 1s regardless of
   the listener — ground truth for "did anything actually happen," decoupled from whether the
   listener noticed.
3. The same window's `wndproc` also logs any `WM_POWERBROADCAST` it receives, to measure
   whether a message-only (`HWND_MESSAGE`) window even gets power-event broadcasts at all
   (undocumented for this window type, and relevant to whether a resume-triggered proactive
   fix would even be buildable).

Log lines are flushed and `fsync`'d immediately per line, since the process is suspended
mid-run by the very event under test.

## Result: did not reproduce, across a real, long sleep

Real sleep window, confirmed via `Get-WinEvent`/Power-Troubleshooter (not the misleading
near-instant `Kernel-Power` 42/107 event pair, which does not reflect true sleep duration on
this machine): **14:12:25 → 22:53:03, 7h41m** on 2026-08-14, and separately a **7h17m**
overnight sleep the following night (01:42:55 → 08:59:41 on 2026-08-15).

Across both real sleep cycles, the probe's listener fired correctly for every post-wake test
copy, matched by the independent poll thread within under a second each time:

```
2026-08-15 09:01:59.xxx  [listener] WM_CLIPBOARDUPDATE fired, sequence_number()=737
2026-08-15 09:01:59.xxx  [poll] sequence_number changed 736 -> 737 (ground truth: ...)
2026-08-15 09:02:02.xxx  [listener] WM_CLIPBOARDUPDATE fired, sequence_number()=738
2026-08-15 09:02:14.xxx  [listener] WM_CLIPBOARDUPDATE fired, sequence_number()=739
```

A direct, independent read of `GetClipboardSequenceNumber()` immediately afterward (via a
fresh, unrelated `ctypes` call, not the probe process) matched the probe's last logged value
exactly (`739`) — confirming the probe's own state wasn't stale or lagging.

No `WM_POWERBROADCAST` line was ever logged, across either sleep — consistent with (though
not conclusive proof of) the general expectation that message-only windows don't receive
broadcast messages, but this was only ever observed as an absence, not confirmed against a
known-good broadcast-receiving control.

## What this does and doesn't prove

- **The hypothesis did not reproduce.** Two long, real sleep cycles, zero missed events, zero
  divergence between the event-driven listener and the independent ground-truth poll. This is
  real evidence against the hypothesis, not just an absence of trying.
- **It does not prove the mechanism can never go stale.** This class of Windows quirk is
  documented as intermittent, not deterministic, elsewhere; two clean runs lower the prior
  significantly but don't rule it out for a different sleep duration, a different power event
  (hibernate vs. sleep, lid-close vs. Start-menu sleep), or a future Windows update.
- **The real cause was found separately, the same night**: `tailscale status` showed the
  phone's Tailscale connection had been offline for 4 hours and the tablet's for 4 days —
  Android's Doze mode killing Tailscale's background connection during idle, unrelated to
  anything on the Windows side. See `bugs/SPIKE-LAN-TLS-FALLBACK.md` for the follow-up work
  this actually motivated (a same-WiFi fallback transport for exactly this failure mode).

## Verdict

**Do not build the originally-planned Windows-side heartbeat/watchdog fix** for
`winclip.ClipboardWatcher` — the evidence doesn't support it being the actual cause of the
reported bug, and this repo's own convention is to measure before building, not harden
against an unreproduced theory. The known, already-working mitigation (manually restarting
the daemon) remains the workaround for the rare case, if any, where this class of Windows
quirk does eventually strike. Effort is better spent on the confirmed real cause (Tailscale
reliability on Android) — see `SPIKE-LAN-TLS-FALLBACK.md`.
