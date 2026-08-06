"""Fast unit tests for the routing logic. No devices, no network, runs in well under a second.

The three-device rules (dedup across a Samsung sync domain, representative election,
Continuity health, outbox durability) are the parts most likely to break subtly, and they
are exactly the parts that are painful to exercise against real hardware. They are pure
logic by design, so they get tested as pure logic.

    python tests\\test_core.py
"""

from __future__ import annotations

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
    check("sensitive clips never transmitted",
          "sensitive" in pol.reject_reason(
              ClipPayload.from_text("hunter2", "x", sensitive=True)))
    check("non-text rejected while v1 is text-only",
          "non-text" in pol.reject_reason(
              ClipPayload(mime="image/png", data=b"\x89PNG", origin="x")))


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

        first = SingleInstance(pidfile)
        first.acquire()
        check("first instance acquires the singleton", True)
        check("pid file written", os.path.isfile(pidfile))

        second = SingleInstance(pidfile)
        try:
            second.acquire()
            second.release()
            check("second instance is refused", False, "it was allowed to start")
        except AlreadyRunning as exc:
            check("second instance is refused", True, f"error names PID {exc.pid}")

        first.release()

        third = SingleInstance(pidfile)
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
    ok = final in written
    check("clipboard left in a consistent state", ok,
          "" if ok else f"final value is not any of the writes: {str(final)[:40]!r}")

    if original is not None:
        try:
            winclip.write_text(original)
        except Exception:
            pass


def main() -> int:
    for name, fn in [("payload", test_payload), ("dedup", test_dedup),
                     ("outbox", test_outbox),
                     ("outbox-no-reput", test_outbox_put_skips_unchanged_reput),
                     ("policy", test_policy),
                     ("domain", test_domain), ("hub-stamp", test_hub_stamp_attribution),
                     ("hub-locked-member", test_hub_routes_around_locked_domain_member),
                     ("hub-repeat-copy", test_repeat_copy_reaches_node_missed_by_first_delivery),
                     ("hub-no-double-queue", test_domain_failure_queues_once_not_twice),
                     ("hub-already-satisfied", test_domain_delivery_already_satisfied_is_not_requeued),
                     ("hub-drain-lock", test_drain_outbox_serializes_with_route_lock),
                     ("single-instance", test_single_instance),
                     ("winclip", test_winclip_concurrency)]:
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
