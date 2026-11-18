# Publishing CopyClip publicly

Status as of 2026-11-12: the GitHub repository is **private**. Nothing here has been done to it. This page is the checklist for the day you decide to open it up.

## 1. Decide things only the owner can decide

| Decision | Why it matters |
| :--- | :--- |
| **License** | Done: Apache-2.0. `LICENSE` and `NOTICE` are at the repo root and the README License section is updated |
| **Author name in identifiers** | The Android package and the on-device helper class both use `com.mohitarora.copyclip` (the helper was renamed from the former `me.mohit.copyclip` on 2026-11-18, so the dex and the daemon must come from the same version). They credit the author. Rename them only if you want the name out of the code |
| **Session logs** | `docs/sessions/` are working logs of the project. They were scrubbed of IPs and paths, but you may prefer not to publish them |
| **`CLAUDE.md` and `.claude/`** | `CLAUDE.md` is git-ignored because it points at a private skills library. `.claude/skills/copyclip-feature` is still tracked; remove it from tracking if you do not want it public |

## 2. Do not push the existing history as it is

The working tree is clean (`python tools/privacy_scan.py` reports 0), but git **history** still holds earlier versions of files with private Tailscale addresses and local paths (`python tools/privacy_scan.py --history` lists them). Anyone who clones the repository gets all of it.

Safest route, a fresh repository from a single snapshot:

```bash
# from a clean checkout of the branch you want to publish
git checkout --orphan public-main
git add -A
git status                      # confirm no config.json, *.key, logs/, state/ are listed
git commit -m "CopyClip 3.0"
# create a NEW empty GitHub repo, then:
git remote add public https://github.com/<you>/CopyClip.git
git push public public-main:main
```

Keep the old private repository as your archive. Rewriting history in place (git filter-repo) is possible but easier to get wrong, and every old clone and fork would still have the old data.

## 3. Final checks

```bash
python tools/privacy_scan.py --all --history   # history findings are expected only on the OLD repo
python tools/run_tests.py
git ls-files | findstr /i "config.json .key .pem .pid"   # must print nothing
```

Then read the README as a stranger would, and add screenshots to `docs/images/` and the README.

## 4. After publishing

- Rotate the shared key (Settings, then Rotate key) if there is any chance the old one was ever visible.
- Turn on GitHub secret scanning and branch protection for the new repository.
