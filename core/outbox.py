"""Durable pending-write store.

Answers the "if it does not reach the laptop, queue it and push it later" requirement for
the laptop-to-device direction.

Deliberately **single-slot per target**, not a growing list. The clipboard holds exactly
one item, so delivering a backlog of five superseded clips would be wrong: only the most
recent one is what you actually want pasted. A newer put() replaces an older undelivered
one, which also makes an unbounded queue structurally impossible.

Durability: written with a temp file plus os.replace, which is atomic on Windows and
POSIX. A crash mid-write leaves either the old file or the new one, never a truncated
one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

from .models import ClipPayload

log = logging.getLogger("copyclip.outbox")


class Outbox:
    def __init__(self, path: str) -> None:
        self.path = path
        self._slots: dict[str, ClipPayload] = {}
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load()

    # -- api ---------------------------------------------------------------

    def put(self, key: str, payload: ClipPayload) -> None:
        with self._lock:
            existing = self._slots.get(key)
            if existing is not None and existing.sha256 == payload.sha256:
                # Re-arming the exact content already queued under this key. A caller that
                # retries every tick would otherwise fsync to disk and log this line on every
                # single attempt for as long as delivery keeps failing -- real CPU/IO cost
                # and log noise for something that hasn't actually changed.
                return
            self._slots[key] = payload
            self._save()
        log.info("outbox[%s] holding %s (target unavailable)", key, payload.short)

    def peek(self, key: str) -> ClipPayload | None:
        with self._lock:
            return self._slots.get(key)

    def clear(self, key: str) -> None:
        with self._lock:
            if self._slots.pop(key, None) is None:
                return
            self._save()
        log.debug("outbox[%s] cleared", key)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._slots)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._slots

    # -- persistence -------------------------------------------------------

    def _save(self) -> None:
        """Caller holds the lock."""
        data = {k: v.to_dict() for k, v in self._slots.items()}
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".outbox-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)   # atomic
        except Exception:
            log.exception("failed to persist outbox")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._slots = {k: ClipPayload.from_dict(v) for k, v in data.items()}
            if self._slots:
                log.info("outbox restored %d pending write(s) from disk: %s",
                         len(self._slots), ", ".join(self._slots))
        except Exception:
            # A corrupt outbox must never stop the daemon starting; the cost of dropping
            # it is one un-synced clip.
            log.exception("outbox unreadable, starting empty")
            self._slots = {}
