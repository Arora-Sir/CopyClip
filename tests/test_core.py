"""Fast unit tests for the routing logic. No devices, no network, runs in well under a second.

The three-device rules (dedup across a Samsung sync domain, representative election,
Continuity health, outbox durability) are the parts most likely to break subtly, and they
are exactly the parts that are painful to exercise against real hardware. They are pure
logic by design, so they get tested as pure logic.

    python tests\\test_core.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time

# Two tests deliberately trigger error paths (a corrupt outbox, a degraded domain). Their
# log output is expected, so silence it rather than have it read as a real failure.
logging.disable(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.dedup import RecentClips              # noqa: E402
from core.models import ClipPayload, WriteResult  # noqa: E402
from core.outbox import Outbox                  # noqa: E402
from core.policy import Policy                  # noqa: E402
from domains.sync_domain import SyncDomain      # noqa: E402
from nodes.base import ClipboardNode            # noqa: E402
from nodes.windows_node import WindowsNode      # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeNode(ClipboardNode):
    """Minimal ClipboardNode for exercising domain and hub logic."""

    def __init__(self, name, available=True, locked=False):
        super().__init__(name)
        self._available = available
        self._locked = locked
        self.writes: list[ClipPayload] = []
        self.result = WriteResult.OK

    def start(self): pass
    def stop(self): pass

    @property
    def available(self): return self._available

    @property
    def locked(self): return self._locked

    def write(self, payload):
        self.writes.append(payload)
        return self.result


# --------------------------------------------------------------- payload

def test_payload():
    p = ClipPayload.from_text("hello", origin="laptop")
    check("payload identity is stable", p.sha256 == ClipPayload.from_text("hello", "x").sha256,
          "hash must not depend on origin")
    check("payload roundtrips through dict",
          ClipPayload.from_dict(p.to_dict()).data == p.data)

    uni = ClipPayload.from_text("नमस्ते 🎯\ttab\r\nline", origin="phone")
    check("unicode survives roundtrip",
          ClipPayload.from_dict(uni.to_dict()).text == uni.text)
    check("char_len counts characters not bytes",
          ClipPayload.from_text("🎯🎯", "x").char_len == 2,
          "emoji are 4 bytes each in utf-8")


# ----------------------------------------------------------------- dedup

def test_dedup():
    d = RecentClips(ttl=60)
    p = ClipPayload.from_text("abc", origin="phone")
    sha = p.sha256

    check("first sighting is new", d.observe(sha, "phone") is True)
    check("second sighting is not new", d.observe(sha, "tab") is False)
    check("never returns a clip to a node that has it", d.should_deliver(sha, "phone") is False)
    check("delivers to a node that lacks it", d.should_deliver(sha, "laptop") is True)

    d.mark_delivered(sha, "laptop")
    check("delivered node is recorded", d.should_deliver(sha, "laptop") is False)

    # The scenario v1's single last_hash could not survive: a second, unrelated clip
    # arrives between the original and its Samsung echo.
    other = ClipPayload.from_text("xyz", origin="laptop").sha256
    d.observe(other, "laptop")
    check("older clip still tracked after a newer one arrives",
          d.should_deliver(sha, "phone") is False,
          "v1's single last_hash would have been overwritten here")

    expiring = RecentClips(ttl=0.3)
    expiring.observe(sha, "phone")
    time.sleep(0.45)
    check("entries expire so re-copying the same text syncs again",
          expiring.should_deliver(sha, "phone") is True)


# ---------------------------------------------------------------- outbox

def test_outbox():
    tmp = tempfile.mkdtemp(prefix="copyclip-test-")
    try:
        path = os.path.join(tmp, "state", "outbox.json")
        ob = Outbox(path)
        p1 = ClipPayload.from_text("first", origin="laptop")
        p2 = ClipPayload.from_text("second", origin="laptop")

        ob.put("domain:samsung", p1)
        check("outbox holds a pending write", ob.peek("domain:samsung").text == "first")

        ob.put("domain:samsung", p2)
        check("newer clip supersedes older (single slot)",
              ob.peek("domain:samsung").text == "second",
              "the clipboard holds one item; a backlog would paste the wrong thing")

        check("outbox persisted to disk", os.path.isfile(path))

        # Simulate a daemon restart.
        reloaded = Outbox(path)
        check("survives a restart", reloaded.peek("domain:samsung").text == "second")

        reloaded.clear("domain:samsung")
        check("clear removes the slot", reloaded.peek("domain:samsung") is None)
        check("cleared state persists", Outbox(path).is_empty())

        # A corrupt file must not stop the daemon starting.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        check("corrupt outbox degrades to empty rather than crashing",
              Outbox(path).is_empty())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_outbox_put_skips_unchanged_reput():
    """Regression: re-arming the exact same content under a key used to fsync to disk and
    log on every call, no matter how many times a retry loop called put() for content that
    hadn't changed -- real, avoidable CPU/IO cost on every failed retry tick.
    """
    tmp = tempfile.mkdtemp(prefix="copyclip-outboxreput-")
    try:
        path = os.path.join(tmp, "state", "outbox.json")
        ob = Outbox(path)
        p = ClipPayload.from_text("same content", origin="laptop")

        ob.put("domain:samsung", p)
        mtime_after_first = os.path.getmtime(path)

        time.sleep(0.05)
        ob.put("domain:samsung", p)   # identical content, same key -- should be a no-op
        mtime_after_second = os.path.getmtime(path)

        check("re-arming identical content does not touch disk again",
              mtime_after_second == mtime_after_first)
        check("slot still holds exactly the one entry",
              ob.peek("domain:samsung").text == "same content")

        different = ClipPayload.from_text("different content", origin="laptop")
        ob.put("domain:samsung", different)
        check("genuinely different content still gets saved",
              os.path.getmtime(path) != mtime_after_first)
        check("slot now holds the new content", ob.peek("domain:samsung").text
              == "different content")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_outbox_image_sidecar():
    """Image payloads write a sidecar file (state/outbox/<key>.bin) instead of base64
    inline in outbox.json -- a queued 24 MiB image inline would mean a ~32 MiB fsync on
    every retry tick. Text payloads (tested above) are completely unaffected -- still
    fully inline.
    """
    tmp = tempfile.mkdtemp(prefix="copyclip-outbox-sidecar-")
    try:
        path = os.path.join(tmp, "state", "outbox.json")
        sidecar_dir = os.path.join(tmp, "state", "outbox")
        data = bytes(range(256)) * 100

        ob = Outbox(path)
        image = ClipPayload(mime="image/png", data=data, origin="phone")
        ob.put("domain:samsung", image)

        import json as _json
        with open(path, encoding="utf-8") as fh:
            raw = _json.load(fh)
        check("image entry marked as sidecar, no inline data_b64",
              raw["domain:samsung"].get("sidecar") is True
              and "data_b64" not in raw["domain:samsung"])
        files = os.listdir(sidecar_dir)
        check("exactly one sidecar file written", len(files) == 1, files)
        with open(os.path.join(sidecar_dir, files[0]), "rb") as fh:
            check("sidecar bytes match the original payload", fh.read() == data)

        reloaded = Outbox(path)
        restored = reloaded.peek("domain:samsung")
        check("image payload restored across a simulated restart",
              restored is not None and restored.data == data)

        different = ClipPayload(mime="image/jpeg", data=data[::-1], origin="phone")
        reloaded.put("domain:samsung", different)
        check("same key overwrites the sidecar in place, not accumulated",
              len(os.listdir(sidecar_dir)) == 1)

        reloaded.clear("domain:samsung")
        check("clear() prunes the sidecar file too",
              len(os.listdir(sidecar_dir)) == 0 if os.path.isdir(sidecar_dir) else True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_outbox_sidecar_integrity():
    """A missing or tampered sidecar must drop the stale entry rather than serve corrupt
    or absent data -- reassembly failure fails closed, same principle as the chunked wire
    protocol's sha256 verification.
    """
    tmp = tempfile.mkdtemp(prefix="copyclip-outbox-integrity-")
    try:
        path = os.path.join(tmp, "state", "outbox.json")
        sidecar_dir = os.path.join(tmp, "state", "outbox")

        ob = Outbox(path)
        ob.put("node:tab", ClipPayload(mime="image/png", data=b"real bytes" * 500,
                                       origin="phone"))
        for f in os.listdir(sidecar_dir):
            os.unlink(os.path.join(sidecar_dir, f))
        check("missing sidecar drops the entry, not a crash",
              Outbox(path).peek("node:tab") is None)

        ob2 = Outbox(path)
        ob2.put("node:tab", ClipPayload(mime="image/png", data=b"trustworthy" * 500,
                                        origin="phone"))
        for f in os.listdir(sidecar_dir):
            with open(os.path.join(sidecar_dir, f), "wb") as fh:
                fh.write(b"tampered!!" * 500)
        check("sha256 mismatch drops the entry rather than serving corrupt data",
              Outbox(path).peek("node:tab") is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_outbox_sensitive_zero_disk():
    """Sensitive payloads must NEVER be written to outbox.json or sidecars on disk.
    They remain in volatile RAM only and are cleared by drop_sensitive().
    """
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "outbox.json")
        ob = Outbox(path)
        secret_text = "SECRET_OTP_TEST_123456"
        p_sens = ClipPayload.from_text(secret_text, "phone", sensitive=True)
        ob.put("node:tab", p_sens)

        check("sensitive payload held in outbox memory", ob.peek("node:tab") is not None)
        check("sensitive payload content matches in memory",
              ob.peek("node:tab").text == secret_text)

        # Zero disk persistence verification
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                disk_data = json.load(fh)
            check("sensitive slot excluded from disk outbox.json", "node:tab" not in disk_data)
            with open(path, "r", encoding="utf-8") as fh:
                raw_disk = fh.read()
            check("sensitive plaintext never written to disk", secret_text not in raw_disk)

        # drop_sensitive purges RAM
        dropped = ob.drop_sensitive()
        check("drop_sensitive purged 1 slot", dropped == 1)
        check("outbox is now empty in memory", ob.is_empty())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- policy

def test_policy():
    pol = Policy(max_chars=100, denylist_regex=[r"BEGIN OPENSSH PRIVATE KEY"])
    check("normal text allowed",
          pol.reject_reason(ClipPayload.from_text("hello", "x")) is None)
    check("empty rejected",
          pol.reject_reason(ClipPayload.from_text("", "x")) == "empty")
    check("oversize rejected whole, not truncated",
          "too large" in pol.reject_reason(ClipPayload.from_text("y" * 101, "x")))
    check("denylist honoured",
          "denylist" in pol.reject_reason(
              ClipPayload.from_text("-----BEGIN OPENSSH PRIVATE KEY-----", "x")))
    check("sensitive clips rejected when allow_sensitive is false",
          "sensitive" in pol.reject_reason(
              ClipPayload.from_text("hunter2", "x", sensitive=True)))
    pol_sens = Policy(allow_sensitive=True)
    check("sensitive clips allowed when allow_sensitive is true",
          pol_sens.reject_reason(ClipPayload.from_text("hunter2", "x", sensitive=True)) is None)
    check("non-text, non-image content rejected while allow_non_text is false",
          "non-text" in pol.reject_reason(
              ClipPayload(mime="application/pdf", data=b"%PDF", origin="x")))


def test_policy_images():
    """Images are governed by their own allow_images flag and max_image_bytes cap --
    independent of allow_non_text/max_chars, which stay text's own knobs unchanged.
    """
    off = Policy(allow_images=False)
    check("images rejected by default (allow_images=False)",
          "images disabled" in off.reject_reason(
              ClipPayload(mime="image/png", data=b"x" * 100, origin="phone")))

    on = Policy(allow_images=True, max_image_bytes=1000)
    check("image within cap allowed",
          on.reject_reason(ClipPayload(mime="image/png", data=b"x" * 100, origin="phone"))
          is None)
    check("oversize image rejected whole, not truncated",
          "too large" in on.reject_reason(
              ClipPayload(mime="image/jpeg", data=b"x" * 2000, origin="phone")))
    check("image size cap is independent of max_chars",
          on.reject_reason(ClipPayload(mime="image/gif", data=b"x" * 999, origin="phone"))
          is None, "max_chars defaults to 800_000, far above this -- confirms the image "
                    "cap, not max_chars, is what's actually being enforced")

    sensitive_denylist = Policy(allow_images=True, denylist_regex=[r"SECRET"])
    check("denylist (text-only) never matches against raw image bytes",
          sensitive_denylist.reject_reason(
              ClipPayload(mime="image/png", data=b"...SECRET...", origin="phone")) is None,
          "an image that happens to contain matching bytes must still sync -- the "
          "denylist only ever inspects payload.text, which is None for an image")

    check("sensitive-flag rejection still applies to images",
          "sensitive" in on.reject_reason(
              ClipPayload(mime="image/png", data=b"x" * 10, origin="phone", sensitive=True)))


# ---------------------------------------------------------------- domain

def test_domain():
    phone, tab = FakeNode("phone"), FakeNode("tab")
    dom = SyncDomain("samsung", [phone, tab])

    check("prefers the first writable member", dom.representative().name == "phone")
    check("healthy domain writes to exactly one device",
          [n.name for n in dom.write_targets()] == ["phone"],
          "this is the no-redundant-calls guarantee")

    # The real robustness win: Android discards writes behind the keyguard, so a locked
    # phone must hand the job to the awake tablet.
    phone._locked = True
    check("locked phone hands over to the awake tablet",
          dom.representative().name == "tab")

    phone._locked = False
    phone._available = False
    check("offline phone hands over to the tablet",
          dom.representative().name == "tab")
    phone._available = True

    # Continuity health state machine.
    dom.note_write("hash-1", phone)
    dom.note_observed("hash-1", "tab")
    check("propagation confirmed by the other member", dom.healthy is True)

    dom.grace_s = 0.01
    for i in range(3):
        dom.note_write(f"hash-miss-{i}", phone)
        time.sleep(0.02)
        dom.tick()
    check("repeated propagation failures mark Continuity degraded", dom.healthy is False)
    check("degraded domain writes to every reachable member",
          sorted(n.name for n in dom.write_targets()) == ["phone", "tab"])

    dom.note_write("hash-2", phone)
    dom.note_observed("hash-2", "tab")
    check("Continuity recovery restores single-target writes",
          dom.healthy is True and [n.name for n in dom.write_targets()] == ["phone"])

    # Confirmation from the device we wrote to proves nothing about propagation.
    solo = SyncDomain("s", [FakeNode("a"), FakeNode("b")])
    solo.note_write("h", solo.members[0])
    solo.note_observed("h", "a")
    solo.grace_s = 0.01
    time.sleep(0.02)
    solo.tick()
    check("self-confirmation does not count as propagation",
          solo._consecutive_misses == 1)


# ------------------------------------------------------------- hub stamp
def test_hub_stamp_attribution():
    from core.hub import Hub
    laptop, phone, tab = FakeNode("laptop"), FakeNode("phone"), FakeNode("tab")
    dom = SyncDomain("samsung", [phone, tab])
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-hubtest-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        hub = Hub(nodes=[laptop, phone, tab], domains=[dom], policy=pol, outbox=ob)
        hub.on_stamp("tab", "100")
        check("stamp records recent node", hub._last_stamp_node == "tab")

        p = ClipPayload.from_text("hello from tab via phone", origin="phone")
        hub.on_clip(p)
        check("laptop received clip from phone", len(laptop.writes) == 1)
        check("stamp cleared after correlation", hub._last_stamp_node is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------ locked domain member

def test_hub_routes_around_locked_domain_member():
    """A locked phone must not block laptop -> tablet delivery.

    SyncDomain.representative() already proves the *choice* switches to the tablet
    (test_domain, above). This proves the hub actually *acts* on that choice: the tablet's
    write() fires and the locked phone's does not.
    """
    from core.hub import Hub

    laptop, phone, tab = FakeNode("laptop"), FakeNode("phone", locked=True), FakeNode("tab")
    dom = SyncDomain("samsung", [phone, tab])
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-lockedtest-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        hub = Hub(nodes=[laptop, phone, tab], domains=[dom], policy=pol, outbox=ob)

        p = ClipPayload.from_text("laptop copy while phone is locked", origin="laptop")
        hub.on_clip(p)

        check("tablet received the write", len(tab.writes) == 1 and tab.writes[0] is p)
        check("locked phone received nothing", len(phone.writes) == 0)
        check("delivered stat incremented", hub.stats["delivered"] == 1)

        # Bonus: the other untested branch of the same function -- both domain members
        # unreachable means the clip is queued, not dropped (this is the exact class of bug
        # BUG-002 fixed for single nodes; the domain path deserves the same proof). Locked
        # alone is not enough here: representative()'s fallback loop still hands an
        # available-but-locked phone the job, so both must be unavailable to empty targets.
        phone._available = False
        tab._available = False
        p2 = ClipPayload.from_text("laptop copy while everything is unreachable",
                                    origin="laptop")
        hub.on_clip(p2)
        check("fully-blocked domain queues rather than drops",
              ob.peek("domain:samsung") is not None
              and ob.peek("domain:samsung").text == p2.text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_repeat_copy_reaches_node_missed_by_first_delivery():
    """Regression: re-copying identical text must still reach a node the first delivery
    missed, not be silently dropped as an 'echo' just because the hash repeats.
    """
    from core.hub import Hub

    laptop, phone, tab = FakeNode("laptop"), FakeNode("phone"), FakeNode("tab")
    dom = SyncDomain("samsung", [phone, tab])
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-repeattest-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        hub = Hub(nodes=[laptop, phone, tab], domains=[dom], policy=pol, outbox=ob)

        p = ClipPayload.from_text("same word", origin="laptop")
        hub.on_clip(p)
        check("first copy delivered to the representative (phone)", len(phone.writes) == 1)
        check("tablet did not also get it (single-representative optimization holds)",
              len(tab.writes) == 0)

        # Phone goes unreachable, mirroring a mid-copy Tailscale drop.
        phone._available = False

        p2 = ClipPayload.from_text("same word", origin="laptop")   # identical text/sha
        hub.on_clip(p2)
        check("re-copying identical text still reaches the tablet once phone is gone",
              len(tab.writes) == 1)
        check("unreachable phone was not written to again", len(phone.writes) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_domain_failure_queues_once_not_twice():
    """Regression: a failed domain write used to queue BOTH 'node:<name>' and
    'domain:<name>' for the same payload. The stray node-level entry bypassed the domain's
    own representative re-election on retry and could survive to fire a stale rewrite well
    after the domain had recovered -- part of what produced a real phone<->tablet loop.
    """
    from core.hub import Hub

    laptop, phone, tab = FakeNode("laptop"), FakeNode("phone"), FakeNode("tab")
    phone.result = WriteResult.FAILED
    dom = SyncDomain("samsung", [phone, tab])
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-dqueue-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        hub = Hub(nodes=[laptop, phone, tab], domains=[dom], policy=pol, outbox=ob)

        p = ClipPayload.from_text("will fail", origin="laptop")
        hub.on_clip(p)

        check("domain-level entry queued", ob.peek("domain:samsung") is not None)
        check("no stray node-level entry for the failed representative",
              ob.peek("node:phone") is None)
        check("phone write was actually attempted", len(phone.writes) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_domain_delivery_already_satisfied_is_not_requeued():
    """Regression: an outbox entry that becomes satisfied by some other path (e.g. the
    representative already got this exact content through a different delivery) used to be
    indistinguishable from a genuine failure -- _deliver_to_node returned False either way,
    so _deliver_to_domain kept re-queuing (and re-logging, re-saving) it every tick until the
    dedup record's 90s TTL expired. That is also how a long-stale write could fire a
    "phantom" delivery well after the fact. Also verifies the fix doesn't call note_write for
    the already-satisfied case, which would otherwise falsely trip the domain into
    "degraded" (a health probe armed for content nobody needs to re-confirm just times out).
    """
    from core.hub import Hub

    laptop, phone, tab = FakeNode("laptop"), FakeNode("phone"), FakeNode("tab")
    dom = SyncDomain("samsung", [phone, tab])
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-alreadysat-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        hub = Hub(nodes=[laptop, phone, tab], domains=[dom], policy=pol, outbox=ob)

        p = ClipPayload.from_text("already delivered elsewhere", origin="laptop")
        hub.dedup.mark_delivered(p.sha256, "phone")   # phone already has this, per dedup

        satisfied = hub._deliver_to_domain(dom, p)

        check("reported as satisfied, not needing retry", satisfied is True)
        check("no write attempted against the node that already has it",
              len(phone.writes) == 0)
        check("nothing queued to the outbox", ob.peek("domain:samsung") is None)
        check("no health probe armed for a no-op delivery", dom._probe is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hub_restart_survives_baseline_drop():
    """Regression: core/hub.py's baseline-adoption-on-first-connect used to be purely
    in-memory (`_baselined`), so a fresh process -- including a spurious restart of an
    already-healthy daemon -- treated a device's current clip as never-seen-before and
    silently dropped it instead of syncing it. Measured live: AHK relaunching CopyClip
    mid-session dropped a clip this way twice in the same boot.

    A BaselineStore backed by the same on-disk file across three separate Hub instances
    simulates the daemon dying and restarting: each later Hub has no in-memory knowledge
    of the earlier one's state, only what was persisted to disk.
    """
    from core.baseline_store import BaselineStore
    from core.hub import Hub

    tmp = tempfile.mkdtemp(prefix="copyclip-restartbaseline-")
    try:
        baseline_path = os.path.join(tmp, "baseline.json")

        # -- "boot 1": daemon starts, phone connects, syncs one clip normally.
        laptop1, phone1 = FakeNode("laptop"), FakeNode("phone")
        ob1 = Outbox(os.path.join(tmp, "outbox1.json"))
        hub1 = Hub(nodes=[laptop1, phone1], domains=[], policy=Policy(), outbox=ob1,
                   baseline_store=BaselineStore(baseline_path))
        hub1.on_node_connected(phone1)
        p1 = ClipPayload.from_text("first thing copied on phone", origin="phone")
        hub1.on_clip(p1)
        check("boot 1: first-ever connect drops the clip as baseline (no prior record)",
              len(laptop1.writes) == 0)

        p2 = ClipPayload.from_text("second thing, copied normally after connect",
                                    origin="phone")
        hub1.on_clip(p2)
        check("boot 1: a later clip on the same connection syncs normally",
              len(laptop1.writes) == 1 and laptop1.writes[0].text == p2.text)

        # -- daemon restarts (e.g. AHK relaunching it). New process, new Hub, new
        # in-memory state -- but the SAME on-disk baseline file.
        laptop2, phone2 = FakeNode("laptop"), FakeNode("phone")
        ob2 = Outbox(os.path.join(tmp, "outbox2.json"))
        hub2 = Hub(nodes=[laptop2, phone2], domains=[], policy=Policy(), outbox=ob2,
                   baseline_store=BaselineStore(baseline_path))
        hub2.on_node_connected(phone2)

        # Phone's clip is unchanged since boot 1 (still p2's text) -- nothing new to sync.
        hub2.on_clip(ClipPayload.from_text(p2.text, origin="phone"))
        check("boot 2: unchanged content since the last record is correctly skipped, "
              "not re-treated as a fresh sync", len(laptop2.writes) == 0)

        # -- daemon restarts again. This time something WAS copied on the phone while it
        # was down/mid-restart, so the very next thing seen from phone is genuinely new.
        laptop3, phone3 = FakeNode("laptop"), FakeNode("phone")
        ob3 = Outbox(os.path.join(tmp, "outbox3.json"))
        hub3 = Hub(nodes=[laptop3, phone3], domains=[], policy=Policy(), outbox=ob3,
                   baseline_store=BaselineStore(baseline_path))
        hub3.on_node_connected(phone3)
        p3 = ClipPayload.from_text("copied while the daemon was restarting", origin="phone")
        hub3.on_clip(p3)
        check("boot 3: a clip that changed while the daemon was down is applied, "
              "not silently dropped as a baseline",
              len(laptop3.writes) == 1 and laptop3.writes[0].text == p3.text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_drain_outbox_serializes_with_route_lock():
    """Regression: _drain_outbox() used to run without route_lock held, so a tick-thread
    retry could race a concurrently in-flight on_clip()-triggered write to the same node --
    AndroidNode's ACK-correlation state has no lock of its own and assumes only one write
    is ever in flight. Verified here by holding route_lock and confirming drain blocks.

    Needs a real queued entry: the lock is now taken per outbox key (not once for the whole
    drain), so an empty outbox would never touch route_lock at all and this test would pass
    for the wrong reason.
    """
    from core.hub import Hub

    laptop = FakeNode("laptop")
    pol = Policy()
    tmp = tempfile.mkdtemp(prefix="copyclip-drainlock-")
    try:
        ob = Outbox(os.path.join(tmp, "outbox.json"))
        ob.put("node:laptop", ClipPayload.from_text("queued", origin="phone"))
        hub = Hub(nodes=[laptop], domains=[], policy=pol, outbox=ob)

        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with hub._route_lock:
                entered.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        entered.wait(timeout=2)

        drained = threading.Event()

        def do_drain():
            hub._drain_outbox()
            drained.set()

        drainer = threading.Thread(target=do_drain)
        drainer.start()
        blocked = not drained.wait(timeout=0.3)
        check("drain_outbox blocks while route_lock is held elsewhere", blocked)

        release.set()
        holder.join(timeout=2)
        drainer.join(timeout=2)
        check("drain_outbox completes once the lock is released", drained.is_set())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------- single instance

def test_single_instance():
    """BUG-003: a second daemon must be refused.

    Two daemons is not a benign duplicate. Each independently watches the Windows clipboard
    and independently writes to the devices, so one copy on the laptop lands on the phone
    TWICE, and an incoming clip is applied by whichever instance wins the race, leaving the
    other console silent. It reads as flaky sync, not as a duplicate process.
    """
    from core.single_instance import AlreadyRunning, SingleInstance

    tmp = tempfile.mkdtemp(prefix="copyclip-si-")
    try:
        pidfile = os.path.join(tmp, "state", "copyclip.pid")
        test_mutex = f"Local\\CopyClipTest_{os.getpid()}"

        first = SingleInstance(pidfile, mutex_name=test_mutex)
        first.acquire()
        check("first instance acquires the singleton", True)
        check("pid file written", os.path.isfile(pidfile))

        second = SingleInstance(pidfile, mutex_name=test_mutex)
        try:
            second.acquire()
            second.release()
            check("second instance is refused", False, "it was allowed to start")
        except AlreadyRunning as exc:
            check("second instance is refused", True, f"error names PID {exc.pid}")

        first.release()

        third = SingleInstance(pidfile, mutex_name=test_mutex)
        try:
            third.acquire()
            check("singleton is reusable after a clean release", True)
            third.release()
        except AlreadyRunning:
            check("singleton is reusable after a clean release", False,
                  "the guard leaked and would block every future start")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- winclip

def test_winclip_concurrency():
    """BUG-002: concurrent clipboard access must not raise.

    The daemon writes from a device node's pump thread while the watcher thread reads on
    WM_CLIPBOARDUPDATE. The Windows clipboard is a single system-wide exclusive resource,
    so before the process-wide lock and whole-operation retry, a mid-sequence failure
    (EmptyClipboard returning ERROR_CLIPBOARD_NOT_OPEN) aborted the write and the clip was
    silently lost.

    Saves and restores whatever is on the clipboard so running the suite is not destructive.
    """
    import threading as _t
    import winclip

    try:
        original = winclip.read_text()
    except Exception:
        original = None

    errors: list[Exception] = []
    written: list[str] = []

    def writer(tag: str, n: int) -> None:
        for i in range(n):
            try:
                value = f"{tag}-{i}-" + "x" * 500
                winclip.write_text(value)
                written.append(value)
            except Exception as exc:
                errors.append(exc)

    def reader(n: int) -> None:
        for _ in range(n):
            try:
                winclip.read_text()
            except winclip.ClipboardBusy:
                pass                      # legitimate: another process held it
            except Exception as exc:
                errors.append(exc)

    threads = [_t.Thread(target=writer, args=("A", 15)),
               _t.Thread(target=writer, args=("B", 15)),
               _t.Thread(target=reader, args=(30,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    check("concurrent clipboard access raises nothing",
          not errors, f"{len(errors)} error(s): {errors[:2]}")
    check("all concurrent writes completed", len(written) == 30, f"{len(written)}/30")

    final = winclip.read_text()
    # Accept None: an external process (daemon, another test) may have cleared the clipboard
    # between the last write and this read. An empty clipboard is consistent; what we guard
    # against is a partial/corrupt value that is not in the written set and not None.
    ok = final is None or final in written
    check("clipboard left in a consistent state", ok,
          "" if ok else f"final value is not any of the writes: {str(final)[:40]!r}")


    if original is not None:
        try:
            winclip.write_text(original)
        except Exception:
            pass


def test_write_image_excludes_from_clipboard_history():
    """A device-synced image must not appear in (or evict entries from) Windows Clipboard
    History -- see bugs/BUG-005's Open points for the full investigation. This checks that
    CF_CAN_INCLUDE_IN_HISTORY=0 is actually set alongside the image data; whether that
    genuinely stops History from evicting other entries is real-use verification, not
    something a unit test can observe (there's no API to inspect History's own state).

    Saves and restores whatever is on the clipboard so running the suite is not destructive.
    """
    import winclip
    import winimage

    time.sleep(0.2)
    try:
        original = winclip.read_text()
    except Exception:
        original = None

    w, h = 4, 4
    bgra = bytes([100, 150, 200, 255]) * (w * h)
    png = winimage.png_from_bgra(bgra, w, h, has_alpha=True)

    winclip.write_image(png, "image/png")

    with winclip._Opened():
        available = bool(winclip.user32.IsClipboardFormatAvailable(
            winclip.CF_CAN_INCLUDE_IN_HISTORY))
        value = winclip._read_global(winclip.CF_CAN_INCLUDE_IN_HISTORY) if available else None

    check("CanIncludeInClipboardHistory format is set on a device-synced image", available)
    check("CanIncludeInClipboardHistory value is 0 (excluded)",
          value == (0).to_bytes(4, "little"), repr(value))

    # exclude_from_history=False (config's screenshot_exclude_from_history toggle) must
    # skip setting the format entirely, not set it with a different value.
    winclip.write_image(png, "image/png", exclude_from_history=False)
    with winclip._Opened():
        available_off = bool(winclip.user32.IsClipboardFormatAvailable(
            winclip.CF_CAN_INCLUDE_IN_HISTORY))
    check("exclude_from_history=False does not set the format at all", not available_off)

    if original is not None:
        try:
            winclip.write_text(original)
        except Exception:
            pass


def test_write_text_sensitive_excludes_from_history_and_cloud():
    """A sensitive text clip must carry both CanIncludeInClipboardHistory=0 and
    CanUploadToCloudClipboard=0 on Windows.
    Saves and restores whatever was on the clipboard.
    """
    import winclip

    try:
        original = winclip.read_text()
    except Exception:
        original = None

    secret = "OTP_TEST_998877"
    winclip.write_text_sensitive(secret)

    with winclip._Opened():
        hist_avail = bool(winclip.user32.IsClipboardFormatAvailable(
            winclip.CF_CAN_INCLUDE_IN_HISTORY))
        hist_val = winclip._read_global(winclip.CF_CAN_INCLUDE_IN_HISTORY) if hist_avail else None

        cloud_avail = bool(winclip.user32.IsClipboardFormatAvailable(
            winclip.CF_CAN_UPLOAD_TO_CLOUD))
        cloud_val = winclip._read_global(winclip.CF_CAN_UPLOAD_TO_CLOUD) if cloud_avail else None

    check("CanIncludeInClipboardHistory format is set on sensitive text", hist_avail)
    check("CanIncludeInClipboardHistory value is 0 (excluded)",
          hist_val == (0).to_bytes(4, "little"), repr(hist_val))
    check("CanUploadToCloudClipboard format is set on sensitive text", cloud_avail)
    check("CanUploadToCloudClipboard value is 0 (excluded)",
          cloud_val == (0).to_bytes(4, "little"), repr(cloud_val))
    check("clipboard text matches secret", winclip.read_text() == secret)

    if original is not None:
        try:
            winclip.write_text(original)
        except Exception:
            pass


# ---------------------------------------------------------------- screenshot backup

def test_screenshot_backup():
    """Stopgap local-file backup for the Windows 11 25H2 Snipping Tool double-write bug
    (see ARCHITECTURE.md's "Two entries in Windows Clipboard History" note).
    """
    tmp = tempfile.mkdtemp()
    try:
        node = WindowsNode("laptop", screenshot_dir=tmp, screenshot_device_subfolders=False)
        png = b"\x89PNG\r\n\x1a\n" + b"not a real png body, just bytes to persist" * 4
        payload = ClipPayload(mime="image/png", data=png, origin="laptop")

        node._maybe_save_screenshot(payload)
        files = os.listdir(tmp)
        check("screenshot file created", len(files) == 1, str(files))
        check("filename matches Windows' own style",
              bool(files) and files[0].startswith("Screenshot ") and files[0].endswith(".png"),
              str(files))
        if files:
            with open(os.path.join(tmp, files[0]), "rb") as fh:
                check("saved bytes match the payload exactly, no re-encoding", fh.read() == png)

        # Same content saved again moments later must be deduped, not written as a second
        # file -- this is the fix for the duplicate-save bug found in live testing: Windows
        # firing the clipboard-changed notification twice for one snip, and the own-write
        # echo race in write(), both reach _maybe_save_screenshot() with identical bytes.
        node._maybe_save_screenshot(payload)
        check("re-saving identical content moments later is deduped, not written again",
              os.listdir(tmp) == files, str(os.listdir(tmp)))

        # A genuinely different image landing in the same second must still get its own
        # file -- dedup is content-based (by sha256), not "only ever save once per second".
        different = ClipPayload(mime="image/png", data=png + b"more", origin="laptop")
        node._maybe_save_screenshot(different)
        files2 = sorted(os.listdir(tmp))
        check("a different image in the same second still gets its own, distinctly-named file",
              len(files2) == 2 and any(" (2)" in f for f in files2), str(files2))

        # Dedup is TTL-bounded, not permanent: a deliberately re-taken identical screenshot
        # later must still be saved. Same pattern as test_dedup's "entries expire" case.
        short_ttl_node = WindowsNode("laptop", screenshot_dir=tmp, screenshot_device_subfolders=False)
        short_ttl_node._screenshot_dedup = RecentClips(ttl=0.3)
        third = ClipPayload(mime="image/png", data=png + b"unique-for-ttl-test", origin="laptop")
        short_ttl_node._maybe_save_screenshot(third)
        before_ttl = set(os.listdir(tmp))
        time.sleep(0.45)
        short_ttl_node._maybe_save_screenshot(third)
        check("identical content saves again once the dedup window has expired",
              len(os.listdir(tmp)) == len(before_ttl) + 1, str(os.listdir(tmp)))

        # Off by default: a falsy screenshot_dir must be a complete no-op.
        before = set(os.listdir(tmp))
        WindowsNode("laptop", screenshot_dir=None)._maybe_save_screenshot(payload)
        check("no screenshot_dir configured -> nothing written anywhere",
              set(os.listdir(tmp)) == before)

        # Never raises, even when the directory genuinely cannot be created (a file, not a
        # folder, sits where a parent directory component needs to be).
        blocker = os.path.join(tmp, "blocked_by_a_file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        bad_node = WindowsNode("laptop", screenshot_dir=os.path.join(blocker, "sub"))
        try:
            bad_node._maybe_save_screenshot(payload)
            check("unwritable target directory does not raise", True)
        except Exception as exc:
            check("unwritable target directory does not raise", False, repr(exc))

        # write()'s screenshot_include_devices gate -- stubbed to avoid touching the real
        # clipboard (the actual winclip.write_image() call is exercised separately by
        # test_winclip_concurrency and BUG-002's own coverage; garbage PNG bytes here would
        # only fail decode inside write(), which is irrelevant to what this checks).
        calls: list[str] = []
        included = WindowsNode("laptop", screenshot_dir=tmp, screenshot_include_devices=True)
        included._maybe_save_screenshot = lambda p: calls.append("included")
        included.write(payload)
        check("write() saves when screenshot_include_devices=True", calls == ["included"])

        excluded = WindowsNode("laptop", screenshot_dir=tmp, screenshot_include_devices=False)
        excluded._maybe_save_screenshot = lambda p: calls.append("excluded")
        excluded.write(payload)
        check("write() skips the save when screenshot_include_devices=False",
              calls == ["included"], str(calls))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, fn in [("payload", test_payload), ("dedup", test_dedup),
                     ("outbox", test_outbox),
                     ("outbox-no-reput", test_outbox_put_skips_unchanged_reput),
                     ("outbox-image-sidecar", test_outbox_image_sidecar),
                     ("outbox-sidecar-integrity", test_outbox_sidecar_integrity),
                     ("outbox-sensitive-zero-disk", test_outbox_sensitive_zero_disk),
                     ("policy", test_policy),
                     ("policy-images", test_policy_images),
                     ("domain", test_domain), ("hub-stamp", test_hub_stamp_attribution),
                     ("hub-locked-member", test_hub_routes_around_locked_domain_member),
                     ("hub-repeat-copy", test_repeat_copy_reaches_node_missed_by_first_delivery),
                     ("hub-no-double-queue", test_domain_failure_queues_once_not_twice),
                     ("hub-already-satisfied", test_domain_delivery_already_satisfied_is_not_requeued),
                     ("hub-restart-baseline", test_hub_restart_survives_baseline_drop),
                     ("hub-drain-lock", test_drain_outbox_serializes_with_route_lock),
                     ("single-instance", test_single_instance),
                     ("winclip", test_winclip_concurrency),
                     ("winclip-history-exclude", test_write_image_excludes_from_clipboard_history),
                     ("winclip-sensitive-exclude", test_write_text_sensitive_excludes_from_history_and_cloud),
                     ("screenshot-backup", test_screenshot_backup)]:
        print(f"{name}:")
        fn()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all core unit tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
