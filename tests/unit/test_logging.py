import io
import logging
import os
import tempfile
import unittest

from tests import fakes  # noqa: F401
from tests.fakes import FakeClock
from core.logsetup import FoldingHandler, setup_logging


def make(clock=None, window=60.0):
    stream = io.StringIO()
    inner = logging.StreamHandler(stream)
    inner.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler = FoldingHandler(inner, window_s=window, clock=clock or FakeClock())
    logger = logging.Logger("t")
    logger.addHandler(handler)
    return logger, handler, stream


def lines(stream):
    return [x for x in stream.getvalue().splitlines() if x]


class LoggingEnabled(unittest.TestCase):
    """tests/unit silences logging globally; these tests need real log output."""

    def setUp(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)


class FoldingTests(LoggingEnabled):
    def test_identical_repeats_fold_into_one_summary(self):
        log, h, out = make()
        for _ in range(1000):
            log.error("write failed")
        log.info("recovered")
        self.assertEqual(lines(out), ["ERROR write failed",
                                      "ERROR (previous message repeated 999 more times)",
                                      "INFO recovered"])

    def test_different_messages_all_pass(self):
        log, h, out = make()
        for i in range(3):
            log.warning("m%d", i)
        self.assertEqual(len(lines(out)), 3)

    def test_same_text_different_level_is_not_folded(self):
        log, h, out = make()
        log.warning("x")
        log.error("x")
        self.assertEqual(len(lines(out)), 2)

    def test_window_expiry_re_emits(self):
        clock = FakeClock()
        log, h, out = make(clock, window=60)
        log.error("down")
        log.error("down")
        clock.advance(61)
        log.error("down")
        self.assertEqual(lines(out), ["ERROR down",
                                      "ERROR (previous message repeated 1 more times)",
                                      "ERROR down"])

    def test_repeated_tracebacks_fold(self):
        log, h, out = make()
        for _ in range(50):
            try:
                raise OSError("denied")
            except OSError:
                log.exception("clipboard write failed")
        h.flush()
        text = out.getvalue()
        self.assertEqual(text.count("Traceback"), 1)
        self.assertIn("repeated 49 more times", text)

    def test_flush_emits_pending_summary(self):
        log, h, out = make()
        log.error("a")
        log.error("a")
        h.flush()
        self.assertIn("repeated 1 more times", out.getvalue())
        h.flush()
        self.assertEqual(out.getvalue().count("repeated"), 1)  # not twice

    def test_interleaved_pattern_never_hides_a_new_message(self):
        log, h, out = make()
        for msg in ("a", "a", "b", "a"):
            log.error(msg)
        self.assertEqual(lines(out), ["ERROR a", "ERROR (previous message repeated 1 more times)",
                                      "ERROR b", "ERROR a"])


class SetupTests(LoggingEnabled):
    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)

    def test_creates_dir_and_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "nested", "logs")
            setup_logging("INFO", target)
            logging.getLogger("x").info("hello")
            for h in logging.getLogger().handlers:
                h.flush()
            with open(os.path.join(target, "copyclip.log"), encoding="utf-8") as f:
                self.assertIn("hello", f.read())
            self.tearDown()

    def test_unknown_level_falls_back_to_info(self):
        with tempfile.TemporaryDirectory() as d:
            setup_logging("NOPE", d)
            self.assertEqual(logging.getLogger().level, logging.INFO)
            self.tearDown()

    def test_no_console_handler_without_stdout(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            real = sys.stdout
            sys.stdout = None
            try:
                setup_logging("INFO", d)
            finally:
                sys.stdout = real
            self.assertEqual(len(logging.getLogger().handlers), 1)
            self.tearDown()


if __name__ == "__main__":
    unittest.main()
