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
import re
import tempfile
import threading
import time

from .models import ClipPayload

log = logging.getLogger("copyclip.outbox")


def _sidecar_name(key: str) -> str:
    """Filesystem-safe filename for a sidecar. Outbox keys are "domain:<name>" or
    "node:<name>" -- small, fixed vocabulary -- but ':' is a reserved character in Windows
    filenames, so it (and anything else non-alphanumeric) is substituted.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".bin"


class Outbox:
    def __init__(self, path: str) -> None:
        self.path = path
        self._sidecar_dir = os.path.join(os.path.dirname(path), "outbox")
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

    def drop_sensitive(self) -> int:
        """Purge all sensitive slots held in volatile memory.

        Called when allow_sensitive policy is toggled off at runtime.
        Since sensitive payloads are never written to disk, disk state is untouched.
        Returns count of dropped slots.
        """
        with self._lock:
            dropped = [k for k, v in self._slots.items() if v.sensitive]
            for k in dropped:
                del self._slots[k]
        if dropped:
            log.info("outbox dropped %d sensitive in-flight slot(s): %s", len(dropped), dropped)
        return len(dropped)

    # -- persistence -------------------------------------------------------

    def _write_sidecar(self, path: str, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._sidecar_dir, prefix=".sidecar-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)   # atomic; overwrites any prior sidecar for this key
        except Exception:
            log.exception("failed to persist outbox sidecar %s", path)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _save(self) -> None:
        """Caller holds the lock.

        Image payloads write a **sidecar** file (state/outbox/<key>.bin) instead of
        base64 inline in outbox.json: a queued 24 MiB image base64'd inline would mean a
        ~32 MiB fsync on every put(), for a payload that is by construction never more
        than one per key (single-slot outbox). Text payloads are untouched -- still fully
        inline, exactly as before.
        """
        os.makedirs(self._sidecar_dir, exist_ok=True)
        data = {}
        live_sidecars = set()
        for k, v in self._slots.items():
            if v.sensitive:
                # Sensitive payloads (OTPs, credentials) must NEVER touch disk.
                # They stay in memory in self._slots only.
                continue
            if v.is_image:
                fname = _sidecar_name(k)
                live_sidecars.add(fname)
                self._write_sidecar(os.path.join(self._sidecar_dir, fname), v.data)
                data[k] = {
                    "mime": v.mime, "sha256": v.sha256, "sidecar": True,
                    "origin": v.origin, "ts": v.ts, "sensitive": v.sensitive,
                }
            else:
                data[k] = v.to_dict()

        # Prune sidecars for keys no longer queued (cleared, or superseded by a text put()
        # for the same key) -- otherwise stale image blobs accumulate on disk forever.
        if os.path.isdir(self._sidecar_dir):
            for fname in os.listdir(self._sidecar_dir):
                if fname not in live_sidecars:
                    try:
                        os.unlink(os.path.join(self._sidecar_dir, fname))
                    except OSError:
                        pass

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

    def _payload_from_entry(self, key: str, entry: dict) -> ClipPayload | None:
        if not entry.get("sidecar"):
            return ClipPayload.from_dict(entry)

        sidecar_path = os.path.join(self._sidecar_dir, _sidecar_name(key))
        if not os.path.isfile(sidecar_path):
            log.warning("outbox[%s] sidecar missing (%s); dropping stale entry",
                       key, sidecar_path)
            return None
        with open(sidecar_path, "rb") as fh:
            image_data = fh.read()
        payload = ClipPayload(
            mime=entry["mime"], data=image_data, origin=entry.get("origin", "?"),
            ts=entry.get("ts", time.time()), sensitive=entry.get("sensitive", False))
        if payload.sha256 != entry.get("sha256"):
            log.warning("outbox[%s] sidecar content does not match recorded sha256; "
                       "dropping stale entry", key)
            return None
        return payload

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            slots = {}
            for k, entry in data.items():
                payload = self._payload_from_entry(k, entry)
                if payload is not None:
                    slots[k] = payload
            self._slots = slots
            if self._slots:
                log.info("outbox restored %d pending write(s) from disk: %s",
                         len(self._slots), ", ".join(self._slots))
        except Exception:
            # A corrupt outbox must never stop the daemon starting; the cost of dropping
            # it is one un-synced clip.
            log.exception("outbox unreadable, starting empty")
            self._slots = {}
