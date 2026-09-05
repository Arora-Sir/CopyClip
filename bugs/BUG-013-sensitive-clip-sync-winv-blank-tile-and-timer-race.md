# BUG-013: Sensitive clip dropping, Win+V history blank card artifact, and timer supersession race

**Status**: CONFIRMED, fixed and verified live on hardware (2026-09-05)
**Severity**: High (silent sync drop on credentials, visual history pollution, potential clipboard wipe race)
**Affects**: `core/policy.py`, `core/outbox.py`, `nodes/windows_node.py`, `winclip.py`, `android/ClipBridge.java`, `windows_app/dashboard.py`

---

## 1. Symptoms

1. **Silent Drop of Authenticator Codes**: Copying a one-time passcode (OTP) in Google Authenticator (or credentials in Bitwarden, 1Password, or Samsung Pass) on the phone showed the code on the Android clipboard, but it was never transmitted to the laptop or Pixel. However, tapping the item a second time from Samsung's clipboard history panel synced it immediately.
2. **Win+V Clipboard History Blank Tile**: After the sensitive auto-wipe timer expired on Windows, pressing `Win+V` displayed an empty white rectangle card at the top of the history list.
3. **Timer Supersession Race Condition**: If a sensitive OTP was copied (arming a 10s auto-wipe countdown) and the user immediately copied normal text or an image before the 10 seconds elapsed, the pending timer was not cancelled; when the timer expired, it wiped the user's new content.
4. **Dashboard Inaccurate Queue Count**: The Windows dashboard permanently displayed "1 Outbox Queued" in amber warning text even when all three devices were online, active, and fully synchronized with an empty outbox.

---

## 2. Measured Evidence & Root Cause Analysis

### A. The Google Authenticator Drop Mechanism
On Android 13+ (API 33+), password managers and authenticator applications attach a boolean extra to the clip metadata:
```java
ClipDescription.EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE";
```
In `android/ClipBridge.java`:
```java
PersistableBundle extras = desc.getExtras();
boolean isSensitive = extras != null && extras.getBoolean("android.content.extra.IS_SENSITIVE", false);
```
In `core/policy.py`, the default policy dropped all sensitive clips unconditionally:
```python
if payload.sensitive and not self.allow_sensitive:
    return "flagged sensitive by the source app"
```
**Why copying from Samsung's clipboard tray succeeded:**
When a user opened Samsung Keyboard's clipboard tray or Samsung Edge panel and tapped a previously copied item, Samsung's system UI created a fresh `ClipData.newPlainText()` item without re-attaching the source app's `PersistableBundle` extras. Consequently, `isSensitive` was `false` on the second copy, bypassing the policy filter.

### B. The Win+V Blank History Card Mechanism
When `_wipe_sensitive()` executed on Windows, it previously invoked `winclip.write_text("")`.
In `winclip.py`:
```python
data = text.encode("utf-16-le") + b"\x00\x00"
_write_retrying([(CF_UNICODETEXT, data)], attempts)
```
Under Win32 user32 APIs, calling `EmptyClipboard()` followed by `SetClipboardData(CF_UNICODETEXT, ...)` registers a legitimate new clipboard format containing a null-terminated empty string. The Windows 10/11 Clipboard History service (`dwm.exe` / `TextInputHost.exe`) monitors format updates and adds a card for every `SetClipboardData` event, creating a visible blank rectangle.

By contrast, calling `user32.EmptyClipboard()` inside `OpenClipboard`/`CloseClipboard` without calling `SetClipboardData()` clears the clipboard completely without generating a Windows Clipboard History item.

### C. The Timer Supersession Race
In `nodes/windows_node.py`:
- `write()` checked `if payload.sensitive:` and cancelled `self._wipe_timer`.
- However, if an incoming non-sensitive clip (`payload.sensitive is False`) or image (`payload.is_image is True`) arrived, `self._wipe_timer` was untouched.
- Similarly, `_on_change()` did not cancel `self._wipe_timer` when the user copied something locally.
- On Android, `ClipBridge.java`'s `doSet()` (regular text), `doSetImage()` (images), and the poll loop (local user copies) did not cancel `pendingWipeFuture`.

