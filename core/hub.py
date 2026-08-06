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
        # Nodes whose next observed clip is adopted as a starting point rather than applied.
        # This must happen ONLY on the first connect of this daemon's life: at startup we
        # have no idea whether the device's clip is newer than the laptop's, so clobbering
        # is rude. On a later reconnect the opposite is true, and re-arming it there would
        # silently swallow a clip copied while the link was down, which is precisely the
        # catch-up this is supposed to deliver.
        self._baseline_pending: set[str] = set()
        self._baselined: set[str] = set()
        self._last_stamp_node: str | None = None
        self._last_stamp_time: float = 0.0
        self.stats = {"observed": 0, "delivered": 0, "skipped": 0, "queued": 0}

    # -- topology ----------------------------------------------------------

    def domain_of(self, node_name: str) -> SyncDomain | None:
        return next((d for d in self.domains if d.contains(node_name)), None)

    def _standalone_nodes(self) -> list[ClipboardNode]:
        return [n for n in self.nodes.values() if self.domain_of(n.name) is None]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for node in self.nodes.values():
            node.subscribe(self.on_clip, self.on_descriptor, self.on_stamp)
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
                    # Re-assert read modes every tick. It is a no-op unless the elected
                    # representative or Continuity health actually changed, and it also
                    # re-applies the mode to a device that just reconnected.
                    domain.apply_modes()
                except Exception:
                    log.exception("domain tick failed")
            try:
                self._drain_outbox()
            except Exception:
                log.exception("outbox drain failed")

    # -- inbound -----------------------------------------------------------

    def on_stamp(self, node_name: str, stamp: str) -> None:
        """A device saw a change but deliberately did not read the content."""
        with self._route_lock:
            self._last_stamp_node = node_name
            self._last_stamp_time = time.time()
        for d in self.domains:
            d.note_stamp(node_name)
        log.debug("[%s] change announced without a content read (stamp %s)", node_name, stamp)

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

            if is_new:
                self.stats["observed"] += 1
            else:
                # Not new does not mean fully delivered: a node can still lack this exact
                # content (e.g. a write to it was silently lost). Keep going into _route()
                # so the per-node should_deliver() check below decides who still needs it,
                # rather than bailing out on the hash alone.
                log.debug("%s already observed; re-checking whether any node still lacks it",
                          payload.short)

            reason = self.policy.reject_reason(payload)
            if reason:
                self.stats["skipped"] += 1
                log.info("skip clip from %s: %s", source, reason)
                return

            now = time.time()
            if (self._last_stamp_node and self._last_stamp_node != source
                    and (now - self._last_stamp_time <= 6.0)):
                origin_display = f"{self._last_stamp_node} (via {source})"
                self._last_stamp_node = None
            else:
                origin_display = source

            self._route(payload, source_domain=domain, origin_display=origin_display)

    # -- outbound ----------------------------------------------------------

    def _route(self, payload: ClipPayload, source_domain: SyncDomain | None,
               origin_display: str | None = None) -> None:
        source = payload.origin

        # Standalone nodes (the laptop) each get their own copy.
        for node in self._standalone_nodes():
            if node.name == source:
                continue
            self._deliver_to_node(node, payload, origin_display=origin_display)

        # Domains get exactly one write, unless Continuity is degraded.
        for domain in self.domains:
            if source_domain is domain:
                # Origin is inside this domain: Samsung is already distributing it. This
                # is the redundancy the design exists to avoid.
                log.debug("not writing to domain %s; clip originated inside it", domain.name)
                continue
            self._deliver_to_domain(domain, payload, origin_display=origin_display)

    def _deliver_to_domain(self, domain: SyncDomain, payload: ClipPayload,
                           origin_display: str | None = None) -> bool:
        """Returns True once nothing is left outstanding for this domain (delivered now or
        already held), so callers -- specifically _drain_outbox -- know whether to clear the
        outbox entry. Do not infer this from stats["delivered"]: an already-satisfied target
        resolves without incrementing it.
        """
        targets = domain.write_targets()
        if not targets:
            self.outbox.put(f"domain:{domain.name}", payload)
            self.stats["queued"] += 1
            return False
        sha = payload.sha256
        satisfied = False
        for node in targets:
            # Captured before the call: this is the only way to know whether
            # _deliver_to_node is about to attempt a genuine write or short-circuit because
            # the node already has this content. note_write() below must only fire for a
            # real write -- redundantly re-arming it for an already-satisfied node makes the
            # health probe time out every grace_s against a device with nothing new to
            # confirm, which falsely trips the domain into "degraded" after fail_threshold
            # misses even though nothing is actually wrong.
            fresh = self.dedup.should_deliver(sha, node.name)
            # queue_on_failure=False: a domain target that fails is this function's problem,
            # not _deliver_to_node's. Queuing at the node level here used to leave a stray
            # "node:<name>" entry alongside the "domain:<name>" one below for the exact same
            # payload -- retrying the node-level entry writes straight back to that specific
            # device, bypassing the domain's own representative re-election, and could
            # survive to fire a stale rewrite well after the domain had already recovered.
            if self._deliver_to_node(node, payload, origin_display=origin_display,
                                      queue_on_failure=False):
                satisfied = True
                if fresh:
                    domain.note_write(sha, node)
        if not satisfied:
            self.outbox.put(f"domain:{domain.name}", payload)
            self.stats["queued"] += 1
        return satisfied

    def _deliver_to_node(self, node: ClipboardNode, payload: ClipPayload,
                         origin_display: str | None = None, *,
                         queue_on_failure: bool = True) -> bool:
        sha = payload.sha256
        display = origin_display or payload.origin
        if not self.dedup.should_deliver(sha, node.name):
            # Nothing outstanding for this node -- treat as satisfied, not failed. Returning
            # False here used to make a stale outbox entry for an already-delivered payload
            # re-queue itself forever (every tick, with its own log line and disk write)
            # until the dedup record's TTL expired, up to 90s later -- which is exactly how
            # a long-since-superseded write could fire a "phantom" delivery well after the
            # fact.
            log.debug("%s already holds %s", node.name, payload.short)
            return True

        result = node.write(payload)

        if result in (WriteResult.OK, WriteResult.DEFERRED):
            # Mark delivered on DEFERRED too: ClipBridge has taken custody and replays on
            # unlock, so re-sending later would double-apply.
            self.dedup.mark_delivered(sha, node.name)
            self.stats["delivered"] += 1
            log.info("%s -> %s: %d chars%s", display, node.name, payload.char_len,
                     " (queued on device until unlock)"
                     if result == WriteResult.DEFERRED else "")
            return True

        if result in (WriteResult.UNAVAILABLE, WriteResult.FAILED):
            # FAILED is queued too, not just UNAVAILABLE. A transient local failure (a
            # clipboard the OS briefly would not hand over, say) used to be logged and the
            # clip discarded outright, which is how a 93 KB clip from the tablet was lost.
            # The outbox tick retries it. queue_on_failure is False when this node is a
            # domain target: the caller (_deliver_to_domain) queues once at the domain
            # level instead, so retry re-runs representative election rather than forcing
            # this exact device again.
            if queue_on_failure:
                self.outbox.put(f"node:{node.name}", payload)
                self.stats["queued"] += 1
            if result == WriteResult.FAILED:
                log.warning("%s -> %s failed%s", display, node.name,
                            "; queued for retry" if queue_on_failure else "")
            return False

        log.warning("%s -> %s rejected (%s)", display, node.name, result)
        return False

    # -- recovery ----------------------------------------------------------

    def _drain_outbox(self) -> None:
        # Locked per entry, not for the whole drain: node.write() can now block for real
        # (waiting on an ACK), and AndroidNode's ack-correlation state is not itself safe
        # against two writes to the same node overlapping, so each individual delivery
        # attempt still needs to be atomic against on_clip()'s _route() chain. But holding
        # the lock for the *entire* loop let one slow or genuinely-still-failing entry starve
        # a concurrent, unrelated on_clip() call for as long as the whole backlog took to
        # walk -- narrowing to per-entry keeps the same race protection with a much smaller
        # worst-case latency hit on real-time events.
        for key in list(self.outbox.keys()):
            with self._route_lock:
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
                    if self._deliver_to_domain(domain, payload):
                        self.outbox.clear(key)

    def on_node_connected(self, node: ClipboardNode) -> None:
        """Catch-up when a node rejoins.

        The device clipboard holds exactly one item, so re-reading it *is* the catch-up:
        whatever you last copied arrives. Anything already known is filtered by dedup.
        """
        with self._route_lock:
            first_time = node.name not in self._baselined
            if first_time:
                self._baselined.add(node.name)
                self._baseline_pending.add(node.name)
        log.info("[%s] %s", node.name,
                 "connected; adopting its clip as the baseline" if first_time
                 else "reconnected; catching up (a clip copied while it was away will apply)")
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
