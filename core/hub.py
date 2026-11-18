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

from typing import TYPE_CHECKING

from .interfaces import ClipboardNode

if TYPE_CHECKING:   # annotations only: core never imports the domains package at runtime
    from domains.sync_domain import SyncDomain

from .activity import ActivityLog
from .baseline_store import BaselineStore
from .dedup import RecentClips
from .models import ClipDescriptor, ClipPayload, WriteResult
from .outbox import Outbox
from .imagemeta import dimensions as image_dimensions, has_exif
from .policy import Policy

log = logging.getLogger("copyclip.hub")


#: Seconds an image must stay unchanged before announcing it again counts as a re-copy.
IMAGE_RECOPY_GUARD_S = 15.0
#: A second image of the same size from the same device within this window, whose size differs by
#: under this fraction, is a re-save of the first.
RESAVE_WINDOW_S = 4.0
RESAVE_SIZE_TOLERANCE = 0.02
#: Samsung puts a small preview of a screenshot on the clipboard around the time the real one lands.
#: Images under this size wait PREVIEW_HOLD_S for a larger picture of the same shape from the same device.
PREVIEW_MAX_BYTES = 60_000
PREVIEW_HOLD_S = 2.5
PREVIEW_LOOKBACK_S = 6.0

class Hub:
    def __init__(self, *, nodes: list[ClipboardNode], domains: list[SyncDomain],
                 policy: Policy, outbox: Outbox, dedup: RecentClips | None = None,
                 baseline_store: BaselineStore | None = None,
                 tick_s: float = 2.0, adopt_on_connect: bool = False, filter_previews: bool = True,
                 direct_password_sync: bool = False,
                 allow_recopy: bool = True,
                 activity: ActivityLog | None = None) -> None:
        self.nodes = {n.name: n for n in nodes}
        self.domains = domains
        self.policy = policy
        self.outbox = outbox
        self._recent_images: dict[str, tuple] = {}
        self._pending_previews: dict[str, tuple] = {}
        self._preview_notes: dict[str, str] = {}
        self._preview_lock = threading.Lock()
        self.preview_hold_s = PREVIEW_HOLD_S
        self.filter_previews = filter_previews
        self.dedup = dedup or RecentClips()
        self.baseline_store = baseline_store
        self.tick_s = tick_s
        self.adopt_on_connect = adopt_on_connect
        self.direct_password_sync = direct_password_sync
        self.allow_recopy = allow_recopy
        # Plain bool, not lock-guarded: a UI toggle racing one in-flight clip by a few
        # milliseconds is harmless, and CPython's GIL already makes the read/write atomic.
        self.paused = False
        self.paused_devices: set[str] = set()

        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._route_lock = threading.Lock()
        self._baseline_pending: set[str] = set()
        self._baselined: set[str] = set()
        self._last_stamp_node: str | None = None
        self._last_stamp_time: float = 0.0
        self.stats = {"observed": 0, "delivered": 0, "skipped": 0, "queued": 0}
        self.activity = activity if activity is not None else ActivityLog()
        self.started_at = time.time()
        self._node_last_clip: dict[str, float] = {}
        # Per-route delivery trace for the activity log. Only touched under _route_lock.
        self._trace: list[dict] | None = None

    def pause_device(self, name: str) -> None:
        self.paused_devices.add(name)
        log.info("sync paused for device: %s", name)

    def resume_device(self, name: str) -> None:
        self.paused_devices.discard(name)
        log.info("sync resumed for device: %s", name)

    def is_device_paused(self, name: str) -> bool:
        return self.paused or (name in self.paused_devices)

    def reload_config(self, cfg: dict) -> list[str]:
        """Hot-apply every reloadable key that is present in `cfg`; returns the keys applied.

        A key that is absent is left exactly as it is. Startup defaults live in
        core/config.py, so a partial dict can never silently flip a setting.
        """
        applied: list[str] = []

        def has(*keys: str) -> bool:
            present = [k for k in keys if k in cfg]
            applied.extend(present)
            return bool(present)

        if has("allow_images"):
            self.policy.allow_images = bool(cfg["allow_images"])
        if has("max_chars"):
            self.policy.max_chars = int(cfg["max_chars"])
        if has("allow_non_text"):
            self.policy.allow_non_text = bool(cfg["allow_non_text"])
        if has("denylist_regex"):
            self.policy.set_denylist(cfg["denylist_regex"])
        if has("max_image_bytes"):
            self.policy.max_image_bytes = int(cfg["max_image_bytes"])
            for node in self.nodes.values():
                if hasattr(node, "set_max_image_bytes"):
                    node.set_max_image_bytes(self.policy.max_image_bytes)
        if has("ack_timeout_s"):
            for node in self.nodes.values():
                if hasattr(node, "set_ack_timeout_s"):
                    node.set_ack_timeout_s(float(cfg["ack_timeout_s"]))
        if has("allow_sensitive"):
            new_allow_sensitive = bool(cfg["allow_sensitive"])
            if not new_allow_sensitive and self.policy.allow_sensitive:
                self.outbox.drop_sensitive()
            self.policy.allow_sensitive = new_allow_sensitive
            for node in self.nodes.values():
                if hasattr(node, "set_allow_sensitive"):
                    node.set_allow_sensitive(new_allow_sensitive)

        wipe_s = float(cfg["sensitive_wipe_s"]) if has("sensitive_wipe_s") else None
        if wipe_s is not None:
            for node in self.nodes.values():
                if hasattr(node, "set_sensitive_wipe_s"):
                    node.set_sensitive_wipe_s(wipe_s)

        if has("direct_password_sync"):
            self.direct_password_sync = bool(cfg["direct_password_sync"])
        if has("allow_recopy"):
            self.allow_recopy = bool(cfg["allow_recopy"])
        if has("adopt_on_connect"):
            self.adopt_on_connect = bool(cfg["adopt_on_connect"])
        if has("filter_screenshot_previews"):
            self.filter_previews = bool(cfg["filter_screenshot_previews"])

        camera_backup = bool(cfg["camera_backup"]) if has("camera_backup") else None
        if camera_backup is not None:
            for node in self.nodes.values():
                if hasattr(node, "set_camera_backup"):
                    node.set_camera_backup(camera_backup)

        laptop_node = self.nodes.get("laptop")
        storage_keys = ("screenshot_backup_dir", "screenshot_backup_include_devices",
                        "screenshot_device_subfolders", "device_screenshot_paths",
                        "screenshot_exclude_from_history", "camera_backup_dir",
                        "camera_device_subfolders")
        if laptop_node is not None and hasattr(laptop_node, "update_config") and has(*storage_keys):
            laptop_node.update_config(**self._storage_kwargs(cfg, wipe_s, camera_backup))
        elif laptop_node is not None and hasattr(laptop_node, "update_config") and (
                wipe_s is not None or camera_backup is not None
                or "direct_password_sync" in cfg):
            laptop_node.update_config(**self._storage_kwargs(cfg, wipe_s, camera_backup))
        log.info("hub configuration hot-reloaded (%d keys)", len(applied))
        return applied

    def _storage_kwargs(self, cfg: dict, wipe_s, camera_backup) -> dict:
        laptop = self.nodes["laptop"]
        kwargs = dict(
            screenshot_dir=(cfg["screenshot_backup_dir"] or None)
            if "screenshot_backup_dir" in cfg else getattr(laptop, "_screenshot_dir", None),
            screenshot_include_devices=cfg.get(
                "screenshot_backup_include_devices",
                getattr(laptop, "_screenshot_include_devices", True)),
            screenshot_device_subfolders=cfg.get(
                "screenshot_device_subfolders",
                getattr(laptop, "_screenshot_device_subfolders", True)),
            device_screenshot_paths=cfg.get(
                "device_screenshot_paths", getattr(laptop, "_device_screenshot_paths", {})),
            exclude_synced_images_from_history=cfg.get(
                "screenshot_exclude_from_history",
                getattr(laptop, "_exclude_synced_images_from_history", True)),
            sensitive_wipe_s=wipe_s,
            direct_password_sync=self.direct_password_sync,
            camera_backup=camera_backup,
            camera_device_subfolders=cfg.get("camera_device_subfolders"),
        )
        if "camera_backup_dir" in cfg:
            kwargs["camera_backup_dir"] = cfg["camera_backup_dir"] or ""  # "" clears the override
        return kwargs

    def apply_key(self, key_hex: str) -> list[str]:
        """Give every connected device a new shared key; returns the names updated.

        Devices that are offline pick the key up from the CONF line sent on reconnect.
        """
        updated: list[str] = []
        for node in self.nodes.values():
            if hasattr(node, "send_conf"):
                node.send_conf(key_hex, getattr(node, "hub_host", ""),
                               getattr(node, "push_port", 8387),
                               getattr(node, "device_name", node.name))
                if node.available:
                    updated.append(node.name)
        return updated

    #: topology ----------------------------------------------------------

    def domain_of(self, node_name: str) -> SyncDomain | None:
        return next((d for d in self.domains if d.contains(node_name)), None)

    def _standalone_nodes(self) -> list[ClipboardNode]:
        return [n for n in self.nodes.values() if self.domain_of(n.name) is None]

    #: lifecycle ---------------------------------------------------------

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

    #: inbound -----------------------------------------------------------

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
                # Images use a long guard: a device echoing the same image right after we read it
                # (Samsung bumps the clip stamp) is not a person copying it again.
                if self.allow_recopy and self.dedup.is_recopy(
                        desc.sha256, desc.node, min_interval_s=IMAGE_RECOPY_GUARD_S if desc.is_image else 0.3):
                    log.info("[%s] re-copied %s; re-fetching body to update clipboard",
                             desc.node, desc.short)
                    self.dedup.prepare_for_redelivery(desc.sha256, desc.node)
                else:
                    self.dedup.mark_delivered(desc.sha256, desc.node)
                    log.debug("[%s] announced %s, already known; body not fetched",
                              desc.node, desc.short)
                    return

            if desc.sensitive and not self.policy.allow_sensitive:
                self.stats["skipped"] += 1
                log.info("skip clip from %s: flagged sensitive by the source app", desc.node)
                self.activity.record(kind="text", size=desc.char_len, origin=desc.node,
                                     status="skipped", sensitive=True,
                                     reason="flagged sensitive; sensitive sync is off")
                return

            size_cap = self.policy.max_image_bytes if desc.is_image else self.policy.max_chars
            if desc.char_len > size_cap:
                # Reject on the announcement, before paying to transfer it at all.
                self.stats["skipped"] += 1
                log.info("skip clip from %s: too large (%d > %d), body not fetched",
                         desc.node, desc.char_len, size_cap)
                self.activity.record(kind="image" if desc.is_image else "text",
                                     size=desc.char_len, origin=desc.node, status="skipped",
                                     reason=f"too large ({desc.char_len} > {size_cap})")
                return

            node = self.nodes.get(desc.node)

        if node is not None:
            log.debug("[%s] announced new %s (%d chars); fetching body",
                      desc.node, desc.short, desc.char_len)
            node.request_refresh()

    def on_clip(self, payload: ClipPayload, *, _released: bool = False) -> None:
        """A clip was observed on payload.origin."""
        if self.paused or (payload.origin in self.paused_devices):
            log.debug("sync paused for %s; ignoring clip", payload.origin)
            return

        if (self.filter_previews and payload.is_image and not payload.is_camera and not _released
                and self._preview_gate(payload)):
            return

        if payload.is_camera:
            laptop = self.nodes.get("laptop")
            if laptop is not None:
                res = laptop.write(payload)
                log.info("[%s] camera photo delivered to laptop: %s", payload.origin, res)
                self._record(payload, [{"node": "laptop", "status": self._trace_status(res)}],
                             reason="camera photo backup")
            return

        with self._route_lock:
            sha = payload.sha256
            source = payload.origin

            if self.allow_recopy and self.dedup.is_recopy(
                    sha, source, min_interval_s=IMAGE_RECOPY_GUARD_S if payload.is_image else 0.3):
                log.info("[%s] local re-copy of %s; refreshing mesh", source, payload.short)
                self.dedup.prepare_for_redelivery(sha, source)

            is_new = self.dedup.observe(sha, source)

            # Tell the domain regardless of novelty: a clip arriving on the *standby*
            # member is exactly the evidence that Continuity is alive, and it is normally
            # not new (we wrote it to the representative moments earlier).
            domain = self.domain_of(source)
            for d in self.domains:
                d.note_observed(sha, source)

            # Record what's now current on the source device regardless of how this clip
            # ends up routed below: this is the persisted memory that lets a later
            # daemon restart tell "device unchanged since we last looked" apart from
            # "device changed while we were down", see on_node_connected().
            if self.baseline_store:
                prior_hash = self.baseline_store.get(source)
                self.baseline_store.set(source, sha)
            else:
                prior_hash = None

            if source in self._baseline_pending:
                self._baseline_pending.discard(source)
                changed_while_away = prior_hash is not None and prior_hash != sha
                if not self.adopt_on_connect and not changed_while_away:
                    # Adopt as the shared starting point without applying it anywhere:
                    # either this is genuinely the first time we have ever seen this
                    # device (prior_hash is None), or its content matches what we last
                    # recorded for it, so there is nothing new to sync either way.
                    for name in self.nodes:
                        self.dedup.mark_delivered(sha, name)
                    log.info("baseline from %s (%d chars), not applied",
                             source, payload.char_len)
                    return
                if changed_while_away:
                    log.info("[%s] clip changed while this daemon was not running "
                              "(%d chars); syncing it now instead of dropping it as a "
                              "baseline", source, payload.char_len)

            if payload.is_image and self._is_resaved_image(payload):
                self.stats["skipped"] += 1
                log.info("[%s] skipped %s: same picture as the one just received, re-saved by the device",
                         source, payload.short)
                self._record(payload, [], status="skipped",
                             reason="same screenshot re-saved by the device a moment later")
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
                self._record(payload, [], status="skipped", reason=reason)
                return

            now = time.time()
            stamp_domain = self.domain_of(self._last_stamp_node) if self._last_stamp_node else None
            if (self._last_stamp_node and domain is not None and stamp_domain is domain
                    and self._last_stamp_node != source
                    and (now - self._last_stamp_time <= 6.0)):
                origin_display = f"{self._last_stamp_node} (via {source})"
                self._last_stamp_node = None
            else:
                origin_display = source

            self._trace = []
            try:
                self._route(payload, source_domain=domain, origin_display=origin_display)
            finally:
                trace, self._trace = self._trace, None
            self._node_last_clip[source] = time.time()
            wipe = getattr(self.nodes.get("laptop"), "sensitive_wipe_s", None)
            note = (f"clears itself after {wipe:g} s"
                    if payload.sensitive and not self.direct_password_sync and wipe else "")
            self._record(payload, trace, reason=note)

    def _trace_status(self, result: str) -> str:
        return {WriteResult.OK: "delivered", WriteResult.DEFERRED: "deferred",
                WriteResult.UNAVAILABLE: "queued", WriteResult.FAILED: "queued",
                WriteResult.REJECTED: "rejected"}.get(result, "queued")

    @staticmethod
    def _same_shape(a, b) -> bool:
        """True when two (width, height) pairs have the same aspect ratio within 2 percent."""
        if not a or not b:
            return False
        ra, rb = a[0] / a[1], b[0] / b[1]
        return abs(ra - rb) <= 0.02 * max(ra, rb)

    def _preview_gate(self, payload: ClipPayload) -> bool:
        """Keep the small preview of a screenshot from being synced as a clip of its own.

        Returns True when this payload was dropped or is being held. A preview that arrives first
        waits a moment and is dropped if the full picture follows. One that arrives after the full
        picture is dropped at once. Anything else goes through unchanged."""
        dims = image_dimensions(payload.data)
        size = len(payload.data)
        origin = payload.origin
        now = time.time()
        area = dims[0] * dims[1] if dims else 0
        with self._preview_lock:
            held = self._pending_previews.get(origin)
            if held is not None:
                held_payload, held_dims, timer = held
                if dims and size > len(held_payload.data) * 2 and self._same_shape(dims, held_dims)                         and area >= 2 * held_dims[0] * held_dims[1]:
                    timer.cancel()
                    del self._pending_previews[origin]
                    self.stats["skipped"] += 1
                    log.info("[%s] dropped the preview %s: the full screenshot followed",
                             origin, held_payload.short)
                    self._preview_notes[origin] = "ignored the small preview Samsung adds"
                    return False
                timer.cancel()
                del self._pending_previews[origin]
                release_now = held_payload
            else:
                release_now = None
            late = self._recent_images.get(origin)
            if (release_now is None and dims and size < PREVIEW_MAX_BYTES and late is not None
                    and late[0] and now - late[2] <= PREVIEW_LOOKBACK_S and self._same_shape(dims, late[0])
                    and late[0][0] * late[0][1] >= 2 * area and late[1] > size * 2):
                self.stats["skipped"] += 1
                log.info("[%s] dropped the preview %s: it followed the full screenshot", origin, payload.short)
                self.activity.annotate_last_image(origin, "ignored the small preview Samsung adds")
                return True
            hold = bool(dims) and size < PREVIEW_MAX_BYTES
            if hold:
                timer = threading.Timer(self.preview_hold_s, self._release_preview, args=(origin,))
                timer.daemon = True
                self._pending_previews[origin] = (payload, dims, timer)
                timer.start()
        if release_now is not None:
            self.on_clip(release_now, _released=True)
        return hold

    def _release_preview(self, origin: str) -> None:
        with self._preview_lock:
            held = self._pending_previews.pop(origin, None)
        if held is not None:
            self.on_clip(held[0], _released=True)

    def _is_resaved_image(self, payload: ClipPayload) -> bool:
        """Samsung sometimes saves one screenshot twice a second apart with a different JPEG encoding,
        so the hashes differ but the picture is the same. Same device, same pixel size, almost the
        same byte size, within a few seconds, means the second one is that re-save."""
        dims = image_dimensions(payload.data)
        now = time.time()
        size = len(payload.data)
        last = self._recent_images.get(payload.origin)
        exif = has_exif(payload.data)
        if last is None or dims is None:
            self._recent_images[payload.origin] = (dims, size, now, payload.sha256, exif)
            return False
        l_dims, l_size, l_ts, l_sha, l_exif = last
        twin = (l_dims == dims and l_sha != payload.sha256 and now - l_ts <= RESAVE_WINDOW_S
                and abs(size - l_size) <= RESAVE_SIZE_TOLERANCE * max(size, l_size))
        if not twin:
            self._recent_images[payload.origin] = (dims, size, now, payload.sha256, exif)
            return False
        # The clipboard copy has no capture metadata, the file saved on the device does. Pixels are
        # the same, so keep the original file on disk in place of the stripped first copy.
        if exif and not l_exif:
            laptop = self.nodes.get("laptop")
            upgrade = getattr(laptop, "replace_last_screenshot", None)
            if upgrade is not None:
                try:
                    upgrade(payload)
                except Exception:
                    log.exception("could not swap in the original screenshot file")
            self._recent_images[payload.origin] = (dims, size, l_ts, payload.sha256, True)
        return True

    def _with_mirrored(self, trace: list[dict]) -> list[dict]:
        """Samsung Continuity copies a clip between the members of a sync group on its own, so a
        delivery to the representative also reaches its group mates. Show them, labelled as mirrored."""
        out = list(trace)
        seen = {t["node"] for t in out}
        for t in trace:
            if t["status"] not in ("delivered", "deferred"):
                continue
            for domain in getattr(self, "domains", []):
                if not domain.contains(t["node"]):
                    continue
                for m in domain.members:
                    if m.name not in seen and not self.is_device_paused(m.name):
                        out.append({"node": m.name, "status": "mirrored"})
                        seen.add(m.name)
        return out

    def _record(self, payload: ClipPayload, trace: list[dict], *, status: str | None = None,
                reason: str = "") -> None:
        try:
            trace = self._with_mirrored(trace)
            if not trace and status is None:
                # Nothing was left to send: every device already had this clip (a restart or a repeat copy).
                status, reason = "insync", reason or "already on every device"
            kind = "camera" if payload.is_camera else "image" if payload.is_image else "text"
            if kind == "image":
                note = self._preview_notes.pop(payload.origin, "")
                reason = f"{reason}. {note}" if reason and note else reason or note
            event = self.activity.record(
                kind=kind, size=payload.char_len if kind == "text" else payload.size,
                origin=payload.origin, targets=trace, status=status, reason=reason,
                latency_ms=max(0, int((time.time() - payload.ts) * 1000)),
                fingerprint=payload.short, sensitive=payload.sensitive, text=payload.text)
            lookup = getattr(self.nodes.get("laptop"), "saved_path", None)
            if kind == "image" and not payload.sensitive and lookup is not None:
                event.file = lookup(payload.sha256)
        except Exception:
            log.exception("activity record failed")  # never let diagnostics break routing

    #: outbound ----------------------------------------------------------

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
        already held), so callers: specifically _drain_outbox: know whether to clear the
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
            # real write: redundantly re-arming it for an already-satisfied node makes the
            # health probe time out every grace_s against a device with nothing new to
            # confirm, which falsely trips the domain into "degraded" after fail_threshold
            # misses even though nothing is actually wrong.
            fresh = self.dedup.should_deliver(sha, node.name)
            # queue_on_failure=False: a domain target that fails is this function's problem,
            # not _deliver_to_node's. Queuing at the node level here used to leave a stray
            # "node:<name>" entry alongside the "domain:<name>" one below for the exact same
            # payload: retrying the node-level entry writes straight back to that specific
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
        if self.is_device_paused(node.name):
            log.debug("skipping delivery to %s; device is paused", node.name)
            if self._trace is not None:
                self._trace.append({"node": node.name, "status": "paused"})
            return True
        sha = payload.sha256
        display = origin_display or payload.origin
        if not self.dedup.should_deliver(sha, node.name):
            # Nothing outstanding for this node: treat as satisfied, not failed. Returning
            # False here used to make a stale outbox entry for an already-delivered payload
            # re-queue itself forever (every tick, with its own log line and disk write)
            # until the dedup record's TTL expired, up to 90s later: which is exactly how
            # a long-since-superseded write could fire a "phantom" delivery well after the
            # fact.
            log.debug("%s already holds %s", node.name, payload.short)
            return True

        result = node.write(payload)
        if self._trace is not None:
            self._trace.append({"node": node.name, "status": self._trace_status(result)})

        if result in (WriteResult.OK, WriteResult.DEFERRED):
            # Mark delivered on DEFERRED too: ClipBridge has taken custody and replays on
            # unlock, so re-sending later would double-apply.
            self.dedup.mark_delivered(sha, node.name)
            if self.baseline_store:
                self.baseline_store.set(node.name, sha)
            self.stats["delivered"] += 1
            size_str = f"{payload.char_len} bytes [image]" if payload.is_image else f"{payload.char_len} chars"
            log.info("%s -> %s: %s%s", display, node.name, size_str,
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

    #: recovery ----------------------------------------------------------

    def _drain_outbox(self) -> None:
        # Locked per entry, not for the whole drain: node.write() can now block for real
        # (waiting on an ACK), and AndroidNode's ack-correlation state is not itself safe
        # against two writes to the same node overlapping, so each individual delivery
        # attempt still needs to be atomic against on_clip()'s _route() chain. But holding
        # the lock for the *entire* loop let one slow or genuinely-still-failing entry starve
        # a concurrent, unrelated on_clip() call for as long as the whole backlog took to
        # walk: narrowing to per-entry keeps the same race protection with a much smaller
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
                    if self.is_device_paused(ident):
                        # Keep the queued clip for resume; clearing it here lost it silently.
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
            for domain in self.domains:
                if domain.contains(node.name):
                    try:
                        domain.apply_modes()
                    except Exception:
                        log.exception("apply_modes on unlock failed for domain %s", domain.name)
            self._drain_outbox()

    #: diagnostics -------------------------------------------------------

    def status(self) -> dict:
        return {
            "paused": self.paused,
            "paused_devices": list(self.paused_devices),
            "nodes": {n.name: {
                "available": n.available,
                "locked": n.locked,
                "paused": self.is_device_paused(n.name),
                "last_error": getattr(n, "last_error", None),
                "last_clip_ts": self._node_last_clip.get(n.name),
            } for n in self.nodes.values()},
            "uptime_s": int(time.time() - self.started_at),
            "domains": [d.status() for d in self.domains],
            "outbox": self.outbox.keys(),
            "stats": dict(self.stats),
        }
