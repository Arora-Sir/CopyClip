# CopyClip — standing rules for working in this repo

This file auto-loads every session. It is short on purpose: the long-form pipeline for
adding a feature lives in `.claude/skills/copyclip-feature/SKILL.md` and fires on plain
English ("add X feature", "implement X"). These are the rules that apply regardless of
what you're doing here.

## What this project is

One clipboard across a Windows laptop and two Samsung devices, no installed app, no cloud.
Read [ARCHITECTURE.md](ARCHITECTURE.md) before touching anything non-trivial — it explains
*why* the code is shaped the way it is, not just what it does, and changing something
without reading the relevant section is how a proven mechanism quietly breaks.

## Non-negotiables

- **Never regress the working text-sync path.** It is production-used daily. Every change
  gets checked against it: does `SET`/`CLIP`/`GET`/`DESC` framing stay byte-identical? Does
  `winclip.write_text`/`read_text` stay untouched? A new feature is a parallel code path,
  not a refactor of the proven one, unless the user explicitly asks for a refactor.
- **Zero third-party dependencies**, Python and Java both. `winclip.py`/`winimage.py` use
  raw `ctypes`, not `pywin32`. `ClipBridge.java` resolves everything reflectively against
  the stock Android SDK jar, never a patched hidden-API jar. If a task seems to need a
  library, look for the platform-native way first.
- **Never run as root** on either Android device (`android/ClipBridge.java` runs as the
  shell UID via `app_process`; scrcpy issue #1095 is the documented cautionary tale in
  `otherCopyProjects/NOTES.md`). Never suggest it as a fix for anything.
- **Measure before concluding, and say when something is inferred rather than proven.**
  This is `bugs/README.md`'s own rule, born from real investigations that went wrong by
  skipping it. Read real device output (`adb shell`, `dumpsys`, actual clipboard content)
  rather than reasoning from documentation or memory when device behavior is in question.
- **Spike unproven platform behavior before writing production code.** If a mechanism has
  never been demonstrated in this codebase or its `otherCopyProjects/` references, write a
  throwaway probe under `android/spike/` (see `bugs/SPIKE-IMAGE-CLIPBOARD.md` for the
  pattern), run it against a real device, and write down what was actually measured —
  including what wasn't verified — before building on top of it.
- **`otherCopyProjects/` is evidence, not folklore.** It holds shallow clones of prior art
  (scrcpy, ClipCascade, KDE Connect, Shizuku, ...). Before assuming a claim in
  `otherCopyProjects/NOTES.md` is still accurate, check it against the actual source in
  that folder — one already turned out to be wrong (the `FakeContext` hidden-API-jar claim,
  corrected 2026-08-06) because it was never re-verified after being written.
- **Docs are part of the change, not a follow-up.** `ARCHITECTURE.md`, `README.md`, and a
  `bugs/` writeup (for anything that needed a spike, or that fixes a real defect) get
  updated in the same pass as the code, not left for later.
- **No em dashes.** Use commas, colons, parentheses, or plain hyphens instead. Straight
  quotes only (`"`/`'`), never curly.
- **A bug stays OPEN until real-use confirmation**, not just a passing test — see
  `bugs/README.md`'s status convention. If something can't be verified live (a live-device
  paste test that was skipped, a UI flow not exercised), say so explicitly and record it as
  open, don't imply it's done.

## Quick orientation

- `core/` — routing, dedup, outbox, policy. Content-agnostic; operates on bytes + MIME.
- `nodes/` — per-device I/O (`windows_node.py`/`winclip.py`/`winimage.py` for the laptop,
  `android_node.py` for the phone/tablet's wire protocol).
- `domains/` — Samsung Continuity awareness (representative election, health).
- `android/ClipBridge.java` — the on-device helper, rebuilt with `android/build_dex.ps1`.
- `bugs/` — dated incident writeups and device spikes, all with measured evidence.
- `tests/` — `test_core.py`/`test_android_node.py`/`test_imaging.py` are pure logic, no
  devices, run in under a second; `test_e2e.py`/`test_three_device.py` need real hardware.
