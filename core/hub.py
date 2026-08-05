"""Routing. The only place that decides where a clip goes.

Depends purely on abstractions (ClipboardNode, SyncDomain, Policy, RecentClips, Outbox).
It has no idea whether a node is reached by adb, TCP or ctypes, which is what keeps
adding a device or a payload type from touching this file.

Routing rules, in full:

1. A clip observed on node N is recorded as held by N, so it is never sent back to N.
2. If N belongs to a sync domain, the other members of that domain are left alone. They
   are already getting it from Samsung, and writing to them would be the redundant work
   we were explicitly asked to avoid.
3. Everything outside N's domain receives it, once, subject to policy.
4. A domain receives exactly one write, to its representative, unless the domain reports
   Continuity as degraded.
5. Anything undeliverable goes to the durable outbox and drains on reconnect or unlock.
"""

from __future__ import annotations

import logging
import threading
import time

from domains.sync_domain import SyncDomain
from nodes.base import ClipboardNode

from .dedup import RecentClips
from .models import ClipDescriptor, ClipPayload, WriteResult
from .outbox import Outbox
from .policy import Policy

log = logging.getLogger("copyclip.hub")


class Hub:
    def __init__(self, *, nodes: list[ClipboardNode], domains: list[SyncDomain],
                 policy: Policy, outbox: Outbox, dedup: RecentClips | None = None,
                 tick_s: float = 2.0, adopt_on_connect: bool = False) -> None:
        self.nodes = {n.name: n for n in nodes}
        self.domains = domains
        self.policy = policy
        self.outbox = outbox
        self.dedup = dedup or RecentClips()
        self.tick_s = tick_s
        self.adopt_on_connect = adopt_on_connect

        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._route_lock = threading.Lock()
        # Nodes whose next observed clip is treated as a baseline only. Without this,
        # every reconnect (a network blip is enough) would overwrite whatever you just
        # copied on the laptop with the device's older clip.
        self._baseline_pending: set[str] = set()
        self.stats = {"observed": 0, "delivered": 0, "skipped": 0, "queued": 0}

    # -- topology ----------------------------------------------------------

    def domain_of(self, node_name: str) -> SyncDomain | None:
        return next((d for d in self.domains if d.contains(node_name)), None)

    def _standalone_nodes(self) -> list[ClipboardNode]:
        return [n for n in self.nodes.values() if self.domain_of(n.name) is None]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for node in self.nodes.values():
            node.subscribe(self.on_clip, self.on_descriptor)
            node.start()
        self._ticker = threading.Thread(target=self._tick_loop, name="copyclip-tick",
                                        daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        for node in self.nodes.values():
            try:
                node.stop()
            except Exception:
                log.exception("stopping %s failed", node.name)
        log.info("hub stopped. stats=%s", self.stats)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            for domain in self.domains:
                try:
                    domain.tick()
                except Exception:
                    log.exception("domain tick failed")
            try:
                self._drain_outbox()
            except Exception:
                log.exception("outbox drain failed")

    # -- inbound -----------------------------------------------------------

    def on_descriptor(self, desc: ClipDescriptor) -> None:
        """A device announced a clipboard change, metadata only.

        This is the redundancy filter. When Samsung mirrors a clip we already know about,
        its hash is familiar and we stop right here without ever pulling the body. Only a
        genuinely unknown hash costs a fetch, and only from the device that reported it
        first, so one copy moves one body no matter how many devices echo it.
        """
        with self._route_lock:
            known = self.dedup.known(desc.sha256)

            # A familiar hash arriving on a second device is precisely the evidence that
            # Samsung Continuity is alive, so feed the health check before returning.
            for d in self.domains:
                d.note_observed(desc.sha256, desc.node)

            if known:
                self.dedup.mark_delivered(desc.sha256, desc.node)
                log.debug("[%s] announced %s, already known; body not fetched",
                          desc.node, desc.short)
                return

            if desc.sensitive:
                self.stats["skipped"] += 1
                log.info("skip clip from %s: flagged sensitive by the source app", desc.node)
                return

            if desc.char_len > self.policy.max_chars:
                # Reject on the announcement, before paying to transfer it at all.
                self.stats["skipped"] += 1
                log.info("skip clip from %s: too large (%d > %d), body not fetched",
                         desc.node, desc.char_len, self.policy.max_chars)
                return

            node = self.nodes.get(desc.node)

        if node is not None:
            log.debug("[%s] announced new %s (%d chars); fetching body",
                      desc.node, desc.short, desc.char_len)
            node.request_refresh()

    def on_clip(self, payload: ClipPayload) -> None:
        """A clip was observed on payload.origin."""
        with self._route_lock:
            sha = payload.sha256
            source = payload.origin

            is_new = self.dedup.observe(sha, source)

            # Tell the domain regardless of novelty: a clip arriving on the *standby*
            # member is exactly the evidence that Continuity is alive, and it is normally
            # not new (we wrote it to the representative moments earlier).
            domain = self.domain_of(source)
            for d in self.domains:
                d.note_observed(sha, source)

            if source in self._baseline_pending:
                self._baseline_pending.discard(source)
                if not self.adopt_on_connect:
                    # Adopt as the shared starting point without applying it anywhere.
                    for name in self.nodes:
                        self.dedup.mark_delivered(sha, name)
                    log.info("baseline from %s (%d chars), not applied",
                             source, payload.char_len)
                    return

            if not is_new:
                log.debug("echo of %s on %s, ignoring", payload.short, source)
                return

            self.stats["observed"] += 1

            reason = self.policy.reject_reason(payload)
            if reason:
                self.stats["skipped"] += 1
                log.info("skip clip from %s: %s", source, reason)
                return

            self._route(payload, source_domain=domain)

    # -- outbound ----------------------------------------------------------

    def _route(self, payload: ClipPayload, source_domain: SyncDomain | None) -> None:
        source = payload.origin

        # Standalone nodes (the laptop) each get their own copy.
        for node in self._standalone_nodes():
            if node.name == source:
                continue
            self._deliver_to_node(node, payload)

        # Domains get exactly one write, unless Continuity is degraded.
        for domain in self.domains:
            if source_domain is domain:
                # Origin is inside this domain: Samsung is already distributing it. This
                # is the redundancy the design exists to avoid.
                log.debug("not writing to domain %s; clip originated inside it", domain.name)
                continue
            self._deliver_to_domain(domain, payload)

    def _deliver_to_domain(self, domain: SyncDomain, payload: ClipPayload) -> None:
        targets = domain.write_targets()
        if not targets:
            self.outbox.put(f"domain:{domain.name}", payload)
            self.stats["queued"] += 1
            return
        wrote_any = False
        for node in targets:
            if self._deliver_to_node(node, payload):
                wrote_any = True
                domain.note_write(payload.sha256, node)
        if not wrote_any:
            self.outbox.put(f"domain:{domain.name}", payload)
            self.stats["queued"] += 1

    def _deliver_to_node(self, node: ClipboardNode, payload: ClipPayload) -> bool:
        sha = payload.sha256
        if not self.dedup.should_deliver(sha, node.name):
            log.debug("%s already holds %s", node.name, payload.short)
            return False

        result = node.write(payload)

        if result in (WriteResult.OK, WriteResult.DEFERRED):
            # Mark delivered on DEFERRED too: ClipBridge has taken custody and replays on
            # unlock, so re-sending later would double-apply.
            self.dedup.mark_delivered(sha, node.name)
            self.stats["delivered"] += 1
            log.info("%s -> %s: %d chars%s", payload.origin, node.name, payload.char_len,
                     " (queued on device until unlock)"
                     if result == WriteResult.DEFERRED else "")
            return True

        if result == WriteResult.UNAVAILABLE:
            self.outbox.put(f"node:{node.name}", payload)
            self.stats["queued"] += 1
            return False

        log.warning("%s -> %s failed (%s)", payload.origin, node.name, result)
        return False

    # -- recovery ----------------------------------------------------------

    def _drain_outbox(self) -> None:
        for key in self.outbox.keys():
            payload = self.outbox.peek(key)
            if payload is None:
                continue
            kind, _, ident = key.partition(":")

            if kind == "node":
                node = self.nodes.get(ident)
                if node is None or not node.available:
                    continue
                if self._deliver_to_node(node, payload):
                    self.outbox.clear(key)

            elif kind == "domain":
                domain = next((d for d in self.domains if d.name == ident), None)
                if domain is None or not domain.write_targets():
                    continue
                before = self.stats["delivered"]
                self._deliver_to_domain(domain, payload)
                if self.stats["delivered"] > before:
                    self.outbox.clear(key)

    def on_node_connected(self, node: ClipboardNode) -> None:
        """Catch-up when a node rejoins.

        The device clipboard holds exactly one item, so re-reading it *is* the catch-up:
        whatever you last copied arrives. Anything already known is filtered by dedup.
        """
        with self._route_lock:
            self._baseline_pending.add(node.name)
        log.info("[%s] rejoined; requesting catch-up", node.name)
        node.request_refresh()
        self._drain_outbox()

    def on_node_unlocked(self, node: ClipboardNode, locked: bool) -> None:
        if not locked:
            self._drain_outbox()

    # -- diagnostics -------------------------------------------------------

    def status(self) -> dict:
        return {
            "nodes": {n.name: {"available": n.available, "locked": n.locked}
                      for n in self.nodes.values()},
            "domains": [d.status() for d in self.domains],
            "outbox": self.outbox.keys(),
            "stats": dict(self.stats),
        }
