"""Whether a clip is allowed to leave this machine at all.

Kept separate from the hub so the rules can be read, tested and changed in one place
without touching routing. Every rejection returns a human-readable reason, because a clip
that silently fails to sync is far more confusing than one that logs why.
"""

from __future__ import annotations

import re

from .models import ClipPayload, TEXT_MIMES


class Policy:
    def __init__(self, *, max_chars: int = 800_000,
                 denylist_regex: list[str] | None = None,
                 allow_non_text: bool = False,
                 allow_sensitive: bool = False,
                 allow_images: bool = False,
                 max_image_bytes: int = 62_914_560) -> None:
        self.max_chars = max_chars
        self.denylist = [re.compile(p) for p in (denylist_regex or [])]
        self.allow_non_text = allow_non_text
        self.allow_sensitive = allow_sensitive
        self.allow_images = allow_images
        self.max_image_bytes = max_image_bytes

    def set_denylist(self, patterns: list[str]) -> None:
        """Replace the denylist atomically; an invalid pattern leaves the old list in place."""
        self.denylist = [re.compile(p) for p in (patterns or [])]

    def reject_reason(self, payload: ClipPayload) -> str | None:
        """None means allowed."""
        if payload.size == 0:
            return "empty"

        if payload.sensitive and not self.allow_sensitive:
            # Android's EXTRA_IS_SENSITIVE, set by password managers. Never transmit.
            return "flagged sensitive by the source app"

        if payload.is_image:
            # Independent gate from allow_non_text: images are governed by their own flag
            # and their own (much larger) size cap, not text's max_chars. The denylist
            # below is text-only -- it can never match image bytes -- so nothing past this
            # point applies to an image.
            if not self.allow_images:
                return "images disabled (allow_images is false)"
            if payload.size > self.max_image_bytes:
                # Skipped whole, never truncated -- same reasoning as oversize text below.
                return f"image too large ({payload.size} > {self.max_image_bytes})"
            return None

        if not self.allow_non_text and payload.mime not in TEXT_MIMES:
            return f"non-text ({payload.mime}); v1 syncs text only"

        if payload.char_len > self.max_chars:
            # Skipped whole, never truncated: half a clip pasted into a shell or an editor
            # is worse than no clip, because it looks like it worked.
            return f"too large ({payload.char_len} > {self.max_chars})"

        text = payload.text
        if text is not None:
            for pat in self.denylist:
                if pat.search(text):
                    return f"denylist:{pat.pattern}"

        return None
