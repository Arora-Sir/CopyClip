import os
import threading
import unittest
from unittest import mock

from tests import fakes  # noqa: F401
from tests.fakes import FakeClock
from windows_app import launcher


class DiscoveryTests(unittest.TestCase):
    def test_command_parsing(self):
        f = launcher.exe_from_command
        self.assertEqual(f('"C:\\Brave\\brave.exe" --single-argument %1'), "C:\\Brave\\brave.exe")
        self.assertEqual(f("C:\\x\\chrome.exe --flag"), "C:\\x\\chrome.exe")
        self.assertIsNone(f(""))
        self.assertIsNone(f("notepad"))

    def test_default_chromium_browser_first_and_isolated(self):
        c = launcher.browser_candidates("C:\\b\\brave.exe", exists=lambda p: True)
        self.assertEqual(c[0], ("C:\\b\\brave.exe", False))      # isolated profile first
        self.assertEqual(c[-1], ("C:\\b\\brave.exe", True))      # its normal profile only as a last resort

    def test_other_browsers_follow_and_never_duplicate_isolated_entries(self):
        c = launcher.browser_candidates("C:\\b\\brave.exe", exists=lambda p: True)
        isolated = [x.lower() for x, normal in c if not normal]
        self.assertEqual(len(isolated), len(set(isolated)))
        self.assertGreater(len(isolated), 1)

    def test_non_chromium_default_is_ignored(self):
        c = launcher.browser_candidates("C:\\f\\firefox.exe", exists=lambda p: True)
        self.assertNotIn("firefox", " ".join(x for x, _ in c).lower())
        self.assertTrue(all(not normal for _, normal in c))

    def test_nothing_installed(self):
        self.assertEqual(launcher.browser_candidates("C:\\b\\brave.exe", exists=lambda p: False), [])

    def test_registry_failure_is_not_fatal(self):
        def boom():
            raise OSError("no key")
        self.assertIsNone(launcher.default_browser_exe(read=boom))

    def test_find_edge(self):
        self.assertIsNone(launcher.find_edge(exists=lambda p: False))

    def test_args_and_profiles(self):
        a = launcher.edge_args("b.exe", "http://127.0.0.1:1/", user_data_dir="D:\\p", size=(10, 20))
        self.assertIn("--app=http://127.0.0.1:1/", a)
        self.assertIn("--user-data-dir=D:\\p", a)
        self.assertIn("--window-size=10,20", a)
        normal = launcher.edge_args("b.exe", "u", user_data_dir=None, size=(1, 2), position=(3, 4))
        self.assertFalse(any(x.startswith("--user-data-dir") for x in normal))
        self.assertIn("--window-position=3,4", normal)

    def test_profile_per_browser(self):
        a, b = launcher.profile_dir("C:\\x\\brave.exe"), launcher.profile_dir("C:\\x\\msedge.exe")
        self.assertNotEqual(a, b)
        self.assertIn(os.path.join("CopyClip", "ui-profile"), a)


