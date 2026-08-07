"""The laptop's clipboard as a ClipboardNode.

Thin adapter over the existing, working winclip.py: the ctypes bindings, the
AddClipboardFormatListener message-only window, and the OpenClipboard retry loop all stay
exactly as they were in v1.
"""

from __future__ import annotations

import logging

import winclip
import winimage
from core.models import ClipPayload, WriteResult

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.windows")


class WindowsNode(ClipboardNode):
    def __init__(self, name: str = "laptop") -> None:
        super().__init__(name)
        self._watcher: winclip.ClipboardWatcher | None = None
        self._running = False
        # Sequence number stamped by our own last successful write. write_image() always
        # sets both CF_DIBV5 and (when available) a pass-through format in one cycle, but
        # the encoder is deterministic (winimage.png_from_bgra), so a read-back of our own
        # write always matches what we sent -- this guards the ClipboardWatcher callback
        # against re-emitting our own write as if it were a new copy from the user, exactly
        # as ClipBridge.java's ownWriteStamp already does for the Android side.
        self._own_write_seq: int | None = None

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
        if text is not None:
            return ClipPayload.from_text(text, origin=self.name)

        # No text: check for an image. Ordering matters -- a browser copy that carries
        # both text and an image html fragment must still sync as text, which the check
        # above already guarantees.
        try:
            image = winclip.read_image()
        except winclip.ClipboardBusy as exc:
            log.warning("[%s] clipboard busy: %s", self.name, exc)
            return None
        except winimage.ImageDecodeError as exc:
            log.warning("[%s] image on clipboard could not be decoded: %s", self.name, exc)
            return None
        except Exception:
            log.exception("[%s] image read failed", self.name)
            return None
        if image is None:
            return None   # neither text nor a recognised image format (files, etc.)
        data, mime = image
        return ClipPayload(mime=mime, data=data, origin=self.name)

    def _on_change(self) -> None:
        if self._own_write_seq is not None:
            seq, self._own_write_seq = self._own_write_seq, None
            if winclip.sequence_number() == seq:
                return  # our own image write echoing back through the same watcher
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    def request_refresh(self) -> None:
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    def write(self, payload: ClipPayload) -> str:
        if payload.is_image:
            try:
                winclip.write_image(payload.data, payload.mime)
                # Own-write suppression: winimage's PNG encoder is deterministic, so a
                # read-back of our own write always matches what we just sent -- there is
                # nothing new to report when the watcher fires for this exact write.
                self._own_write_seq = winclip.sequence_number()
                return WriteResult.OK
            except winimage.ImageDecodeError as exc:
                log.warning("[%s] image write rejected (undecodable): %s", self.name, exc)
                return WriteResult.REJECTED
            except Exception:
                log.exception("[%s] image write failed", self.name)
                return WriteResult.FAILED

        text = payload.text
        if text is None:
            return WriteResult.REJECTED
        try:
            winclip.write_text(text)
            return WriteResult.OK
        except Exception:
            log.exception("[%s] write failed", self.name)
            return WriteResult.FAILED
