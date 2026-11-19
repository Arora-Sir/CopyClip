# Publishing CopyClip

Status as of 2026-11-19: the GitHub repository is **public** (published 2026-11-18, release v3.0.0, licence Apache-2.0). This page records how that was done and keeps the checklist for every later push.

## What was done on 2026-11-18

| Step | Result |
| :--- | :--- |
| Licence | Apache-2.0. `LICENSE` and `NOTICE` are at the repo root, and `NOTICE` credits scrcpy, the Shizuku API and the Android build dependencies |
| History | The history was rewritten in place with `git filter-repo` (not an orphan snapshot), so the commit history is kept. Personal email addresses, local paths and private IP addresses were replaced, and every commit uses the GitHub noreply author |
| Old objects | The old, now unreferenced commits were still reachable by their exact hash. A GitHub Support request asks for garbage collection and removal of cached views. No fork or pull request ever existed, so nothing else holds them |
| Identifiers | The Android package and the on-device helper class both use `com.mohitarora.copyclip` (the helper was renamed from the former `me.mohit.copyclip` on 2026-11-18, so the dex and the daemon must come from the same version) |
| Private files | `CLAUDE.md`, `.claude/` settings, `config.json`, keys, logs and runtime state are git-ignored. `.claude/skills/copyclip-feature` stays tracked on purpose |
| Contact | `contact@mohitarora.me`, listed in `SECURITY.md` and the README |
| GitHub settings | Email privacy and push blocking for private emails are on |

## Before every push

```bash
python tools/privacy_scan.py                 # working tree, must report 0 findings
python tools/privacy_scan.py --history       # history, one finding is expected: a fake key string in an old test
python tools/run_tests.py                    # unit tier, no devices
git ls-files | findstr /i "config.json .key .pem .pid"   # must print nothing
git config user.email                        # must be the GitHub noreply address, never a personal one
```

Show the staged diff, the commit message and the target branch to the owner and wait for a yes before `git commit` or `git push`.

## If history ever has to be rewritten again

History cannot be unpublished. Anyone who cloned before a rewrite keeps the old data, and old commits stay fetchable by their hash until GitHub collects them. Rewrite only for a real leak, rotate any exposed secret first, and ask GitHub Support to garbage collect afterwards.

## Housekeeping

- Rotate the shared key (Settings, then Rotate key) if there is any chance the old one was ever visible.
- Keep GitHub secret scanning on. Branch protection is optional for a one-person project.
- Screenshots in `docs/images/` are demo data from `python tools/demo_ui.py`. Retake them only when the UI changes.
- `docs/sessions/` are scrubbed working logs of the project.