class ProfilePrepTests(unittest.TestCase):
    def test_brave_profile_is_pre_acknowledged_once(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            prof = os.path.join(d, "brave")
            launcher.prepare_profile("C:\\x\\brave.exe", prof)
            state = os.path.join(prof, "Local State")
            data = json.load(open(state, encoding="utf-8"))
            self.assertTrue(data["brave"]["p3a"]["notice_acknowledged"])
            open(state, "w").write('{"user": "changed"}')
            launcher.prepare_profile("C:\\x\\brave.exe", prof)      # existing profile untouched
            self.assertEqual(json.load(open(state, encoding="utf-8")), {"user": "changed"})

    def test_other_browsers_are_left_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            launcher.prepare_profile("C:\\x\\msedge.exe", os.path.join(d, "e"))
            self.assertFalse(os.path.exists(os.path.join(d, "e")))

    def test_unwritable_location_is_not_fatal(self):
        with mock.patch("os.makedirs", side_effect=OSError("read only")):
            launcher.prepare_profile("C:\\x\\brave.exe", "Z:\\nope")


class FlyoutPositionTests(unittest.TestCase):
    WORK = (0, 0, 1920, 1040)

    def test_above_tray_clamped_right(self):
        x, y = launcher.flyout_position((1800, 1050), self.WORK, size=(380, 580))
        self.assertEqual((x, y), (1920 - 380 - 8, 1040 - 580 - 8))

    def test_clamped_left_and_second_monitor(self):
        self.assertEqual(launcher.flyout_position((10, 1050), self.WORK)[0], 8)
        self.assertGreaterEqual(launcher.flyout_position((2000, 1050), (1920, 0, 3840, 1040))[0], 1928)

    def test_taller_than_screen_stays_on_screen(self):
        self.assertEqual(launcher.flyout_position((100, 700), (0, 0, 800, 600), size=(380, 700))[1], 8)


class TitleTests(unittest.TestCase):
    def test_title_matching(self):
        m = launcher.title_matches
        self.assertTrue(m("CopyClip", "CopyClip"))
        self.assertTrue(m("CopyClip - Brave", "CopyClip"))
        self.assertFalse(m("CopyClip quick menu", "CopyClip"))
        self.assertFalse(m("My CopyClip notes - Edge", "CopyClip"))
        self.assertFalse(m("", "CopyClip"))
        self.assertTrue(m("CopyClip quick menu - Brave", "CopyClip quick menu"))

    def test_no_window_for_an_unlikely_title(self):
        self.assertFalse(launcher.focus_existing("CopyClip no such window 0xDEAD"))
        self.assertIsNone(launcher.find_window("CopyClip no such window 0xDEAD"))


class SingleInstanceTests(unittest.TestCase):
    def slot(self):
        self.clock = FakeClock(1000.0)
        return launcher.WindowSlot("CopyClip", clock=self.clock)

    def test_existing_window_is_focused_not_relaunched(self):
        launches = []
        r = self.slot().request(focus=lambda t: True, launch=lambda: launches.append(1) or True)
        self.assertEqual((r, launches), ("focused", []))

    def test_first_click_launches_and_later_clicks_are_ignored_while_opening(self):
        slot, launches = self.slot(), []
        self.assertEqual(slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True), "launched")
        for _ in range(10):   # impatient clicking while the window is still starting
            self.assertEqual(slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True), "launching")
        self.assertEqual(len(launches), 1)

    def test_click_after_window_appears_focuses_it(self):
        slot, launches = self.slot(), []
        slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True)
        self.assertEqual(slot.request(focus=lambda t: True, launch=lambda: launches.append(1) or True), "focused")
        self.assertEqual(len(launches), 1)

    def test_launch_allowed_again_after_the_settle_window(self):
        slot, launches = self.slot(), []
        slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True)
        self.clock.advance(launcher.LAUNCH_SETTLE_S + 1)
        slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True)
        self.assertEqual(len(launches), 2)

    def test_settled_reenables_launch(self):
        slot, launches = self.slot(), []
        slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True)
        slot.settled()
        slot.request(focus=lambda t: False, launch=lambda: launches.append(1) or True)
        self.assertEqual(len(launches), 2)

    def test_failed_launch_does_not_lock_the_slot(self):
        slot, calls = self.slot(), []
        self.assertEqual(slot.request(focus=lambda t: False, launch=lambda: calls.append(1) or False), "failed")
        self.assertEqual(slot.request(focus=lambda t: False, launch=lambda: calls.append(1) or True), "launched")
        self.assertEqual(len(calls), 2)

    def test_twenty_simultaneous_clicks_launch_exactly_once(self):
        slot = launcher.WindowSlot("CopyClip")
        launches, results = [], []
        gate = threading.Barrier(20)

        def click():
            gate.wait()
            results.append(slot.request(focus=lambda t: False,
                                        launch=lambda: (launches.append(1), True)[1]))
        threads = [threading.Thread(target=click) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(launches), 1)
        self.assertEqual(results.count("launched"), 1)
        self.assertEqual(results.count("launching"), 19)

    def test_main_and_flyout_are_independent(self):
        main, fly = launcher._SLOTS["main"], launcher._SLOTS["flyout"]
        self.assertIsNot(main, fly)
        self.assertNotEqual(main.title, fly.title)

    def test_open_ui_routes_main_and_flyout(self):
        class Srv:
            def launch_url(self, route=""):
                return "http://x/#/" + route
        urls = []
        for slot in launcher._SLOTS.values():
            slot.settled()
        with mock.patch.object(launcher, "focus_existing", return_value=False), \
                mock.patch.object(launcher, "open_window", side_effect=lambda u, **k: urls.append(u) or True):
            self.assertEqual(launcher.open_ui(Srv(), "devices"), "launched")
            self.assertEqual(launcher.open_ui(Srv(), flyout=True), "launched")
        self.assertEqual(urls, ["http://x/#/devices", "http://x/#/flyout"])
        for slot in launcher._SLOTS.values():
            slot.settled()


