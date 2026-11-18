"""In-memory record of recent clips: what moved, where it went, and why anything was skipped.

Memory only, never written to disk or to the log. Text previews are short and optional, and
sensitive clips and images never carry a preview, so this is safe to show in the UI.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field

PREVIEW_CHARS = 140
#: Longest text kept in memory so it can be copied again from Activity. Longer clips show no copy button.
KEEP_TEXT_CHARS = 20_000


@dataclass
class ActivityEvent:
    id: int
    ts: float
    kind: str                      # text | image | camera
    size: int                      # characters for text, bytes otherwise
    origin: str
    status: str                    # delivered | queued | skipped | paused | partial
    targets: list = field(default_factory=list)   # [{"node": str, "status": str}]
    reason: str = ""
    latency_ms: int | None = None
    fingerprint: str = ""
    sensitive: bool = False
    preview: str | None = None
    file: str | None = None        # saved image file on this PC, never sent to the window
    text: str | None = None        # full text for Copy, memory only, never sent to the window

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_image"] = bool(d.pop("file"))
        d["has_text"] = bool(d.pop("text"))
        return d


def overall_status(targets: list[dict], override: str | None = None) -> str:
    if override:
        return override
    if not targets:
        return "skipped"
    states = {t["status"] for t in targets}
    # One device that took the clip is a delivery. A paused, replaced or still-waiting extra target
    # is shown in the row detail, it must not turn a successful sync into "Partial".
    if states & {"delivered", "deferred", "mirrored"}:
        return "partial" if "rejected" in states else "delivered"
    if states <= {"paused", "superseded"}:
        return "paused"
    if "queued" in states:
        return "queued"
    if "rejected" in states:
        return "skipped"
    return "partial"


class ActivityLog:
    def __init__(self, capacity: int = 500, previews: bool = True) -> None:
        self.capacity = capacity
        self.previews = previews
        self._events: deque[ActivityEvent] = deque(maxlen=capacity)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def record(self, *, kind: str, size: int, origin: str, targets: list[dict] | None = None,
               status: str | None = None, reason: str = "", latency_ms: int | None = None,
               fingerprint: str = "", sensitive: bool = False,
               text: str | None = None) -> ActivityEvent:
        targets = list(targets or [])
        preview = None
        if text is not None and self.previews and not sensitive and kind == "text":
            preview = text[:PREVIEW_CHARS]
        keep = text if (text is not None and self.previews and not sensitive and kind == "text"
                        and len(text) <= KEEP_TEXT_CHARS) else None
        event = ActivityEvent(
            id=next(self._ids), ts=time.time(), kind=kind, size=size, origin=origin,
            status=overall_status(targets, status), targets=targets, reason=reason,
            latency_ms=latency_ms, fingerprint="" if sensitive else fingerprint,
            sensitive=sensitive, preview=preview, text=keep)
        queued_nodes = {t["node"] for t in targets if t.get("status") == "queued"}
        with self._lock:
            # The outbox keeps one slot per device, so a newer queued clip replaces the older one.
            # Older rows must stop claiming they are still waiting.
            for old in self._events:
                changed = False
                for t in old.targets:
                    if t.get("status") == "queued" and t["node"] in queued_nodes:
                        t["status"] = "superseded"; changed = True
                if changed:
                    old.status = overall_status(old.targets)
            self._events.append(event)
        return event

    def list(self, *, limit: int = 100, after: int = 0, kind: str | None = None,
             status: str | None = None, query: str | None = None,
             sensitive: bool | None = None) -> list[dict]:
        """Newest first. `after` returns only events with a larger id (for polling)."""
        with self._lock:
            events = list(self._events)
        out: list[dict] = []
        q = (query or "").lower()
        for e in reversed(events):
            if e.id <= after:
                break
            if kind and e.kind != kind:
                continue
            if status and e.status != status:
                continue
            if sensitive is not None and e.sensitive != sensitive:
                continue
            if q and q not in f"{e.preview or ''} {e.origin} {e.reason} {e.kind}".lower() \
                    and not any(q in t["node"].lower() for t in e.targets):
                continue
            out.append(e.to_dict())
            if len(out) >= limit:
                break
        return out

    def annotate_last_image(self, origin: str, note: str) -> None:
        """Add a short note to the newest picture from `origin`, if it is not there already."""
        with self._lock:
            for e in reversed(self._events):
                if e.kind == "image" and e.origin == origin:
                    if note not in e.reason:
                        e.reason = f"{e.reason}. {note}" if e.reason else note
                    return

    def get_text(self, event_id: int) -> str | None:
        with self._lock:
            for e in self._events:
                if e.id == event_id:
                    return e.text
        return None

    def get_file(self, event_id: int) -> str | None:
        """Path of the saved picture behind an image event, or None."""
        with self._lock:
            for e in self._events:
                if e.id == event_id:
                    return e.file
        return None

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def set_previews(self, enabled: bool) -> None:
        self.previews = bool(enabled)
        if not enabled:
            with self._lock:
                for e in self._events:
                    e.preview = None
                    e.text = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
