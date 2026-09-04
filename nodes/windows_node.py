"""The laptop's clipboard as a ClipboardNode.

Thin adapter over the existing, working winclip.py: the ctypes bindings, the
AddClipboardFormatListener message-only window, and the OpenClipboard retry loop all stay
exactly as they were in v1.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

import winclip
import winimage
from core.dedup import RecentClips
from core.models import ClipPayload, WriteResult

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.windows")

# Mirrors android/ClipBridge.java's extensionForMime() -- same mime set (core/models.py's
# IMAGE_MIMES), same default-to-PNG fallback.
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


class WindowsNode(ClipboardNode):
    def __init__(self, name: str = "laptop", *, screenshot_dir: str | None = None,
                 screenshot_include_devices: bool = True,
                 screenshot_device_subfolders: bool = True,
                 device_screenshot_paths: dict[str, str] | None = None,
                 exclude_synced_images_from_history: bool = True) -> None:
        super().__init__(name)
        self._watcher: winclip.ClipboardWatcher | None = None
        self._running = False
        self._write_lock = threading.Lock()
        self._own_write_seq: int | None = None
        self._last_saved_screenshot: tuple[str, int, float, str | None] | None = None
        self._screenshot_dir = screenshot_dir or None
        self._screenshot_include_devices = screenshot_include_devices
        self._screenshot_device_subfolders = screenshot_device_subfolders
        self._device_screenshot_paths = dict(device_screenshot_paths or {})
        self._exclude_synced_images_from_history = exclude_synced_images_from_history
        self._screenshot_dedup = RecentClips(ttl=20.0)

    def update_config(self, *, screenshot_dir: str | None = None,
                      screenshot_include_devices: bool = True,
                      screenshot_device_subfolders: bool = True,
                      device_screenshot_paths: dict[str, str] | None = None,
                      exclude_synced_images_from_history: bool = True) -> None:
        """Hot-reloads screenshot directories and options at runtime without restart."""
        self._screenshot_dir = screenshot_dir or None
        self._screenshot_include_devices = screenshot_include_devices
        self._screenshot_device_subfolders = screenshot_device_subfolders
        self._device_screenshot_paths = dict(device_screenshot_paths or {})
        self._exclude_synced_images_from_history = exclude_synced_images_from_history
        log.info("[%s] runtime configuration updated (dir=%s, subfolders=%s, custom=%s)",
                 self.name, self._screenshot_dir, self._screenshot_device_subfolders,
                 list(self._device_screenshot_paths.keys()))

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
        with self._write_lock:
            if self._own_write_seq is not None:
                seq, self._own_write_seq = self._own_write_seq, None
                if winclip.sequence_number() == seq:
                    return  # our own image write echoing back through the same watcher
        payload = self._read()
        if payload is not None and payload.size:
            if payload.is_image:
                self._maybe_save_screenshot(payload)
            self._emit(payload)

    def request_refresh(self) -> None:
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    # -- local screenshot backup --------------------------------------------

    def _maybe_save_screenshot(self, payload: ClipPayload) -> None:
        """Best-effort local file copy of an image clip. Never raises, never blocks or
        delays sync -- a failure here (bad path, permissions, disk full) is a lost
        convenience copy, not a lost sync, so it only ever logs and moves on.
        """
        if not self._screenshot_dir:
            return
        if not self._screenshot_dedup.observe(payload.sha256, "local"):
            # Same image saved moments ago -- either Windows genuinely fired the
            # clipboard-changed notification twice for one snip (ARCHITECTURE.md's "Two
            # entries in Windows Clipboard History" note), or this is write()'s own-write
            # echo racing _on_change() (the watcher thread can see the change before
            # _own_write_seq below is stamped). Either way, one screenshot = one file.
            return
        try:
            custom_path = self._device_screenshot_paths.get(payload.origin, "").strip() if payload.origin else ""
            if custom_path:
                target_dir = custom_path
            elif self._screenshot_device_subfolders and payload.origin:
                target_dir = os.path.join(self._screenshot_dir, payload.origin)
            else:
                target_dir = self._screenshot_dir

            os.makedirs(target_dir, exist_ok=True)
            ext = _IMAGE_EXTENSIONS.get(payload.mime, ".png")
            stem = datetime.now().strftime("Screenshot %Y-%m-%d %H%M%S")
            path = os.path.join(target_dir, stem + ext)
            n = 2
            while os.path.exists(path):
                path = os.path.join(target_dir, f"{stem} ({n}){ext}")
                n += 1
            with open(path, "wb") as fh:
                fh.write(payload.data)
            log.info("[%s] screenshot backed up to %s", self.name, path)

            # Defense-in-depth: if an earlier saved screenshot within the last 8 seconds was a
            # tiny thumbnail preview (< 60 KB) and this new image is a full-resolution capture
            # (> 3x larger), clean up the preview file so duplicate thumbnails do not linger.
            now = time.time()
            current_size = len(payload.data)
            if self._last_saved_screenshot is not None:
                last_path, last_size, last_time, last_origin = self._last_saved_screenshot
                if (now - last_time <= 8.0 and last_size < 60_000 and current_size > 3 * last_size
                        and os.path.abspath(last_path) != os.path.abspath(path)):
                    try:
                        if os.path.exists(last_path):
                            os.remove(last_path)
                            log.info("[%s] pruned thumbnail preview duplicate: %s", self.name, last_path)
                    except Exception as exc:
                        log.debug("[%s] thumbnail prune skipped: %s", self.name, exc)

            self._last_saved_screenshot = (path, current_size, now, payload.origin)
        except Exception:
            log.warning("[%s] screenshot backup failed", self.name, exc_info=True)

    def write(self, payload: ClipPayload) -> str:
        if payload.is_image:
            if self._screenshot_include_devices:
                self._maybe_save_screenshot(payload)
            try:
                with self._write_lock:
                    winclip.write_image(
                        payload.data, payload.mime,
                        exclude_from_history=self._exclude_synced_images_from_history)
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
            with self._write_lock:
                winclip.write_text(text)
            return WriteResult.OK
        except Exception:
            log.exception("[%s] write failed", self.name)
            return WriteResult.FAILED
