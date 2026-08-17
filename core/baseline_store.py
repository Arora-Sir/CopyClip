"""Durable last-known-clip-hash per device.

Answers "have we already seen this exact content from this device before, across a
daemon restart" -- the question `core/hub.py`'s in-memory `_baselined` set cannot answer,
because it resets to empty every time the process starts. Without this, every fresh
process (including a spurious restart of an already-healthy daemon) treats a device's
current clip as "first ever seen" and deliberately does not apply it, silently dropping
anything that changed on the device between the old process dying and the new one
connecting.

Deliberately tiny: one hash per device name, nothing else. Not a history, not a cache of
content -- just enough to tell "unchanged since we last recorded it" from "changed while
we were not looking."

Durability: written with a temp file plus os.replace, same as `core/outbox.py`, which is
atomic on Windows and POSIX. A crash mid-write leaves either the old file or the new one,
never a truncated one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

log = logging.getLogger("copyclip.baseline_store")


class BaselineStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._hashes: dict[str, str] = {}
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load()

    # -- api -----------------------------------------------------------------

    def get(self, node_name: str) -> str | None:
        with self._lock:
            return self._hashes.get(node_name)

    def set(self, node_name: str, sha256: str) -> None:
        with self._lock:
            if self._hashes.get(node_name) == sha256:
                # Unchanged -- skip the fsync and disk write, same reasoning as
                # Outbox.put()'s identical short-circuit.
                return
            self._hashes[node_name] = sha256
            self._save()

    # -- persistence -----------------------------------------------------------

    def _save(self) -> None:
        """Caller holds the lock."""
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".baseline-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._hashes, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)   # atomic
        except Exception:
            log.exception("failed to persist baseline store")
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
            if isinstance(data, dict):
                self._hashes = {str(k): str(v) for k, v in data.items()}
        except Exception:
            # A corrupt file must never stop the daemon starting; the cost of dropping it
            # is falling back to today's "adopt as baseline" behavior for every device.
            log.exception("baseline store unreadable, starting empty")
            self._hashes = {}