### D. The Outbox Metric Discrepancy
In `core/hub.py`:
- `self.stats["queued"]` is a cumulative lifetime counter incremented on lines 310, 336, and 381 every time any item enters the outbox. It is never decremented when items are delivered.
- In `windows_app/dashboard.py`, `refresh()` called `stat_queue_val.set(str(stats.get("queued", 0)))`. If an item was queued once during startup baseline reconciliation, the dashboard showed `1` forever.
- The authoritative active backlog is `status.get("outbox", [])` (`self.outbox.keys()`). When drained, it is `{}`.

---

## 3. Fix Applied

### 1. Opt-In Sensitive Synchronization Policy (`core/policy.py`, `config.json`)
- Added `"allow_sensitive": false` (default) and `"sensitive_wipe_s": 10.0` in `config.json`.
- When `"allow_sensitive": true` is enabled, sensitive clips sync with end-to-end isolation.

### 2. Outbox Volatile RAM Guard (`core/outbox.py`)
- Sensitive payloads are routed to `self._sensitive_entries` (volatile RAM dictionary).
- Sensitive entries are strictly excluded when saving `outbox.json` to disk. If the machine loses power or CopyClip restarts, sensitive credentials leave zero trace on disk.

### 3. Windows Endpoint Hardening & Clean Wipe (`winclip.py`, `nodes/windows_node.py`)
- Added `winclip.clear_clipboard() -> int`:
  - Opens clipboard with exponential retry.
  - Calls `user32.EmptyClipboard()`.
  - Closes clipboard without calling `SetClipboardData()`.
  - Returns `user32.GetClipboardSequenceNumber()`.
- In `_wipe_sensitive()`:
  - Invokes `new_seq = winclip.clear_clipboard()`.
  - Immediately updates `self._own_write_seq = new_seq` to suppress echo loops.

### 4. Multi-Path Timer Supersession
- **Windows (`nodes/windows_node.py`)**:
  - `write()`: Cancels and clears `self._wipe_timer` on image writes, non-sensitive text writes, and sensitive writes.
  - `_on_change()`: Cancels and clears `self._wipe_timer` whenever `winclip.sequence_number() != self._own_write_seq` (local user copy).
- **Android (`android/ClipBridge.java`)**:
  - Added `cancelSensitiveWipe()` helper.
  - Invoked from `doSet()`, `doSetImage()`, `scheduleSensitiveWipe()`, and poll loop on user local copies.

### 5. Wire Protocol Extension (`CONF` and `SETS`)
- Laptop sends `CONF <base64>` carrying `{"allow_sensitive": bool, "sensitive_wipe_s": int}` on connection and configuration hot-reload.
- Sensitive clips are sent to Android via `SETS <base64>` instead of `SET <base64>`. Android replies `ACK set` and arms its own on-device wipe executor.

### 6. Dashboard Live Backlog Display (`windows_app/dashboard.py`)
- Replaced cumulative counter with `active_queued = len(status.get("outbox", []))`.
- Updated `stat_queue_label` foreground color: `WARNING` (amber) when `active_queued > 0`, and `TEXT_MUTED` when `active_queued == 0`.

---

## 4. Verification

1. **Automated Test Suite**:
   - `tests/test_sensitive_sync.py`: 8 suites, 43 assertions passed. Validates `clear_clipboard()`, format exclusions, RAM-only outbox, policy gating, Android `CONF` handshake, timer supersession on non-sensitive writes, and local copy cancellation.
   - `tests/test_android_node.py`, `tests/test_imaging.py`, `tests/test_core.py`: All passed (100%).
2. **Live Mesh Hardware Verification**:
   - `copyclip.py --status`: `phone`, `tab`, and `pixel` connected over Tailscale ADB.
   - `bin/clipbridge.dex` rebuilt and deployed.
