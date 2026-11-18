import unittest

from tests import fakes  # noqa: F401  (sets sys.path)
from transports import framing
from transports.framing import AuthError

KEY = b"k" * 32


class FramingTests(unittest.TestCase):
    def test_roundtrip(self):
        raw = framing.seal(KEY, 1, "SET hello")
        self.assertEqual(framing.open_frame(KEY, raw, 0), (1, "SET hello"))

    def test_wire_format_is_seq_mac_payload(self):
        seq, mac, payload = framing.seal(KEY, 7, "PING").split(" ", 2)
        self.assertEqual((seq, payload), ("7", "PING"))
        self.assertEqual(len(mac), 64)

    def test_empty_payload(self):
        raw = framing.seal(KEY, 1, "")
        self.assertEqual(framing.open_frame(KEY, raw, 0), (1, ""))

    def test_unicode_and_spaces_payload(self):
        payload = "CLIP مرحبا \U0001F600 a  b"
        raw = framing.seal(KEY, 3, payload)
        self.assertEqual(framing.open_frame(KEY, raw, 2)[1], payload)

    def test_replay_rejected(self):
        raw = framing.seal(KEY, 5, "x")
        with self.assertRaises(AuthError):
            framing.open_frame(KEY, raw, 5)

    def test_reorder_rejected(self):
        raw = framing.seal(KEY, 2, "x")
        with self.assertRaises(AuthError):
            framing.open_frame(KEY, raw, 9)

    def test_bad_mac_rejected(self):
        raw = framing.seal(KEY, 1, "x")
        with self.assertRaises(AuthError):
            framing.open_frame(b"z" * 32, raw, 0)

    def test_tampered_payload_rejected(self):
        raw = framing.seal(KEY, 1, "SET a")
        with self.assertRaises(AuthError):
            framing.open_frame(KEY, raw[:-1] + "b", 0)

    def test_malformed_frames(self):
        for raw in ("", "justone", "abc def ghi", "-"):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthError):
                    framing.open_frame(KEY, raw, 0)

    def test_non_integer_seq(self):
        with self.assertRaises(AuthError):
            framing.open_frame(KEY, "x " + "0" * 64 + " p", 0)

    def test_challenge(self):
        nonce = framing.new_nonce()
        good = framing.challenge_response(KEY, nonce)
        self.assertTrue(framing.verify_challenge(KEY, nonce, good))
        self.assertFalse(framing.verify_challenge(KEY, nonce, "0" * 64))
        self.assertFalse(framing.verify_challenge(b"z" * 32, nonce, good))
        self.assertNotEqual(framing.new_nonce(), framing.new_nonce())


if __name__ == "__main__":
    unittest.main()
