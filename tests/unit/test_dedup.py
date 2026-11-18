import unittest
from unittest import mock

from tests.fakes import FakeClock
from core.dedup import RecentClips


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = mock.patch("core.dedup.time.time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_first_observation_is_new_second_is_echo(self):
        d = RecentClips(ttl=90)
        self.assertTrue(d.observe("h", "phone"))
        self.assertFalse(d.observe("h", "tab"))

    def test_should_deliver_per_node(self):
        d = RecentClips()
        d.observe("h", "phone")
        self.assertFalse(d.should_deliver("h", "phone"))
        self.assertTrue(d.should_deliver("h", "pixel"))
        d.mark_delivered("h", "pixel")
        self.assertFalse(d.should_deliver("h", "pixel"))

    def test_unknown_hash_always_deliverable(self):
        self.assertTrue(RecentClips().should_deliver("nope", "phone"))

    def test_ttl_expiry(self):
        d = RecentClips(ttl=90)
        d.observe("h", "phone")
        self.clock.advance(89)
        self.assertTrue(d.known("h"))
        self.clock.advance(2)
        self.assertFalse(d.known("h"))
        self.assertTrue(d.observe("h", "phone"))  # genuinely new again

    def test_ttl_counts_from_first_seen_not_last_seen(self):
        # Characterisation: a clip that keeps being re-observed still expires.
        d = RecentClips(ttl=90)
        d.observe("h", "phone")
        self.clock.advance(60)
        d.observe("h", "tab")
        self.clock.advance(40)
        self.assertFalse(d.known("h"))

    def test_max_entries_evicts_oldest(self):
        d = RecentClips(ttl=1e9, max_entries=3)
        for i in range(6):
            d.observe(f"h{i}", "phone")
            self.clock.advance(1)
        d.known("x")  # triggers prune
        self.assertFalse(d.known("h0"))
        self.assertTrue(d.known("h5"))

    def test_is_recopy_requires_holder_and_interval(self):
        d = RecentClips()
        d.observe("h", "phone")
        self.assertFalse(d.is_recopy("h", "phone"))
        self.clock.advance(0.5)
        self.assertTrue(d.is_recopy("h", "phone"))
        self.assertFalse(d.is_recopy("h", "tab"))
        self.assertFalse(d.is_recopy("unknown", "phone"))

    def test_prepare_for_redelivery(self):
        d = RecentClips()
        d.observe("h", "phone")
        d.mark_delivered("h", "tab")
        d.prepare_for_redelivery("h", "phone")
        self.assertTrue(d.should_deliver("h", "tab"))
        self.assertFalse(d.should_deliver("h", "phone"))

    def test_origin_and_snapshot(self):
        d = RecentClips()
        d.observe("h", "phone")
        self.assertEqual(d.origin_of("h"), "phone")
        self.assertIsNone(d.origin_of("x"))
        self.assertEqual(d.snapshot()["h"]["delivered_to"], ["phone"])


if __name__ == "__main__":
    unittest.main()
