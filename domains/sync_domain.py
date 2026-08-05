"""A set of devices that already mirror each other without our help.

The phone and tablet are one such domain: Samsung Continuity keeps them in step. Measured
on these devices, a shell-UID write to the phone reappeared on the tablet 2,499 ms later,
tagged `mcf_continuity;device_name=S24_Ultra`. Samsung does not care that the write came
from app_process.

That is what makes the "no redundant calls" requirement structural rather than a
convention someone has to remember: the hub writes to exactly **one** member (the
representative) and the domain distributes internally. CopyClip never syncs phone to
tablet itself.

Continuity is the fast path, not a guarantee. Samsung requires the same Wi-Fi network and
Samsung account, and documents that its Wi-Fi Direct channels can degrade after VPN use,
which matters because Tailscale runs here permanently. So the domain measures whether
propagation is actually happening and falls back to writing every member directly when it
is not.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from nodes.base import ClipboardNode

log = logging.getLogger("copyclip.domain")


@dataclass
class _Probe:
    sha: str
    written_to: str
    at: float


class SyncDomain:
    """Members that mirror each other externally, with health-aware write routing."""

    def __init__(self, name: str, members: list[ClipboardNode], *,
                 grace_s: float = 8.0, fail_threshold: int = 3,
                 assume_healthy: bool = True) -> None:
        if not members:
            raise ValueError("a sync domain needs at least one member")
        self.name = name
        self.members = members           # preference order; first is preferred rep
        self.grace_s = grace_s
        self.fail_threshold = fail_threshold

        self._healthy = assume_healthy
        self._consecutive_misses = 0
        self._probe: _Probe | None = None
        self._lock = threading.Lock()

    # -- membership --------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """True when the members are observably mirroring each other."""
        with self._lock:
            return self._healthy

    def member(self, name: str) -> ClipboardNode | None:
        return next((m for m in self.members if m.name == name), None)

    def contains(self, node_name: str) -> bool:
        return any(m.name == node_name for m in self.members)

    def representative(self) -> ClipboardNode | None:
        """Pick the member to write through.

        Prefers a device that is connected **and unlocked**, because Android silently
        discards writes behind the keyguard. This is why a locked phone with an awake
        tablet still works: the write goes to the tablet and Continuity carries it to the
        phone on unlock.
        """
        for m in self.members:
            if m.writable:
                return m
        for m in self.members:          # fall back to connected-but-locked: ClipBridge
            if m.available:             # parks it and replays on unlock
                return m
        return None

    def write_targets(self) -> list[ClipboardNode]:
        """Who to write to for one clip.

        Healthy: exactly one member. Degraded: every reachable member, because nothing is
        relaying for us.
        """
        if self.healthy:
            rep = self.representative()
            return [rep] if rep is not None else []
        return [m for m in self.members if m.available]

    # -- continuity health -------------------------------------------------

    def note_write(self, sha: str, target: ClipboardNode) -> None:
        """Arm a health probe after writing to `target`.

        Only meaningful with 2+ members; with one member there is nothing to mirror to.
        """
        if len(self.members) < 2:
            return
        others = [m for m in self.members if m.name != target.name and m.available]
        if not others:
            return   # cannot judge propagation if nothing else is connected
        with self._lock:
            self._probe = _Probe(sha=sha, written_to=target.name, at=time.time())

    def note_observed(self, sha: str, node_name: str) -> None:
        """A member reported holding `sha`. Confirms propagation if it is the other one."""
        with self._lock:
            probe = self._probe
            if probe is None or probe.sha != sha or node_name == probe.written_to:
                return
            self._probe = None
            self._consecutive_misses = 0
            if not self._healthy:
                self._healthy = True
                log.info("[%s] Continuity propagation confirmed; back to single-target writes",
                         self.name)
                return
        log.debug("[%s] Continuity confirmed via %s", self.name, node_name)

    def tick(self) -> None:
        """Expire an unconfirmed probe. Call periodically from the hub."""
        with self._lock:
            probe = self._probe
            if probe is None or time.time() - probe.at <= self.grace_s:
                return
            self._probe = None
            self._consecutive_misses += 1
            misses = self._consecutive_misses
            if self._healthy and misses >= self.fail_threshold:
                self._healthy = False
                log.warning(
                    "[%s] Continuity has not propagated %d writes in a row; "
                    "switching to direct writes to every member", self.name, misses)
                return
        log.debug("[%s] Continuity probe missed (%d/%d)", self.name, misses,
                  self.fail_threshold)

    # -- diagnostics -------------------------------------------------------

    def status(self) -> dict:
        rep = self.representative()
        return {
            "domain": self.name,
            "healthy": self.healthy,
            "representative": rep.name if rep else None,
            "members": {m.name: {"available": m.available, "locked": m.locked}
                        for m in self.members},
        }
