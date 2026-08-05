"""HMAC framing for the TCP tier.

Tailscale already gives WireGuard-grade confidentiality and integrity between nodes, so
this is not about encryption. It is about authorisation *within* the tailnet: without it,
any other device on the tailnet could inject clipboard content into this machine or read
what is being synced. The key turns "anyone on my tailnet" into "the one paired device".

Wire format, one frame per line:

    <seq> <hmac-sha256-hex> <payload>

The sequence number is inside the MAC and must strictly increase, so a captured frame
cannot be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


class AuthError(RuntimeError):
    """Frame failed authentication. Treat the peer as hostile and drop the connection."""


def new_nonce() -> str:
    return secrets.token_hex(16)


def _mac(key: bytes, seq: int, payload: str) -> str:
    msg = f"{seq}:{payload}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def seal(key: bytes, seq: int, payload: str) -> str:
    return f"{seq} {_mac(key, seq, payload)} {payload}"


def open_frame(key: bytes, raw: str, last_seq: int) -> tuple[int, str]:
    """Verify and unwrap a frame. Returns (seq, payload); raises AuthError otherwise."""
    parts = raw.split(" ", 2)
    if len(parts) < 3:
        # A payload can be empty, but seq and mac are mandatory.
        if len(parts) == 2:
            parts.append("")
        else:
            raise AuthError("malformed frame")
    seq_s, mac, payload = parts
    try:
        seq = int(seq_s)
    except ValueError as exc:
        raise AuthError("bad sequence number") from exc
    if seq <= last_seq:
        raise AuthError(f"replay or reorder: seq {seq} <= {last_seq}")
    if not hmac.compare_digest(mac, _mac(key, seq, payload)):
        raise AuthError("bad MAC")
    return seq, payload


def challenge_response(key: bytes, nonce: str) -> str:
    return hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_challenge(key: bytes, nonce: str, response: str) -> bool:
    return hmac.compare_digest(response, challenge_response(key, nonce))
