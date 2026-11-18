# Security

## Reporting a problem

If you find a way to read, inject or alter someone's clipboard through CopyClip, please report it privately to the maintainer instead of opening a public issue. Include the steps to reproduce and the Windows and Android versions involved.

## Trust model in one paragraph

CopyClip has no server. It moves clips between your own devices over a private Tailscale network. Tailscale (WireGuard) protects traffic in transit, and CopyClip adds its own authorisation on top: any socket that carries clipboard data first completes a mutual HMAC-SHA256 challenge against a shared key, and framed messages carry a strictly increasing sequence number inside the MAC so a captured frame cannot be replayed. The on-device helper runs as Android's shell user and never as root.

## What is deliberately not in the repository

| Never committed | Where it lives instead |
| :--- | :--- |
| `copyclip.key` (shared HMAC key) and every `*.key`, `*.pem` | your PC, git-ignored |
| `config.json` (device addresses, folders) | your PC, git-ignored. Start from `config.example.json` |
| `logs/`, `state/` (outbox, baseline, control file) | your PC, git-ignored. They can contain clip content |
| `.privacy-terms` (your personal strings for the scanner) | your PC, git-ignored |

`python tools/privacy_scan.py` checks tracked files for private IP addresses, personal profile paths, email addresses, token formats and private key blocks. A unit test runs it, so a commit that adds any of those fails the test suite. Run `python tools/privacy_scan.py --history` before making a repository public: history keeps old versions of files.

## The window's local API

The window is a browser talking to the tray over loopback. The rules that keep other software on the machine out are tested in `tests/unit/test_control_api.py`:

- listens on `127.0.0.1` only, on a random port;
- every API call needs a random per-run token (an HttpOnly, SameSite=Strict cookie, or a header);
- the `Host` header must be this server's own address, which blocks DNS rebinding;
- state-changing calls must send JSON and, when using the cookie, a matching `Origin`, which blocks cross-site form posts;
- only a fixed list of static files is served, and folder-opening endpoints accept a name from a fixed set, never a path.

The token is also stored in `state/control.json` for `copyclip.py --status`, readable by the same Windows user only. Any program running as you can read it, which is the same exposure as the clipboard itself.

## Known limits

- Any program running as your Windows user can read the Windows clipboard while a sensitive clip is on it. The auto-clear timer shortens that window and does not remove it.
- The shared key is stored as text in `copyclip.key`. Protect that file as you would a password.
