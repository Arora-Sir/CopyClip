import os
import subprocess
import tempfile
import unittest

from tests import fakes  # noqa: F401
from tools import privacy_scan as ps


def kinds(text, terms=()):
    return [f.kind for f in ps.scan_text("x.md", text, terms)]


class PatternTests(unittest.TestCase):
    def test_tailscale_and_lan_addresses_flagged(self):
        for ip in ("100.101.102.103", "100.64.9.9", "100.127.255.1", "192.168.1.20", "10.1.2.3", "172.16.0.9"):
            with self.subTest(ip=ip):
                self.assertIn("private/Tailscale IP address", kinds(f"device {ip}:5555"))

    def test_placeholders_and_public_ips_pass(self):
        for ip in ("100.64.0.11", "10.0.0.5", "127.0.0.1", "0.0.0.0", "8.8.8.8", "192.0.2.4", "203.0.113.9"):
            with self.subTest(ip=ip):
                self.assertEqual(kinds(f"host {ip}"), [])

    def test_version_numbers_and_non_ips_pass(self):
        for text in ("version 3.14.3.1", "build 1.2.3.4.5", "value 999.1.1.1", "x 12.34"):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_windows_profile_paths(self):
        for path in (r"C:\Users\alice\Pictures", "C:/Users/bob/x", r"D:\\Users\\carol\\y", r"c:\users\Dave\z"):
            with self.subTest(path=path):
                self.assertIn("Windows user profile path", kinds(path))
        for path in (r"C:\Users\you\Pictures", r"C:\Users\<user>\x", r"C:\Users\Public\Docs",
                     r"C:\Users\%USERNAME%\x", r"C:\Users\Default\x"):
            with self.subTest(path=path):
                self.assertEqual(kinds(path), [])

    def test_emails(self):
        self.assertIn("email address", kinds("write to someone@company.com"))
        self.assertEqual(kinds("a@example.com 123+x@users.noreply.github.com"), [])

    def test_secrets(self):
        samples = {
            "private key block": "-----BEGIN RSA PRIVATE KEY-----",
            "GitHub token": "token ghp_" + "a1B2c3D4e5" * 4,
            "AWS access key": "AKIA" + "ABCDEFGHIJKLMNOP",
            "Slack token": "xoxb-1234567890-abcdefghij",
            "hex secret assigned to a key-like name": "shared_key = '" + "ab12" * 16 + "'",
        }
        for kind, line in samples.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds(line))

    def test_secret_values_never_echoed(self):
        out = ps.scan_text("x", "api_key = " + "ab12" * 16)
        self.assertTrue(out)
        self.assertNotIn("ab12ab12", " ".join(str(f) for f in out))

    def test_short_hex_is_not_a_secret(self):
        self.assertEqual(kinds("key = 'abc123'"), [])

    def test_marker_suppresses_a_reviewed_line(self):
        self.assertEqual(kinds("100.101.102.103  # privacy-ok: documentation"), [])

    def test_personal_terms_are_case_insensitive(self):
        self.assertEqual(kinds("Hello JANE doe", terms=("jane",)), ["personal term from .privacy-terms"])
        self.assertEqual(kinds("nothing here", terms=("jane",)), [])

    def test_line_numbers(self):
        f = ps.scan_text("x", "ok\nok\nip 100.101.102.103\n")[0]
        self.assertEqual(f.line, 3)

    def test_binary_and_unscannable_files_skipped(self):
        self.assertFalse(ps._scannable("bin/clipbridge.dex"))
        self.assertFalse(ps._scannable("otherCopyProjects/x/readme.md"))
        self.assertFalse(ps._scannable("tests/unit/test_privacy_scan.py"))
        self.assertTrue(ps._scannable("docs/bugs/BUG-001.md"))


class FileScanTests(unittest.TestCase):
    def test_scan_files_reads_real_files_and_skips_huge_ones(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.md"), "w", encoding="utf-8") as f:
                f.write("see C:\\Users\\alice\\x\n")
            with open(os.path.join(d, "big.md"), "w", encoding="utf-8") as f:
                f.write("100.101.102.103\n" * 10)
            ps_max = ps.MAX_BYTES
            try:
                ps.MAX_BYTES = 50
                found = ps.scan_files(["a.md", "big.md", "missing.md"], root=d)
            finally:
                ps.MAX_BYTES = ps_max
        self.assertEqual([f.path for f in found], ["a.md"])


class RepositoryIsCleanTests(unittest.TestCase):
    """The guard that matters: nothing tracked may carry personal data or secrets."""

    def test_tracked_files_have_no_findings(self):
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ps.ROOT, capture_output=True).returncode:
            self.skipTest("not a git checkout")
        found = ps.scan_files(ps.candidate_files(), extra_terms=ps.load_terms())
        self.assertEqual(found, [], "\n" + "\n".join(str(f) for f in found[:25]))

    def test_personal_files_are_ignored_by_git(self):
        for path in ("config.json", ".privacy-terms", "copyclip.key", "copyclip_tls.key",
                     "state/outbox.json", "logs/copyclip.log", "state/control.json"):
            r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ps.ROOT)
            self.assertEqual(r.returncode, 0, f"{path} is not git-ignored")

    def test_example_config_is_valid_and_has_only_placeholders(self):
        import json
        from core import config
        with open(os.path.join(ps.ROOT, "config.example.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(config.validate(config.with_defaults({k: v for k, v in raw.items() if not k.startswith("_")})), [])
        self.assertEqual(ps.scan_text("config.example.json", json.dumps(raw, indent=1)), [])


if __name__ == "__main__":
    unittest.main()
