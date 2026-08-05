"""The laptop's clipboard as a ClipboardNode.

Thin adapter over the existing, working winclip.py: the ctypes bindings, the
AddClipboardFormatListener message-only window, and the OpenClipboard retry loop all stay
exactly as they were in v1.
"""

from __future__ import annotations

import logging

import winclip
from core.models import ClipPayload, WriteResult

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.windows")


class WindowsNode(ClipboardNode):
    def __init__(self, name: str = "laptop") -> None:
        super().__init__(name)
        self._watcher: winclip.ClipboardWatcher | None = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._watcher = winclip.ClipboardWatcher(self._on_change)
        self._watcher.start()
        self._running = True
        log.info("[%s] watching clipboard", self.name)

    def stop(self) -> None:
        self._running = False
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @property
    def available(self) -> bool:
        return self._running

    # -- io ----------------------------------------------------------------

    def _read(self) -> ClipPayload | None:
        try:
            text = winclip.read_text()
        except winclip.ClipboardBusy as exc:
            # Another app held the clipboard through the whole retry window. Dropping this
            # one event is correct; the next copy will fire another notification.
            log.warning("[%s] clipboard busy: %s", self.name, exc)
            return None
        except Exception:
            log.exception("[%s] read failed", self.name)
            return None
        if text is None:
            return None   # image or files present; v1 syncs text only
        return ClipPayload.from_text(text, origin=self.name)

    def _on_change(self) -> None:
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    def request_refresh(self) -> None:
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    def write(self, payload: ClipPayload) -> str:
        text = payload.text
        if text is None:
            return WriteResult.REJECTED
        try:
            winclip.write_text(text)
            return WriteResult.OK
        except Exception:
            log.exception("[%s] write failed", self.name)
            return WriteResult.FAILED
