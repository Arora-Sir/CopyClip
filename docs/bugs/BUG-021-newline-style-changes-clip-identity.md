# BUG-021: The same multi-line text hashes differently with CRLF and LF line endings

## Status
OPEN. Characterised by a test, **not fixed, not measured on devices.** This is inferred from the code.

## Severity
Unknown until measured. If it happens in practice it breaks loop prevention for multi-line clips.

## Observation
Clip identity is the SHA-256 of the raw bytes. Windows usually stores line breaks as `\r\n`; Android as `\n`. `tests/unit/test_models_policy.py::test_crlf_and_lf_hash_differently_today` shows two clips that differ only in line endings are different clips to dedup. Nothing normalises newlines on either side, and `ClipBridge.java` has no `\r` handling.

## What would settle it
Copy the same multi-line text on the laptop and on a phone, then compare `fingerprint` values in the Activity screen or the `hub` debug log lines. If they differ and the clip bounces back, normalise only the **dedup key**, never the content that is pasted.
