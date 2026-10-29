# BUG-016: Orphaned ClipBridge Process Accumulation on Network Reconnect

## Status
CONFIRMED, fixed and verified live on hardware (2026-10-29).

## Severity
Medium (resource competition and duplicate event polling).

## Symptom
After Wi-Fi or Tailscale network disconnects, running `ps -ef | grep app_process` on the Android device revealed multiple instances of `ClipBridge serve`. Old background instances remained running from hours earlier.

## Root Cause
When an ADB network connection is dropped abruptly, `adbd` does not always deliver `EOF` or `SIGHUP` immediately to child processes on stdin.
When `AdbTransport.start()` in `transports/adb_transport.py` reconnected, it pushed the DEX and launched `app_process` without terminating any existing `ClipBridge` process. The old process continued its polling loop alongside the new instance.

## Fix
In `transports/adb_transport.py`, added a pre-connection cleanup step:
```python
def start(self) -> None:
    self._ensure_connected()
    try:
        self._adb("shell", "pkill -f me.mohit.copyclip.ClipBridge 2>/dev/null || true", timeout=5)
    except Exception:
        pass
    self._push_dex()
```
Executing `pkill` before starting `SERVE_CMD` guarantees that exactly one `ClipBridge` process runs per connected device.

## Verification
Checked `ps -ef | grep app_process` on Tab S10 Ultra and S24 Ultra after restarts. Confirmed that only a single `ClipBridge` process exists on each device.
