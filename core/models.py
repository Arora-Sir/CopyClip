"""Value objects shared across CopyClip.

The canonical payload form is **bytes plus a MIME type**, not `str`. Text is simply
`text/plain` holding UTF-8. That is deliberate: it is what lets image and file support
arrive later as new MIME types handled at the node boundary, without the hub, dedup,
outbox or transport layers changing at all.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field

TEXT_MIMES = ("text/plain", "text/html")


@dataclass(frozen=True)
class ClipPayload:
    """One clipboard item, as it travels between nodes."""

    mime: str
    data: bytes
    origin: str                 # node name this clip was first observed on
    ts: float = field(default_factory=time.time)
    sensitive: bool = False

    # -- identity ----------------------------------------------------------

    @property
    def sha256(self) -> str:
        """Content identity: SHA-256 of the raw bytes.

        Identity is the content hash and never the clip label. Samsung rewrites labels
        when Continuity relays a clip (observed: our own write reappeared on the tablet
        tagged `mcf_continuity;device_name=S24_Ultra`), so a label-based identity would
        treat one clip as two and loop forever.

        Hashes the bytes alone, deliberately excluding the MIME type, so that ClipBridge
        can compute the identical value on-device from the clipboard text without having
        to agree on a composite hashing scheme.
        """
        return hashlib.sha256(self.data).hexdigest()

    @property
    def short(self) -> str:
        return self.sha256[:12]

    # -- text convenience --------------------------------------------------

    @property
    def is_text(self) -> bool:
        return self.mime in TEXT_MIMES

    @property
    def text(self) -> str | None:
        if not self.is_text:
            return None
        return self.data.decode("utf-8", "replace")

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def char_len(self) -> int:
        """Length in characters for text, else byte length. Used for size policy."""
        t = self.text
        return len(t) if t is not None else len(self.data)

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_text(cls, text: str, origin: str, *, sensitive: bool = False,
                  mime: str = "text/plain") -> "ClipPayload":
        return cls(mime=mime, data=text.encode("utf-8"), origin=origin, sensitive=sensitive)

    @classmethod
    def from_b64(cls, b64: str, *, mime: str, origin: str,
                 sensitive: bool = False) -> "ClipPayload":
        return cls(mime=mime, data=base64.b64decode(b64), origin=origin, sensitive=sensitive)

    def to_b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    # -- serialisation for the durable outbox ------------------------------

    def to_dict(self) -> dict:
        return {
            "mime": self.mime,
            "data_b64": self.to_b64(),
            "origin": self.origin,
            "ts": self.ts,
            "sensitive": self.sensitive,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ClipPayload":
        return cls(
            mime=d["mime"],
            data=base64.b64decode(d["data_b64"]),
            origin=d["origin"],
            ts=d.get("ts", time.time()),
            sensitive=d.get("sensitive", False),
        )

    def __repr__(self) -> str:
        return (f"ClipPayload({self.mime}, {self.size}B, from={self.origin}, "
                f"sha={self.short})")


@dataclass(frozen=True)
class ClipDescriptor:
    """Metadata about a clip, without the body.

    Devices announce changes with this instead of shipping the payload. It is what keeps
    Samsung's mirroring from doubling our traffic: when the tablet echoes a clip we
    already routed, we recognise the hash and never pull the body. The body is fetched
    only for clips that are genuinely new, and only from the device that reported first.
    """

    node: str
    mime: str
    sha256: str
    ts: int = 0
    sensitive: bool = False
    char_len: int = 0

    @property
    def short(self) -> str:
        return self.sha256[:12]


class WriteResult:
    """Outcome of writing a payload to a node."""

    OK = "ok"                 # applied
    DEFERRED = "deferred"     # target reachable but locked; it will replay on unlock
    UNAVAILABLE = "unavail"   # target not connected; caller should use the outbox
    FAILED = "failed"         # real error
    REJECTED = "rejected"     # policy said no
