# BUG-021: The same multi-line text hashes differently with CRLF and LF line endings

## Status
OPEN. Characterised by a test, **not fixed, not measured on devices.** This is inferred from the code.

## Severity
Unknown until measured. If it happens in practice it breaks loop prevention for multi-line clips.

## Observation
Clip identity is the SHA-256 of the raw bytes. Windows usually stores line breaks as `\r\n`; Android as `\n`. `tests/unit/test_models_policy.py::test_crlf_and_lf_hash_differently_today` shows two clips that differ only in line endings are different clips to dedup. Nothing normalises newlines on either side, and `ClipBridge.java` has no `\r` handling.

## What would settle it
Copy the same multi-line text on the laptop and on a phone, then compare `fingerprint` values in the Activity screen or the `hub` debug log lines. If they differ and the clip bounces back, normalise only the **dedup key**, never the content that is pasted.

## Measurement 2026-11-18 (laptop to S24 direction only)
Wrote a three-line clip with CRLF endings to the laptop clipboard through `winclip.write_text`. The hub logged `laptop -> phone: 47 chars` once, nothing came back from the phone or the tablet within 50 seconds, and the laptop clipboard still held the original CRLF bytes. The same text with LF endings was then written and logged `laptop -> phone: 37 chars` once, again with no bounce. So the two forms are distinct clips to dedup (as the unit test pins), but no echo loop appeared in this direction.

Phone to laptop, measured the same day: the owner copied an 11-line text on the S24 (LF endings). The hub logged `phone -> laptop: 111 chars` once, the laptop clipboard held the text with its LF endings intact, and nothing bounced back in the following 50 seconds. A phone-side re-read changing the endings of a received clip was not seen either.

Conclusion: no echo loop in either direction with ordinary clips. The hashes still differ between CRLF and LF forms, so a clip whose line endings are changed by an app in between would be sent again once, which is harmless. Status: OPEN as low severity, no code change made. Reopen if a bounce is ever seen in real use.
