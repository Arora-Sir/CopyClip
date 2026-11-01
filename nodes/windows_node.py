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
import ctypes
from datetime import datetime

import winclip
import winimage
from core.dedup import RecentClips
from core.models import ClipPayload, WriteResult

from .base import ClipboardNode

log = logging.getLogger("copyclip.node.windows")

# Mirrors android/ClipBridge.java's extensionForMime(): same mime set (core/models.py's
# IMAGE_MIMES), same default-to-PNG fallback.
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


def _notify_shell(path: str, is_delete: bool = False) -> None:
    """Notify Windows Shell/File Explorer to refresh folder view immediately."""
    try:
        # SHCNE_CREATE = 0x00000002, SHCNE_DELETE = 0x00000004, SHCNF_PATHW = 0x0005
        event = 0x00000004 if is_delete else 0x00000002
        ctypes.windll.shell32.SHChangeNotify(event, 0x0005, str(path), None)
    except Exception:
        pass


class WindowsNode(ClipboardNode):
    def __init__(self, name: str = "laptop", *, screenshot_dir: str | None = None,
                 screenshot_include_devices: bool = True,
                 screenshot_device_subfolders: bool = True,
                 device_screenshot_paths: dict[str, str] | None = None,
                 exclude_synced_images_from_history: bool = True,
                 sensitive_wipe_s: float = 10.0,
                 direct_password_sync: bool = False,
                 camera_backup: bool = True,
                 camera_backup_dir: str | None = None,
                 camera_device_subfolders: bool = False) -> None:
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
        self.sensitive_wipe_s = float(sensitive_wipe_s)
        self.direct_password_sync = direct_password_sync
        self._camera_backup = camera_backup
        self._camera_backup_dir = camera_backup_dir or None
        self._camera_device_subfolders = camera_device_subfolders
        self._wipe_timer: threading.Timer | None = None

    def update_config(self, *, screenshot_dir: str | None = None,
                      screenshot_include_devices: bool = True,
                      screenshot_device_subfolders: bool = True,
                      device_screenshot_paths: dict[str, str] | None = None,
                      exclude_synced_images_from_history: bool = True,
                      sensitive_wipe_s: float | None = None,
                      direct_password_sync: bool | None = None,
                      camera_backup: bool | None = None,
                      camera_backup_dir: str | None = None,
                      camera_device_subfolders: bool | None = None) -> None:
        """Hot-reloads screenshot directories and options at runtime without restart."""
        self._screenshot_dir = screenshot_dir or None
        self._screenshot_include_devices = screenshot_include_devices
        self._screenshot_device_subfolders = screenshot_device_subfolders
        self._device_screenshot_paths = dict(device_screenshot_paths or {})
        self._exclude_synced_images_from_history = exclude_synced_images_from_history
        if sensitive_wipe_s is not None:
            self.sensitive_wipe_s = float(sensitive_wipe_s)
        if direct_password_sync is not None:
            self.direct_password_sync = direct_password_sync
        if camera_backup is not None:
            self._camera_backup = camera_backup
        if camera_backup_dir is not None:
            self._camera_backup_dir = camera_backup_dir or None
        if camera_device_subfolders is not None:
            self._camera_device_subfolders = camera_device_subfolders
        log.info("[%s] runtime configuration updated (dir=%s, subfolders=%s, custom=%s, camera_backup=%s)",
                 self.name, self._screenshot_dir, self._screenshot_device_subfolders,
                 list(self._device_screenshot_paths.keys()), self._camera_backup)

    #: lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._watcher = winclip.ClipboardWatcher(self._on_change)
        self._watcher.start()
        self._running = True
        log.info("[%s] watching clipboard", self.name)

    def stop(self) -> None:
        self._running = False
        if self._wipe_timer is not None:
            self._wipe_timer.cancel()
            self._wipe_timer = None
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @property
    def available(self) -> bool:
        return self._running

    #: io ----------------------------------------------------------------

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

        # No text: check for an image. Ordering matters: a browser copy that carries
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
            # User copied something locally. Cancel any pending sensitive-wipe so the
            # new content is not silently wiped by a stale timer.
            if self._wipe_timer is not None:
                self._wipe_timer.cancel()
                self._wipe_timer = None
        payload = self._read()
        if payload is not None and payload.size:
            if payload.is_image:
                self._maybe_save_screenshot(payload)
            self._emit(payload)


    def request_refresh(self) -> None:
        payload = self._read()
        if payload is not None and payload.size:
            self._emit(payload)

    #: local screenshot backup --------------------------------------------

    def _maybe_save_screenshot(self, payload: ClipPayload) -> None:
        """Best-effort local file copy of an image clip. Never raises, never blocks or
        delays sync: a failure here (bad path, permissions, disk full) is a lost
        convenience copy, not a lost sync, so it only ever logs and moves on.
        """
        if not self._screenshot_dir:
            return
        if not self._screenshot_dedup.observe(payload.sha256, "local"):
            # Same image saved moments ago: either Windows genuinely fired the
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
            _notify_shell(path, is_delete=False)
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
                            _notify_shell(last_path, is_delete=True)
                            log.info("[%s] pruned thumbnail preview duplicate: %s", self.name, last_path)
                    except Exception as exc:
                        log.debug("[%s] thumbnail prune skipped: %s", self.name, exc)

            self._last_saved_screenshot = (path, current_size, now, payload.origin)
        except Exception:
            log.warning("[%s] screenshot backup failed", self.name, exc_info=True)

    def _save_camera_photo(self, payload: ClipPayload) -> None:
        if not self._screenshot_dir or not self._camera_backup:
            return
        camera_dir = self._camera_backup_dir or os.path.join(self._screenshot_dir, "Camera")
        if self._camera_device_subfolders and payload.origin:
            camera_dir = os.path.join(camera_dir, payload.origin)
        try:
            os.makedirs(camera_dir, exist_ok=True)
            ext = ".jpg"
            if payload.mime == "image/png":
                ext = ".png"
            elif payload.mime in ("image/heic", "image/heif"):
                ext = ".heic"
            elif payload.mime == "image/webp":
                ext = ".webp"
            now = time.strftime("%Y-%m-%d %H%M%S")
            origin_tag = f"_{payload.origin}" if payload.origin else ""
            filename = f"Photo {now}{origin_tag}{ext}"
            path = os.path.join(camera_dir, filename)
            idx = 2
            while os.path.exists(path):
                path = os.path.join(camera_dir, f"Photo {now}{origin_tag} ({idx}){ext}")
                idx += 1
            with open(path, "wb") as f:
                f.write(payload.data)
            _notify_shell(path, is_delete=False)
            log.info("[%s] camera photo backed up to %s (%d bytes)", self.name, path, len(payload.data))
        except Exception:
            log.warning("[%s] camera photo backup failed", self.name, exc_info=True)

    def _wipe_sensitive(self, target_seq: int) -> None:
        """Clear sensitive text from clipboard if it has not been superseded.

        Uses winclip.clear_clipboard() (bare EmptyClipboard, no SetClipboardData)
        so the wipe does NOT create a blank tile in Windows Clipboard History (Win+V).
        Stamps _own_write_seq immediately so the ClipboardWatcher echo is suppressed
        and the empty state is not re-broadcast to Android nodes.
        """
        with self._write_lock:
            current_seq = winclip.sequence_number()
            if current_seq == target_seq:
                try:
                    new_seq = winclip.clear_clipboard()
                    self._own_write_seq = new_seq
                    self._wipe_timer = None
                    log.info("[%s] sensitive clip auto-wiped after %.0fs (no Win+V entry)",
                             self.name, self.sensitive_wipe_s)
                except Exception as exc:
                    log.warning("[%s] sensitive clip auto-wipe failed: %s", self.name, exc)
            else:
                log.debug("[%s] sensitive clip superseded (seq %d != %d), wipe skipped",
                          self.name, current_seq, target_seq)
            self._wipe_timer = None


    def write(self, payload: ClipPayload) -> str:
        if payload.is_camera:
            self._save_camera_photo(payload)
            return WriteResult.OK

        if payload.is_image:
            if self._screenshot_include_devices:
                self._maybe_save_screenshot(payload)
            try:
                with self._write_lock:
                    # Cancel any pending sensitive-wipe: the new image supersedes the clip.
                    if self._wipe_timer is not None:
                        self._wipe_timer.cancel()
                        self._wipe_timer = None
                    winclip.write_image(
                        payload.data, payload.mime,
                        exclude_from_history=self._exclude_synced_images_from_history)
                    # Own-write suppression: winimage's PNG encoder is deterministic, so a
                    # read-back of our own write always matches what we just sent: there is
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
                if payload.sensitive:
                    if self.direct_password_sync:
                        if self._wipe_timer is not None:
                            self._wipe_timer.cancel()
                            self._wipe_timer = None
                        winclip.write_text(text)
                        self._own_write_seq = winclip.sequence_number()
                        log.info("[%s] wrote sensitive text directly (Win+V history enabled, no auto-wipe)", self.name)
                    else:
                        winclip.write_text_sensitive(text)
                        seq = winclip.sequence_number()
                        self._own_write_seq = seq
                        if self._wipe_timer is not None:
                            self._wipe_timer.cancel()
                        wipe_s = max(1.0, float(self.sensitive_wipe_s))
                        self._wipe_timer = threading.Timer(wipe_s, self._wipe_sensitive, args=(seq,))
                        self._wipe_timer.daemon = True
                        self._wipe_timer.start()
                        log.info("[%s] wrote sensitive text; auto-wipe timer armed (%.0fs)", self.name, wipe_s)
                else:
                    # Cancel any pending sensitive-wipe: non-sensitive content supersedes it.
                    if self._wipe_timer is not None:
                        self._wipe_timer.cancel()
                        self._wipe_timer = None
                    winclip.write_text(text)
                    self._own_write_seq = winclip.sequence_number()
            return WriteResult.OK
        except Exception:
            log.exception("[%s] write failed", self.name)
            return WriteResult.FAILED