class EnsureWindowTests(unittest.TestCase):
    def proc(self, alive):
        p = mock.Mock(returncode=0)
        p.poll.return_value = None if alive else 0
        return p

    def run_ensure(self, proc, remaining, has_window, launched):
        clock = FakeClock(0.0)
        slot = launcher.WindowSlot("CopyClip", clock=clock)
        slot._launching_until = 99999.0
        with mock.patch.object(launcher, "_launch", side_effect=lambda *a, **k: launched.append(a[0]) or self.proc(False)), \
                mock.patch("webbrowser.open") as wb:
            launcher._ensure_window(proc, remaining, "http://x/", False, (1, 2), None, slot,
                                    sleep=clock.advance, clock=clock, has_window=has_window)
        return wb, slot

    def test_window_appears_slowly_nothing_else_is_launched(self):
        ticks = iter([False, False, False, True])
        launched = []
        wb, slot = self.run_ensure(self.proc(False), [("b.exe", False)], lambda: next(ticks, True), launched)
        self.assertEqual(launched, [])
        wb.assert_not_called()
        self.assertEqual(slot._launching_until, 0.0)       # settled

    def test_browser_still_starting_after_the_wait_is_left_alone(self):
        launched = []
        wb, _ = self.run_ensure(self.proc(True), [("b.exe", False)], lambda: False, launched)
        self.assertEqual(launched, [])
        wb.assert_not_called()

    def test_browser_that_died_without_a_window_escalates_in_order_then_default_browser(self):
        launched = []
        wb, slot = self.run_ensure(self.proc(False), [("b.exe", False), ("c.exe", True)], lambda: False, launched)
        self.assertEqual(launched, ["b.exe", "c.exe"])
        wb.assert_called_once_with("http://x/")
        self.assertEqual(slot._launching_until, 0.0)

    def test_escalation_stops_at_the_first_browser_that_works(self):
        launched = []
        wb, _ = self.run_ensure(self.proc(False), [("b.exe", False), ("c.exe", False)],
                                lambda: len(launched) >= 1, launched)   # window shows up after the first retry
        self.assertEqual(launched, ["b.exe"])
        wb.assert_not_called()


class WaitForWindowTests(unittest.TestCase):
    def proc(self, alive):
        p = mock.Mock()
        p.poll.return_value = None if alive else 0
        return p

    def test_browser_that_exited_without_a_window_fails_fast_not_after_the_full_wait(self):
        clock = FakeClock(0.0)
        state = launcher._wait_for_window(self.proc(False), "CopyClip", sleep=clock.advance, clock=clock,
                                          has_window=lambda: False)
        self.assertEqual(state, "gone")
        self.assertLess(clock(), launcher.WINDOW_WAIT_S / 2)

    def test_hand_off_launcher_that_exits_but_shows_the_window_within_the_grace_is_a_success(self):
        clock = FakeClock(0.0)
        state = launcher._wait_for_window(self.proc(False), "CopyClip", sleep=clock.advance, clock=clock,
                                          has_window=lambda: clock() >= launcher.GONE_GRACE_S - 0.5)
        self.assertEqual(state, "window")

    def test_still_running_browser_keeps_the_full_wait(self):
        clock = FakeClock(0.0)
        state = launcher._wait_for_window(self.proc(True), "CopyClip", sleep=clock.advance, clock=clock,
                                          has_window=lambda: False)
        self.assertEqual(state, "alive")
        self.assertGreaterEqual(clock(), launcher.WINDOW_WAIT_S)


class ProfileRotationTests(unittest.TestCase):
    def setUp(self):
        launcher._PROFILE_PROCS.clear()
        launcher._BAD_PROFILES.clear()
        self.addCleanup(launcher._PROFILE_PROCS.clear)
        self.addCleanup(launcher._BAD_PROFILES.clear)

    def proc(self, alive):
        p = mock.Mock()
        p.poll.return_value = None if alive else 0
        return p

    def test_slot_zero_keeps_the_original_folder_name(self):
        self.assertTrue(launcher.profile_dir("C:\\b\\brave.exe").endswith(os.path.join("ui-profile", "brave")))
        self.assertTrue(launcher.profile_dir("C:\\b\\brave.exe", 1).endswith(os.path.join("ui-profile", "brave-2")))

    def test_first_free_slot_is_used(self):
        self.assertEqual(launcher.pick_profile("C:\\b\\brave.exe"), launcher.profile_dir("C:\\b\\brave.exe", 0))

    def test_a_slot_whose_last_browser_is_still_closing_is_skipped(self):
        exe = "C:\\b\\brave.exe"
        launcher._PROFILE_PROCS[launcher.profile_dir(exe, 0)] = self.proc(alive=True)
        self.assertEqual(launcher.pick_profile(exe), launcher.profile_dir(exe, 1))

    def test_a_slot_whose_browser_has_ended_is_reused(self):
        exe = "C:\\b\\brave.exe"
        launcher._PROFILE_PROCS[launcher.profile_dir(exe, 0)] = self.proc(alive=False)
        self.assertEqual(launcher.pick_profile(exe), launcher.profile_dir(exe, 0))

    def test_a_slot_marked_bad_is_skipped_until_it_expires(self):
        exe = "C:\\b\\brave.exe"
        clock = FakeClock(0.0)
        launcher.mark_profile_bad(launcher.profile_dir(exe, 0), clock=clock)
        self.assertEqual(launcher.pick_profile(exe, clock=clock), launcher.profile_dir(exe, 1))
        clock.advance(launcher.BAD_PROFILE_S + 1)
        self.assertEqual(launcher.pick_profile(exe, clock=clock), launcher.profile_dir(exe, 0))

    def test_all_slots_busy_falls_back_to_slot_zero(self):
        exe = "C:\\b\\brave.exe"
        for slot in range(launcher.PROFILE_SLOTS):
            launcher._PROFILE_PROCS[launcher.profile_dir(exe, slot)] = self.proc(alive=True)
        self.assertEqual(launcher.pick_profile(exe), launcher.profile_dir(exe, 0))

    def test_a_hand_off_launcher_does_not_replace_the_running_process_that_owns_the_profile(self):
        owner, handoff = self.proc(alive=True), self.proc(alive=False)
        launcher._remember_launch("p", owner)
        launcher._remember_launch("p", handoff)
        self.assertIs(launcher._PROFILE_PROCS["p"], owner)

    def test_a_finished_process_is_replaced_by_the_next_launch(self):
        old, new = self.proc(alive=False), self.proc(alive=True)
        launcher._remember_launch("p", old)
        launcher._remember_launch("p", new)
        self.assertIs(launcher._PROFILE_PROCS["p"], new)


