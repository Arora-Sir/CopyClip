"""winclip.read_text retries a NULL handle for an available format, and nothing else.

Measured 2026-11-18: about 1.4 percent of write then read cycles saw Windows report
CF_UNICODETEXT as available while GetClipboardData returned NULL (err 1418). These tests use
fakes, so they never touch the real clipboard.
"""

from __future__ import annotations

import contextlib
import ctypes
import unittest
from unittest import mock

import winclip


class _FakeUser32:
    def __init__(self, available: bool, handles: list[int]) -> None:
        self.available = available
        self.handles = list(handles)
        self.get_calls = 0

    def IsClipboardFormatAvailable(self, fmt: int) -> bool:
        return self.available

    def GetClipboardData(self, fmt: int) -> int:
        self.get_calls += 1
        return self.handles.pop(0) if self.handles else 0


class _FakeKernel32:
    def __init__(self, text: str) -> None:
        self._buf = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\x00\x00")

    def GlobalLock(self, handle: int) -> int:
        return ctypes.addressof(self._buf)

    def GlobalSize(self, handle: int) -> int:
        return len(self._buf)

    def GlobalUnlock(self, handle: int) -> int:
        return 1


@contextlib.contextmanager
def _no_open():
    yield


class ReadTextRetryTests(unittest.TestCase):
    def _patched(self, user32: _FakeUser32, text: str = "hello"):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(winclip, "user32", user32))
        stack.enter_context(mock.patch.object(winclip, "kernel32", _FakeKernel32(text)))
        stack.enter_context(mock.patch.object(winclip, "_Opened", _no_open))
        stack.enter_context(mock.patch.object(winclip.time, "sleep"))
        return stack

    def test_null_handle_then_data_is_returned(self):
        user32 = _FakeUser32(True, [0, 0, 7])
        with self._patched(user32):
            self.assertEqual(winclip.read_text(), "hello")
        self.assertEqual(user32.get_calls, 3)

    def test_persistent_null_handle_gives_up_after_bounded_retries(self):
        user32 = _FakeUser32(True, [])
        with self._patched(user32):
            self.assertIsNone(winclip.read_text())
        self.assertEqual(user32.get_calls, winclip._READ_NULL_RETRIES + 1)

    def test_format_not_available_does_not_retry(self):
        user32 = _FakeUser32(False, [])
        with self._patched(user32):
            self.assertIsNone(winclip.read_text())
        self.assertEqual(user32.get_calls, 0)

    def test_normal_read_makes_a_single_call(self):
        user32 = _FakeUser32(True, [7])
        with self._patched(user32, "line one\nline two"):
            self.assertEqual(winclip.read_text(), "line one\nline two")
        self.assertEqual(user32.get_calls, 1)


if __name__ == "__main__":
    unittest.main()
