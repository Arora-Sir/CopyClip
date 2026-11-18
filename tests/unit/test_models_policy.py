import unittest

from tests import fakes  # noqa: F401
from core.models import ClipPayload
from core.policy import Policy


def text(s, **kw):
    return ClipPayload.from_text(s, "phone", **kw)


class ModelTests(unittest.TestCase):
    def test_hash_is_content_only(self):
        a = ClipPayload(mime="text/plain", data=b"x", origin="a")
        b = ClipPayload(mime="text/html", data=b"x", origin="b")
        self.assertEqual(a.sha256, b.sha256)

    def test_dict_roundtrip(self):
        p = ClipPayload(mime="image/png", data=b"\x00\xff", origin="tab",
                        sensitive=True, is_camera=True)
        q = ClipPayload.from_dict(p.to_dict())
        self.assertEqual((q.mime, q.data, q.origin, q.sensitive, q.is_camera),
                         (p.mime, p.data, p.origin, True, True))

    def test_from_dict_missing_key_raises(self):
        with self.assertRaises(KeyError):
            ClipPayload.from_dict({"mime": "text/plain"})

    def test_unicode_char_len_vs_size(self):
        p = text("é\U0001F600")
        self.assertEqual(p.char_len, 2)
        self.assertEqual(p.size, 6)

    def test_invalid_utf8_text_is_replaced_not_raised(self):
        p = ClipPayload(mime="text/plain", data=b"\xff\xfe", origin="x")
        self.assertIn("�", p.text)

    def test_crlf_and_lf_hash_differently_today(self):
        # Characterisation of a known gap: no newline normalisation exists (plan Phase 1).
        self.assertNotEqual(text("a\r\nb").sha256, text("a\nb").sha256)

    def test_embedded_nul_and_rtl_survive(self):
        s = "a\x00b שלום"
        self.assertEqual(text(s).text, s)

    def test_non_text_has_no_text(self):
        self.assertIsNone(ClipPayload(mime="image/png", data=b"1", origin="x").text)


class PolicyTests(unittest.TestCase):
    def test_empty_rejected(self):
        self.assertEqual(Policy().reject_reason(text("")), "empty")

    def test_max_chars_boundary(self):
        p = Policy(max_chars=5)
        self.assertIsNone(p.reject_reason(text("12345")))
        self.assertIn("too large", p.reject_reason(text("123456")))

    def test_sensitive_gate(self):
        s = text("123456", sensitive=True)
        self.assertIn("sensitive", Policy().reject_reason(s))
        self.assertIsNone(Policy(allow_sensitive=True).reject_reason(s))

    def test_images_gate_and_cap(self):
        img = ClipPayload(mime="image/png", data=b"x" * 10, origin="x")
        self.assertIn("disabled", Policy().reject_reason(img))
        self.assertIsNone(Policy(allow_images=True).reject_reason(img))
        self.assertIn("too large", Policy(allow_images=True, max_image_bytes=9).reject_reason(img))
        self.assertIsNone(Policy(allow_images=True, max_image_bytes=10).reject_reason(img))

    def test_non_text_gate(self):
        p = ClipPayload(mime="application/pdf", data=b"x", origin="x")
        self.assertIn("non-text", Policy().reject_reason(p))
        self.assertIsNone(Policy(allow_non_text=True).reject_reason(p))

    def test_html_is_text(self):
        self.assertIsNone(Policy().reject_reason(text("<b>x</b>", mime="text/html")))

    def test_denylist(self):
        pol = Policy(denylist_regex=[r"^sk-\w+"])
        self.assertIn("denylist", pol.reject_reason(text("sk-abc123")))
        self.assertIsNone(pol.reject_reason(text("hello")))

    def test_denylist_ignored_for_images(self):
        pol = Policy(allow_images=True, denylist_regex=[".*"])
        self.assertIsNone(pol.reject_reason(ClipPayload(mime="image/png", data=b"x", origin="x")))


if __name__ == "__main__":
    unittest.main()
