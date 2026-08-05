"""Loop prevention across an arbitrary number of nodes.

v1 tracked a single `last_hash`, which was sufficient for two nodes. It is not sufficient
here, and this is the single most likely place for a three-device build to break.

With the phone and tablet inside Samsung's own sync domain, **one copy legitimately
surfaces up to three times**: once on the device you copied from, once on its Samsung
sibling about 2.5 s later (relabelled, so it looks like a different clip to anything that
keys on labels), and once more if we wrote it ourselves. A single `last_hash` gets
overwritten by the second arrival and the third one ping-pongs forever.

So delivery is tracked per (hash, node) instead.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Entry:
    origin: str
    first_seen: float
    delivered_to: set[str] = field(default_factory=set)


class RecentClips:
    """TTL-bounded record of which clips which nodes already have.

    TTL rather than a permanent set, because re-copying the same text later is a real
    user action that must sync again. 90 s is comfortably longer than Samsung's ~2.5 s
    propagation and any plausible reconnect, while still feeling instant if you
    deliberately re-copy something.
    """

    def __init__(self, ttl: float = 90.0, max_entries: int = 512) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    # -- observation -------------------------------------------------------

    def observe(self, sha: str, node: str, *, origin: bool = False) -> bool:
        """Record that `node` holds this clip.

        Returns True if this is the first time we have seen `sha` at all, i.e. it is a
        genuinely new clip rather than an echo of one already in flight.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(sha)
            if entry is None:
                self._entries[sha] = _Entry(origin=node if origin else node,
                                            first_seen=now,
                                            delivered_to={node})
                return True
            entry.delivered_to.add(node)
            return False

    def should_deliver(self, sha: str, node: str) -> bool:
        """False if `node` already holds this clip, so we never send it back.

        Prunes first. In the normal flow observe() has already pruned, but relying on
        that would make this method silently honour the TTL only when called in the right
        order, which is exactly the kind of hidden coupling that breaks later.
        """
        with self._lock:
            self._prune(time.time())
            entry = self._entries.get(sha)
            if entry is None:
                return True
            return node not in entry.delivered_to

    def mark_delivered(self, sha: str, node: str) -> None:
        with self._lock:
            entry = self._entries.get(sha)
            if entry is None:
                self._entries[sha] = _Entry(origin=node, first_seen=time.time(),
                                            delivered_to={node})
            else:
                entry.delivered_to.add(node)

    def origin_of(self, sha: str) -> str | None:
        with self._lock:
            entry = self._entries.get(sha)
            return entry.origin if entry else None

    def known(self, sha: str) -> bool:
        with self._lock:
            self._prune(time.time())
            return sha in self._entries

    # -- housekeeping ------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Caller must hold the lock."""
        if len(self._entries) > self.max_entries:
            # Hard cap as a backstop against pathological churn, oldest first.
            for sha in sorted(self._entries, key=lambda s: self._entries[s].first_seen)[
                    : len(self._entries) - self.max_entries]:
                self._entries.pop(sha, None)
        expired = [s for s, e in self._entries.items() if now - e.first_seen > self.ttl]
        for sha in expired:
            del self._entries[sha]

    def snapshot(self) -> dict[str, dict]:
        """Diagnostics only."""
        with self._lock:
            return {s: {"origin": e.origin, "age": round(time.time() - e.first_seen, 1),
                        "delivered_to": sorted(e.delivered_to)}
                    for s, e in self._entries.items()}
