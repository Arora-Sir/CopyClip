r"""CopyClip dashboard: a real dark-themed status window, not just the tray menu.

tkinter is Python's standard library, not a third-party dependency (Tcl/Tk ships with the
standard CPython Windows installer) -- consistent with this project's zero-dependency rule.
ttk's "clam" theme is used because it is the one built-in theme that actually honors custom
colors; the native "vista"/"winnative" themes mostly ignore them and would silently render
light-on-light.

Runs on its own dedicated thread with its own Tk mainloop, entirely separate from tray.py's
Win32 message loop on the main thread -- the two never touch the same event loop, only the
same `hub` object, which is safe to read from any thread (see hub.status()'s existing use
from copyclip.py's own cmd_status()).
"""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import ttk

BG = "#101218"
SURFACE = "#171a21"
SURFACE_VARIANT = "#1f232c"
TEXT = "#e4e6ec"
TEXT_MUTED = "#9aa1b2"
ACCENT = "#7aa2ff"
CONNECTED = "#6fd8c7"
WAITING = "#e8b25a"
ERROR = "#e8697a"

_open_lock = threading.Lock()
_is_open = False


def open_dashboard(hub) -> None:
    global _is_open
    with _open_lock:
        if _is_open:
            return
        _is_open = True
    threading.Thread(target=_run, args=(hub,), name="copyclip-dashboard", daemon=True).start()


def _dark_titlebar(root: tk.Tk) -> None:
    """Native title bar is otherwise stuck light-mode white against a dark body. Windows 11
    (and later Windows 10 builds) expose exactly this via DWM -- no theming library needed,
    same raw-ctypes-over-pywin32 approach as winclip.py. Attribute id is 20 on current
    Windows; 19 on some older Windows 10 builds, so try both and ignore failure entirely on
    anything older still (the window is just not dark-titled there, nothing breaks).
    """
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwmapi = ctypes.windll.dwmapi
        value = ctypes.c_int(1)
        for attribute in (20, 19):
            if dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


def _run(hub) -> None:
    global _is_open
    try:
        root = tk.Tk()
        root.title("CopyClip")
        root.configure(bg=BG)
        root.geometry("420x480")
        root.minsize(360, 320)
        _dark_titlebar(root)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Segoe UI", 16, "bold"))
        style.configure("Muted.TLabel", background=SURFACE, foreground=TEXT_MUTED,
                        font=("Segoe UI", 9))
        style.configure("TButton", background=SURFACE_VARIANT, foreground=TEXT,
                        font=("Segoe UI", 10), borderwidth=0, focuscolor=BG)
        style.map("TButton", background=[("active", ACCENT)])

        ttk.Label(root, text="CopyClip", style="Title.TLabel").pack(
            anchor="w", padx=20, pady=(18, 4))
        ttk.Label(root, text="Devices connected right now", style="TLabel").pack(
            anchor="w", padx=20, pady=(0, 14))

        list_frame = ttk.Frame(root, style="TFrame")
        list_frame.pack(fill="both", expand=True, padx=16)

        pause_var = tk.StringVar(value="Pause sync")

        def toggle_pause() -> None:
            hub.paused = not hub.paused

        ttk.Button(root, textvariable=pause_var, command=toggle_pause).pack(
            fill="x", padx=16, pady=16)

        rows: dict[str, dict] = {}

        def build_row(name: str) -> dict:
            card = ttk.Frame(list_frame, style="Card.TFrame")
            card.pack(fill="x", pady=6)
            dot = tk.Canvas(card, width=14, height=14, bg=SURFACE, highlightthickness=0)
            dot.pack(side="left", padx=(14, 10), pady=14)
            oval = dot.create_oval(2, 2, 12, 12, fill=TEXT_MUTED, outline="")
            text_col = ttk.Frame(card, style="Card.TFrame")
            text_col.pack(side="left", fill="x", expand=True, pady=10)
            name_lbl = ttk.Label(text_col, text=name, style="Card.TLabel",
                                 font=("Segoe UI", 11, "bold"))
            name_lbl.pack(anchor="w")
            status_lbl = ttk.Label(text_col, text="", style="Muted.TLabel")
            status_lbl.pack(anchor="w")
            return {"dot": dot, "oval": oval, "status": status_lbl}

        def refresh() -> None:
            try:
                status = hub.status()
            except Exception:
                root.after(2000, refresh)
                return

            pause_var.set("Resume sync" if hub.paused else "Pause sync")

            for name, info in status["nodes"].items():
                if name == "laptop":
                    continue
                if name not in rows:
                    rows[name] = build_row(name)
                row = rows[name]
                if info["available"]:
                    color, text = CONNECTED, "Connected"
                else:
                    color, text = ERROR, "Unreachable"
                if info.get("locked"):
                    text += " (locked)"
                row["dot"].itemconfig(row["oval"], fill=color)
                row["status"].configure(text=text)

            for domain in status["domains"]:
                key = f"domain:{domain['domain']}"
                if key not in rows:
                    rows[key] = build_row(f"{domain['domain']} (Continuity)")
                row = rows[key]
                healthy = domain["healthy"]
                row["dot"].itemconfig(row["oval"], fill=CONNECTED if healthy else WAITING)
                row["status"].configure(
                    text=f"{'Healthy' if healthy else 'Degraded'} - representative: "
                         f"{domain['representative']}"
                )

            root.after(2000, refresh)

        refresh()

        def on_close() -> None:
            global _is_open
            with _open_lock:
                _is_open = False
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.mainloop()
    finally:
        with _open_lock:
            _is_open = False
