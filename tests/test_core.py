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


def main() -> int:
    for name, fn in [("payload", test_payload), ("dedup", test_dedup),
                     ("outbox", test_outbox), ("policy", test_policy),
                     ("domain", test_domain)]:
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
