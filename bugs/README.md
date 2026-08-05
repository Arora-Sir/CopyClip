# CopyClip bug tracker

Open issues live here as markdown, one file per bug, so the current state of the system is
written down rather than remembered. A bug stays **OPEN** until the owner confirms it is fixed
in real use, not merely when a test passes.

## Status

| ID | Title | Severity | Status |
| :--- | :--- | :--- | :--- |
| [BUG-001](BUG-001-clipboard-toast-storm.md) | Toast storm on every copy; Link to Windows reads the clipboard despite being "off" | Medium (noise, privacy) | **OPEN, fix applied, awaiting confirmation** |
| [BUG-002](BUG-002-windows-emptyclipboard-1418.md) | `EmptyClipboard failed (err=1418)` drops the clip entirely | **High (data loss)** | **OPEN, fix applied, awaiting confirmation** |
| [BUG-003](BUG-003-duplicate-daemon-instances.md) | Two daemons run at once: every laptop copy lands on the phone twice, and log lines go missing | High | **OPEN, fix applied, awaiting confirmation** |

## Companion files

- **[DEVICE-STATE.md](DEVICE-STATE.md)** — measured baseline of both devices: settings,
  package enabled-states, `appops` clipboard permissions, running cross-device services.
  Regenerate with `python tools\device_audit.py`. It is read-only and safe to run any time.

## Conventions

Each bug file carries: symptom as reported, **measured** evidence (commands and their real
output, never recollection), root cause, the fix, how to verify, how to roll back, and
status.

Two rules that came out of BUG-001 and are worth keeping:

1. **Measure before concluding.** The first pass of BUG-001 misread `enabled=0` as
   "disabled" (it means DEFAULT, i.e. enabled) and a broken shell loop made every package
   look disabled. Both wrong conclusions came from not checking the raw output.
2. **Say when a root cause is inferred rather than proven.** BUG-002 fixes three real
   defects but the exact interleaving that triggered it was never reproduced, and the file
   says so.
3. **Measure content, not just events.** BUG-003's first measurement counted clipboard
   *changes* and appeared to confirm a duplicate that was really unrelated copying on the
   laptop. Counting transitions into a known content hash gave the correct answer.
