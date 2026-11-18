import logging
import os
import sys
import tempfile
import threading
import unittest

from core import logsetup


class CrashDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        self.addCleanup(self.restore)
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        self.logs = os.path.join(self.tmp.name, "logs")
        self.state = os.path.join(self.tmp.name, "state")

    def restore(self):
        import faulthandler
        faulthandler.disable()
        sys.excepthook, threading.excepthook, sys.unraisablehook = self.saved
        if logsetup._CRASH_FILE:
            logsetup._CRASH_FILE.close()
            logsetup._CRASH_FILE = None

    def test_first_start_is_not_abrupt_and_leaves_marker(self):
        self.assertFalse(logsetup.install_crash_diagnostics(self.logs, self.state))
        self.assertTrue(os.path.exists(os.path.join(self.state, "running.marker")))
        self.assertTrue(os.path.exists(os.path.join(self.logs, "crash.log")))

    def test_leftover_marker_means_previous_run_died(self):
        os.makedirs(self.state)
        open(os.path.join(self.state, "running.marker"), "w").write("123")
        with self.assertLogs("copyclip.crash", level="WARNING") as cm:
            self.assertTrue(logsetup.install_crash_diagnostics(self.logs, self.state))
        self.assertIn("without a clean shutdown", cm.output[0])

    def test_thread_exception_is_logged_with_traceback(self):
        logsetup.install_crash_diagnostics(self.logs, self.state)
        with self.assertLogs("copyclip.crash", level="CRITICAL") as cm:
            def boom():
                raise ValueError("kaboom")
            t = threading.Thread(target=boom, name="probe")
            t.start()
            t.join()
        self.assertIn("probe", cm.output[0])
        self.assertIn("kaboom", cm.output[0])


if __name__ == "__main__":
    unittest.main()
