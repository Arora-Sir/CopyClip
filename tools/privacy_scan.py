"""Scan for personal data and secrets before anything is published.

    python tools/privacy_scan.py                # every git-tracked file, plus staged additions
    python tools/privacy_scan.py --all          # also untracked files that are not ignored
    python tools/privacy_scan.py --history      # also every file version in git history (slow)
    python tools/privacy_scan.py path1 path2    # specific files

Exit status 1 if anything is found, so it can run as a pre-commit hook or in CI.

Personal terms (your name, email, device serials) are not hard-coded here, because that would
leak them. Put them one per line in `.privacy-terms` (git-ignored) and they are scanned too.
Add the marker `privacy-ok` to a line to accept a reviewed false positive.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXT = {".py", ".md", ".txt", ".json", ".ps1", ".bat", ".vbs", ".java", ".kt", ".kts", ".xml",
            ".gradle", ".properties", ".js", ".css", ".html", ".toml", ".cfg", ".ini", ".yml",
            ".yaml", ".sh", ".aidl", ".tsv", ""}
SKIP_DIRS = {".git", "otherCopyProjects", "graphify-out", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_FILES = {"privacy_scan.py", "test_privacy_scan.py"}   # they contain the patterns on purpose
MAX_BYTES = 2_000_000

PLACEHOLDER_IPS = ("100.64.0.", "192.0.2.", "198.51.100.", "203.0.113.", "10.0.0.", "127.", "0.0.0.0")
PLACEHOLDER_USERS = {"you", "user", "username", "<user>", "yourname", "public", "default", "name",
                     "%username%", "<you>", "x", "me"}
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "users.noreply.github.com", "noreply.github.com")

IP_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
WIN_USER_RE = re.compile(r"[A-Za-z]:[\\/]+(?:\\\\)?Users[\\/]+(?:\\\\)?([^\\/\s\"'<>|:*?]+)", re.I)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
SECRET_RES = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("hex secret assigned to a key-like name",
     re.compile(r"(?i)\b\w*(?:key|secret|token|passw(?:or)?d)\w*\s*[:=]\s*[\"']?[0-9a-f]{40,}[\"']?")),
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.excerpt}"


def _is_public_or_placeholder_ip(text: str) -> bool:
    if text.startswith(PLACEHOLDER_IPS):
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True   # not an IP (a version number such as 1.2.3.4 can still be one; handled below)
    return not (ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"))


def scan_text(path: str, text: str, extra_terms: tuple[str, ...] = ()) -> list[Finding]:
    out: list[Finding] = []
    terms = [t for t in extra_terms if t]
    for n, line in enumerate(text.splitlines(), 1):
        if "privacy-ok" in line:
            continue
        for m in IP_RE.finditer(line):
            ip = m.group(1)
            if all(0 <= int(p) <= 255 for p in ip.split(".")) and not _is_public_or_placeholder_ip(ip):
                out.append(Finding(path, n, "private/Tailscale IP address", ip))
        for m in WIN_USER_RE.finditer(line):
            user = m.group(1)
            if user.lower().strip("<>%") not in PLACEHOLDER_USERS and user.lower() not in PLACEHOLDER_USERS:
                out.append(Finding(path, n, "Windows user profile path", m.group(0)))
        for m in EMAIL_RE.finditer(line):
            if not m.group(0).lower().endswith(SAFE_EMAIL_DOMAINS):
                out.append(Finding(path, n, "email address", m.group(0)))
        for kind, rx in SECRET_RES:
            if rx.search(line):
                out.append(Finding(path, n, kind, "(value hidden)"))
        low = line.lower()
        for term in terms:
            if term.lower() in low:
                out.append(Finding(path, n, "personal term from .privacy-terms", term))
    return out


def load_terms(root: str = ROOT) -> tuple[str, ...]:
    path = os.path.join(root, ".privacy-terms")
    try:
        with open(path, encoding="utf-8") as fh:
            return tuple(x.strip() for x in fh if x.strip() and not x.startswith("#"))
    except OSError:
        return ()


def _git(*args: str, root: str = ROOT) -> list[str]:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x]


def candidate_files(root: str = ROOT, include_untracked: bool = False) -> list[str]:
    files = set(_git("ls-files", root=root))
    if include_untracked:
        files |= set(_git("ls-files", "--others", "--exclude-standard", root=root))
    return sorted(f for f in files if _scannable(f))


def _scannable(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if parts[0] in SKIP_DIRS or any(p in SKIP_DIRS for p in parts):
        return False
    if parts[-1] in SKIP_FILES:
        return False
    return os.path.splitext(parts[-1])[1].lower() in TEXT_EXT


def scan_files(rels: list[str], root: str = ROOT, extra_terms: tuple[str, ...] = ()) -> list[Finding]:
    findings: list[Finding] = []
    for rel in rels:
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > MAX_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                findings.extend(scan_text(rel.replace("\\", "/"), fh.read(), extra_terms))
        except OSError:
            continue
    return findings


def scan_history(root: str = ROOT, extra_terms: tuple[str, ...] = ()) -> list[Finding]:
    """Every added line in every commit. Slow on big repos, which is why it is opt-in."""
    findings: list[Finding] = []
    log = subprocess.run(["git", "log", "--all", "-p", "--no-color", "--unified=0", "--format=commit %h"],
                         cwd=root, capture_output=True, text=True, errors="replace").stdout
    commit, path = "", ""
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and _scannable(path):
            for f in scan_text(f"{path}@{commit}", line[1:], extra_terms):
                findings.append(Finding(f.path, 0, f.kind, f.excerpt))
    return sorted(set(findings), key=lambda f: (f.path, f.kind, f.excerpt))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--all", action="store_true", help="include untracked, non-ignored files")
    ap.add_argument("--history", action="store_true", help="scan every commit as well")
    args = ap.parse_args()

    terms = load_terms()
    rels = args.paths or candidate_files(include_untracked=args.all)
    findings = scan_files(rels, extra_terms=terms)
    if args.history:
        findings += scan_history(extra_terms=terms)
    for f in findings:
        print(f)
    scope = "history and working tree" if args.history else "working tree"
    print(f"\n{len(findings)} finding(s) in {len(rels)} file(s) ({scope})"
          + ("" if terms else "; no .privacy-terms file, so personal terms were not checked"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
