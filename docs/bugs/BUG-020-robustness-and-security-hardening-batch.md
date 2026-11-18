# BUG-020: Robustness and security gaps found by the 2026-11-12 engineering audit and fuzzing

## Status
FIXED IN CODE with a regression test each (2026-11-12). **OPEN until confirmed in real use.** None of these were observed failing in production; each was read from the code and then pinned by a test before the fix.

## Severity
Medium to High (several are latent data-loss or exposure paths).

| # | Gap | Fix | Test |
| :-- | :--- | :--- | :--- |
| 1 | `Hub.reload_config` defaulted missing keys (`allow_images` True, 24 MB image cap) differently from startup (False, 60 MB), so a partial dict silently flipped settings | Only keys present are applied. Defaults live in `core/config.py` | `test_hub.py::test_empty_reload_keeps_startup_defaults` |
| 2 | Draining the outbox cleared the entry of a **paused** device, silently losing that clip | Drain skips paused devices and keeps the entry | `test_hub.py::test_paused_device_keeps_its_outbox_entry` |
| 3 | `_chunk_ack_event` was not cleared at the start of a chunked write, so a stale ACK from an earlier transfer could satisfy the first window | Cleared with `_ack_event` | `test_hardening.py::ChunkAckResetTests` |
| 4 | `CLIPB` accepted `nchunks=0` or negative and buffered chunks without bound | Rejected before buffering, and aborted if more data arrives than declared | `test_hardening.py::InboundChunkTests` |
| 5 | `TcpTransport` cleared the socket timeout before the handshake, so a silent peer hung `start()` forever; a missing `READY` leaked the socket | Timeout bounded through the handshake, socket closed on every failure | `test_network.py::test_silent_peer_does_not_hang_start` |
| 6 | Push listener read an unbounded line before authenticating, compared AUTH with `!=`, and let a peer claim origin `laptop` | Line caps, `hmac.compare_digest`, reserved names rejected | `test_network.py` (overlong line, reserved name) |
| 7 | `--replace` killed whatever PID was in the pid file, even if reused by an unrelated process | The PID's executable is checked first | `test_hardening.py::SingleInstanceTests` |
| 8 | `copyclip.py --status` started a second hub, which restarts each device's ClipBridge underneath the live daemon | Asks the running daemon's control API instead | `test_status_tray.py::StatusFromDaemonTests` |
| 9 | Rotating the key wrote the file but the running daemon, its devices and the push listener kept the old key | `Hub.apply_key` resends CONF; the listener key is swapped live | `test_service.py::test_rotate_writes_key_updates_nodes_and_listener` |
| 10 | `load_config` did a shallow update with no validation, so a typo surfaced later as a `KeyError` | `core/config.py` validates every key and reports all problems at once | `test_config_module.py` |
| 11 | `ClipPayload.sha256` rehashed the full bytes on every access (a 60 MB image several times per route) | Computed once | `test_hardening.py::HashCacheTests` |
| 12 | The dashboard rewrote the whole config from a snapshot taken when the window opened | Saves merge into the file as it is now, validated, atomic | `test_service.py::test_external_edit_between_reads_is_not_clobbered` |
| 13 | A `CLIPB` line with more than seven fields raised `ValueError` inside the device read loop (found by fuzzing) | Extra fields ignored | `test_aggressive.py::test_android_node_handle_survives_garbage_lines` |
| 14 | A non-ASCII MAC in a frame or handshake raised `TypeError` instead of a clean `AuthError` (found by fuzzing) | Compared as bytes | `test_aggressive.py::test_framing_open_frame_never_raises_anything_but_autherror` |
| 15 | A JSON list as a request body made the control API return 500 (found by fuzzing) | Non-object bodies get a 400 | `test_aggressive.py::test_random_bodies_and_routes_never_500` |

## Open, found but not changed
- `BaselineStore` and the outbox still fsync inside the routing lock.
- The HMAC handshake is still implemented three times (`tcp_transport`, `push_listener`, `android_node`).
- `winimage` still computes the row flip twice for DIB and DIBV5.