class RetryOnAnotherProfileTests(unittest.TestCase):
    def setUp(self):
        launcher._BAD_PROFILES.clear()
        self.addCleanup(launcher._BAD_PROFILES.clear)

    def run_ensure(self, has_window, retry, remaining=()):
        clock = FakeClock(0.0)
        slot = launcher.WindowSlot("CopyClip", clock=clock)
        slot._launching_until = 99999.0
        launched = []
        dead = mock.Mock()
        dead.poll.return_value = 0
        dead.copyclip_profile = "profile-1"

        def fake_launch(*a, **k):
            launched.append(a[0])
            return dead
        with mock.patch.object(launcher, "_launch", side_effect=fake_launch),                 mock.patch.object(launcher, "mark_profile_bad") as bad,                 mock.patch("webbrowser.open") as wb:
            launcher._ensure_window(dead, list(remaining), "http://x/", False, (1, 2), None, slot, retry=retry,
                                    sleep=clock.advance, clock=clock, has_window=lambda: has_window(launched))
        return launched, bad, wb, slot

    def test_exit_without_window_marks_the_profile_bad_and_restarts_the_same_browser_once(self):
        launched, bad, wb, slot = self.run_ensure(lambda l: len(l) >= 1, ("brave.exe", False),
                                                  remaining=[("edge.exe", False)])
        self.assertEqual(launched, ["brave.exe"])          # same browser again, Edge never tried
        bad.assert_called_once_with("profile-1")
        wb.assert_not_called()
        self.assertEqual(slot._launching_until, 0.0)

    def test_retry_that_also_fails_falls_through_to_the_next_browser(self):
        launched, bad, wb, _ = self.run_ensure(lambda l: False, ("brave.exe", False),
                                               remaining=[("edge.exe", False)])
        self.assertEqual(launched, ["brave.exe", "edge.exe"])
        wb.assert_called_once_with("http://x/")

    def test_normal_profile_launch_is_never_retried_or_marked(self):
        launched, bad, _, _ = self.run_ensure(lambda l: False, ("brave.exe", True))
        self.assertEqual(launched, [])
        bad.assert_not_called()


class OpenWindowTests(unittest.TestCase):
    def test_no_browser_uses_default_browser_tab(self):
        with mock.patch("webbrowser.open", return_value=True) as wb:
            self.assertTrue(launcher.open_window("http://x/", candidates=[]))
        wb.assert_called_once_with("http://x/")

    def test_oserror_moves_to_next_candidate_then_default_browser(self):
        with mock.patch.object(launcher, "_launch", side_effect=OSError("nope")) as ln, \
                mock.patch("webbrowser.open", return_value=True) as wb:
            self.assertTrue(launcher.open_window("u", candidates=[("a.exe", False), ("b.exe", False)]))
        self.assertEqual(ln.call_count, 2)
        wb.assert_called_once()

    def test_first_working_candidate_starts_a_watcher_with_the_rest(self):
        proc = mock.Mock(pid=7)
        with mock.patch.object(launcher, "_launch", return_value=proc), \
                mock.patch.object(launcher.threading, "Thread") as th:
            self.assertTrue(launcher.open_window("u", candidates=[("a.exe", False), ("b.exe", True)]))
        args = th.call_args.kwargs["args"]
        self.assertEqual(args[1], [("b.exe", True)])
        th.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
