"""Tier 1 transport: ClipBridge over a persistent `adb shell` pipe across Tailscale.

Works with zero phone-side setup, which is why it is the default. In principle, wireless ADB
does not survive a phone reboot (`service.adb.tcp.port` is set but `persist.adb.tcp.port` is
not, and making that persistent needs root, which is off limits) -- but in practice, once
Android's own Wireless Debugging has been paired once with a remembered network (which
Shizuku setup for Tier 2 already requires), that pairing itself survives reboot and reopens
the wireless adb listener automatically, and this tier benefits from that for free: confirmed
2026-08-31 by a real phone reboot, `service.adb.tcp.port` back at 5555 within 5 minutes, no
cable touched. Only a device that has never paired Wireless Debugging/Shizuku with a network
(a freshly added device, e.g. a new Pixel before its first pairing) actually needs the manual
`adb -d tcpip 5555` re-arm documented in README.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from . import Transport, TransportError

log = logging.getLogger("copyclip.adb")

REMOTE_DEX = "/data/local/tmp/clipbridge.dex"
MAIN_CLASS = "me.mohit.copyclip.ClipBridge"

# app_process needs a nominal "command directory" argument before the class name.
SERVE_CMD = f"CLASSPATH={REMOTE_DEX} app_process / {MAIN_CLASS} serve"

# Never inherit a console window on Windows; the daemon runs headless.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AdbTransport(Transport):
    name = "adb"

    def __init__(self, device: str, dex_path: str, adb: str = "adb",
                 connect_timeout: float = 15.0) -> None:
        super().__init__()
        self.device = device
        self.dex_path = dex_path
        self.adb = adb
        self.connect_timeout = connect_timeout
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _adb(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.device, *args],
            capture_output=True, text=True, timeout=timeout or self.connect_timeout,
            creationflags=_NO_WINDOW,
        )

    def _ensure_connected(self) -> None:
        # `adb connect` is idempotent and cheap; it re-establishes after a Tailscale blip.
        res = subprocess.run(
            [self.adb, "connect", self.device],
            capture_output=True, text=True, timeout=self.connect_timeout,
            creationflags=_NO_WINDOW,
        )
        blob = (res.stdout + res.stderr).lower()
        if "connected" not in blob:
            raise TransportError(
                f"adb connect {self.device} failed: {(res.stdout + res.stderr).strip()!r}. "
                "If the phone rebooted, wireless ADB needs re-arming (see README)."
            )

        state = self._adb("get-state")
        if state.stdout.strip() != "device":
            raise TransportError(
                f"device {self.device} not ready: {state.stdout.strip() or state.stderr.strip()!r}"
            )

    def _push_dex(self) -> None:
        if not os.path.isfile(self.dex_path):
            raise TransportError(
                f"{self.dex_path} missing. Run android\\build_dex.ps1 first."
            )
        local_size = os.path.getsize(self.dex_path)
        try:
            stat_res = self._adb("shell", f"stat -c %s {REMOTE_DEX}", timeout=5)
            remote_size_str = stat_res.stdout.strip()
            if stat_res.returncode == 0 and remote_size_str.isdigit() and int(remote_size_str) == local_size:
                log.debug("[%s] remote DEX up to date (%d bytes); skipping push", self.device, local_size)
                return
        except Exception:
            pass

        res = self._adb("push", self.dex_path, REMOTE_DEX, timeout=60)
        if res.returncode != 0:
            raise TransportError(f"adb push failed: {(res.stdout + res.stderr).strip()!r}")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._ensure_connected()
        self._push_dex()

        # -T disables PTY allocation. Without it adb rewrites \n as \r\n on the way out,
        # silently corrupting every multi-line clip.
        self._proc = subprocess.Popen(
            [self.adb, "-s", self.device, "shell", "-T", SERVE_CMD],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, creationflags=_NO_WINDOW,
        )

        self._reader = threading.Thread(target=self._read_loop, name="copyclip-adb-reader",
                                        daemon=True)
        self._reader.start()

        # ClipBridge greets with "READY <sdk>" once it has the binder handle. Anything
        # else means app_process never got off the ground.
        try:
            first = self.inbox.get(timeout=self.connect_timeout)
        except Exception as exc:
            self.close()
            raise TransportError(f"no READY from ClipBridge: {exc}") from exc
        if not first.startswith("READY"):
            self.close()
            raise TransportError(f"unexpected greeting from ClipBridge: {first!r}")
        log.info("ClipBridge up on %s (%s)", self.device, first)

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    self.inbox.put(line)
        except Exception as exc:  # pipe torn down under us
            log.debug("adb reader ended: %s", exc)
        finally:
            self._mark_dead()
            # Unblock anyone waiting on inbox.get() so the supervisor notices promptly.
            self.inbox.put("ERR transport-closed")

    def send_line(self, line: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or not self.alive():
            raise TransportError("adb channel is not open")
        data = (line + "\n").encode("utf-8")
        try:
            with self._write_lock:
                proc.stdin.write(data)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._mark_dead()
            raise TransportError(f"adb write failed: {exc}") from exc

    def close(self) -> None:
        self._mark_dead()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                try:
                    proc.stdin.write(b"QUIT\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception as exc:
            log.debug("adb close: %s", exc)
