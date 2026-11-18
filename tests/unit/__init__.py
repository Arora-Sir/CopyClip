"""Unit tier: pure logic, no clipboard, no devices, no network beyond loopback."""

import logging

# Error paths (corrupt files, failed writes) are exercised on purpose; their log output
# is expected and would otherwise read as real failures.
logging.disable(logging.CRITICAL)
