r"""CopyClip modern dashboard: Fluent 2 dark status window & visual configuration manager.

Zero third-party dependencies: uses standard library tkinter / ttk / ctypes with
Windows 11 DWM dark title bar and rounded corners.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "logs", "copyclip.log")
KEY_PATH = os.path.join(HERE, "copyclip.key")

log = logging.getLogger("copyclip.dashboard")

#: Fluent 2 Dark Design System Palette ---------------------------------------
BG = "#0b0d13"                 # Deepest Obsidian background
SURFACE = "#141721"            # Elevated card surface
SURFACE_CARD = "#1a1e2b"       # Inner tile / row surface
SURFACE_BORDER = "#242938"     # Subtle card border outline
SURFACE_HOVER = "#22283a"      # Interactive hover state
TEXT = "#f8f9fc"               # Primary text
TEXT_MUTED = "#94a3b8"         # Secondary / subtitle text
TEXT_DIM = "#64748b"           # Dim / tertiary text

ACCENT = "#2563eb"             # Fluent Electric Blue Primary
ACCENT_HOVER = "#3b82f6"       # Fluent Accent Hover
ACCENT_MUTED = "#1e3a8a"       # Subtle accent background

SUCCESS = "#10b981"            # Emerald green
SUCCESS_BG = "#064e3b"         # Dark emerald pill
SUCCESS_TEXT = "#34d399"       # Crisp emerald text

WARNING = "#f59e0b"            # Amber
WARNING_BG = "#451a03"
WARNING_TEXT = "#fbbf24"

ERROR = "#ef4444"              # Coral red
ERROR_BG = "#450a0a"
ERROR_TEXT = "#f87171"

PAUSED = "#64748b"             # Slate grey
PAUSED_BG = "#1e293b"
PAUSED_TEXT = "#94a3b8"

FONT_FAMILY = "Segoe UI"
FONT_EMOJI = "Segoe UI Emoji"
FONT_MONO = "Cascadia Code" if os.name == "nt" else "Consolas"

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copyclip.ico")

_open_lock = threading.Lock()
_is_open = False


def open_dashboard(hub, cfg: dict | None = None, initial_tab: int = 0) -> None:
    global _is_open
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("me.mohit.copyclip")
    except Exception:
        pass
    with _open_lock:
        if _is_open:
            return
        _is_open = True
    threading.Thread(
        target=_run, args=(hub, cfg, initial_tab), name="copyclip-dashboard", daemon=True
    ).start()


def _apply_win11_attributes(root: tk.Tk | tk.Toplevel) -> None:
    """Enables Windows 11 dark title bar, smooth rounded corners, and consistent icon."""
    try:
        # Decouple process from generic python.exe/tk taskbar group to prevent feather icon
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("me.mohit.copyclip")
    except Exception:
        pass

    if os.path.isfile(ICON_PATH):
        try:
            root.iconbitmap(default=ICON_PATH)
        except Exception:
            pass
        try:
            root.iconbitmap(ICON_PATH)
        except Exception:
            pass
    try:
        root.update_idletasks()
        hwnds_to_set = []
        try:
            frame_id = int(root.wm_frame(), 16)
            if frame_id:
                hwnds_to_set.append(frame_id)
        except Exception:
            pass
        try:
            parent_id = ctypes.windll.user32.GetParent(root.winfo_id())
            if parent_id and parent_id not in hwnds_to_set:
                hwnds_to_set.append(parent_id)
        except Exception:
            pass
        if root.winfo_id() not in hwnds_to_set:
            hwnds_to_set.append(root.winfo_id())

        hwnd = hwnds_to_set[0] if hwnds_to_set else root.winfo_id()

        # Force native taskbar and titlebar icons via Win32 WM_SETICON
        if os.path.isfile(ICON_PATH):
            try:
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x0010
                hicon_big = ctypes.windll.user32.LoadImageW(
                    None, ICON_PATH, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
                )
                hicon_small = ctypes.windll.user32.LoadImageW(
                    None, ICON_PATH, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
                )
                for h in hwnds_to_set:
                    if hicon_big:
                        ctypes.windll.user32.SendMessageW(h, 0x0080, 1, hicon_big)   # WM_SETICON, ICON_BIG
                    if hicon_small:
                        ctypes.windll.user32.SendMessageW(h, 0x0080, 0, hicon_small) # WM_SETICON, ICON_SMALL
            except Exception:
                pass

        dwmapi = ctypes.windll.dwmapi
        # DWMWA_USE_IMMERSIVE_DARK_MODE (20 on Win11/Win10 20H1+, 19 on older)
        for attr in (20, 19):
            val = ctypes.c_int(1)
            if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        # DWMWA_WINDOW_CORNER_PREFERENCE (33): DWMWCP_ROUND = 2
        corner_pref = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner_pref), ctypes.sizeof(corner_pref))
    except Exception:
        pass


def _bind_mousewheel(widget, canvas: tk.Canvas) -> None:
    """Recursively binds MouseWheel scrolling to a target canvas on widget and all its children."""
    def _on_wheel(event):
        try:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    widget.bind("<MouseWheel>", _on_wheel, add="+")
    for child in widget.winfo_children():
        _bind_mousewheel(child, canvas)


def _load_config_file() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_config_file(data: dict) -> None:
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    if os.path.isfile(CONFIG_PATH):
        os.replace(tmp_path, CONFIG_PATH)
    else:
        os.rename(tmp_path, CONFIG_PATH)


def _get_device_meta(name: str) -> tuple[str, str, str]:
    """Returns (badge_text, friendly_display_name, badge_color)."""
    n = name.lower()
    if "phone" in n or "s24" in n:
        return "S24", "Samsung Galaxy S24 Ultra", "#3b82f6"
    elif "tab" in n or "s10" in n:
        return "TAB", "Samsung Galaxy Tab S10 Ultra", "#8b5cf6"
    elif "pixel" in n:
        return "PIXEL", "Google Pixel 1", "#10b981"
    elif "laptop" in n or "pc" in n:
        return "HOST", "Acer Predator Helios 300 (Host)", "#06b6d4"
    return name[:4].upper(), name.capitalize(), "#64748b"


def _add_hover_effect(btn: tk.Widget, normal_bg: str, hover_bg: str, normal_fg: str = TEXT, hover_fg: str = TEXT) -> None:
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover_bg, fg=hover_fg))
    btn.bind("<Leave>", lambda e: btn.configure(bg=normal_bg, fg=normal_fg))


def _run(hub, cfg_initial: dict | None = None, initial_tab: int = 0) -> None:
    global _is_open
    try:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("me.mohit.copyclip")
        except Exception:
            pass
        root = tk.Tk()
        root.title("CopyClip")
        root.configure(bg=BG)
        root.geometry("680x780")
        root.minsize(580, 640)
        _apply_win11_attributes(root)

        #: Global Tk Styles --------------------------------------------------
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TScrollbar", background=SURFACE_CARD, troughcolor=BG,
                        borderwidth=0, arrowsize=11)
        style.map("TScrollbar", background=[("active", SURFACE_HOVER)])
        style.configure("TCombobox", fieldbackground=SURFACE_CARD, background=SURFACE_CARD,
                        foreground=TEXT, darkcolor=SURFACE_BORDER, lightcolor=SURFACE_BORDER)

        # ======================================================================
        # HEADER BAR
        # ======================================================================
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 14))

        header_left = tk.Frame(header, bg=BG)
        header_left.pack(side="left")

        title_row = tk.Frame(header_left, bg=BG)
        title_row.pack(anchor="w")

        tk.Label(title_row, text="CopyClip", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 16, "bold")).pack(side="left")
        ver_lbl = tk.Label(title_row, text=" v2.0", bg=BG, fg=ACCENT_HOVER,
                           font=(FONT_FAMILY, 10, "bold"))
        ver_lbl.pack(side="left", padx=(4, 0), pady=(4, 0))

        subtitle_lbl = tk.Label(header_left, text="Cross-Device Universal Clipboard Mesh",
                                bg=BG, fg=TEXT_MUTED, font=(FONT_FAMILY, 9))
        subtitle_lbl.pack(anchor="w", pady=(2, 0))

        # Global Mesh Status Badge (Top Right)
        status_badge_frame = tk.Frame(header, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                                      highlightthickness=1, padx=12, pady=6)
        status_badge_frame.pack(side="right", pady=(2, 0))

        status_dot = tk.Canvas(status_badge_frame, width=10, height=10, bg=SURFACE, highlightthickness=0)
        status_dot.pack(side="left", padx=(0, 6))
        status_dot_oval = status_dot.create_oval(1, 1, 9, 9, fill=SUCCESS, outline="")

        global_status_text = tk.StringVar(value="Mesh Active")
        status_badge_lbl = tk.Label(status_badge_frame, textvariable=global_status_text,
                                    bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9, "bold"))
        status_badge_lbl.pack(side="left")

        # ======================================================================
        # CUSTOM SEGMENTED PILL TAB SWITCHER
        # ======================================================================
        tab_container = tk.Frame(root, bg=BG)
        tab_container.pack(fill="x", padx=24, pady=(0, 14))

        tab_bar = tk.Frame(tab_container, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                           highlightthickness=1, padx=4, pady=4)
        tab_bar.pack(fill="x")

        current_tab_var = tk.IntVar(value=initial_tab)

        tab_btn_status = tk.Label(
            tab_bar, text="  ⚡  Live Mesh Dashboard  ", font=(FONT_FAMILY, 10, "bold"),
            bg=ACCENT if initial_tab == 0 else SURFACE,
            fg="#ffffff" if initial_tab == 0 else TEXT_MUTED,
            padx=16, pady=8, cursor="hand2"
        )
        tab_btn_status.pack(side="left", fill="x", expand=True, padx=2)

        tab_btn_config = tk.Label(
            tab_bar, text="  ⚙️  Settings & Policies  ", font=(FONT_FAMILY, 10, "bold"),
            bg=ACCENT if initial_tab == 1 else SURFACE,
            fg="#ffffff" if initial_tab == 1 else TEXT_MUTED,
            padx=16, pady=8, cursor="hand2"
        )
        tab_btn_config.pack(side="left", fill="x", expand=True, padx=2)

        # Content Container Frames
        content_container = tk.Frame(root, bg=BG)
        content_container.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        tab_status_frame = tk.Frame(content_container, bg=BG)
        tab_config_frame = tk.Frame(content_container, bg=BG)

        def switch_tab(tab_idx: int):
            current_tab_var.set(tab_idx)
            if tab_idx == 0:
                tab_btn_status.configure(bg=ACCENT, fg="#ffffff")
                tab_btn_config.configure(bg=SURFACE, fg=TEXT_MUTED)
                tab_config_frame.pack_forget()
                tab_status_frame.pack(fill="both", expand=True)
            else:
                tab_btn_config.configure(bg=ACCENT, fg="#ffffff")
                tab_btn_status.configure(bg=SURFACE, fg=TEXT_MUTED)
                tab_status_frame.pack_forget()
                tab_config_frame.pack(fill="both", expand=True)

        tab_btn_status.bind("<Button-1>", lambda e: switch_tab(0))
        tab_btn_config.bind("<Button-1>", lambda e: switch_tab(1))

        # ======================================================================
        # TAB 1: LIVE STATUS
        # ======================================================================
        status_canvas = tk.Canvas(tab_status_frame, bg=BG, highlightthickness=0)
        status_scrollbar = ttk.Scrollbar(tab_status_frame, orient="vertical", command=status_canvas.yview)
        status_scroll_content = tk.Frame(status_canvas, bg=BG)

        status_scroll_content.bind(
            "<Configure>",
            lambda e: status_canvas.configure(scrollregion=status_canvas.bbox("all"))
        )
        canvas_window_status = status_canvas.create_window((0, 0), window=status_scroll_content, anchor="nw")

        def _on_status_canvas_resize(event):
            status_canvas.itemconfig(canvas_window_status, width=event.width)

        status_canvas.bind("<Configure>", _on_status_canvas_resize)
        status_canvas.configure(yscrollcommand=status_scrollbar.set)

        status_canvas.pack(side="left", fill="both", expand=True)
        status_scrollbar.pack(side="right", fill="y")

        # 1. Quick Stats Banner
        stats_card = tk.Frame(status_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                              highlightthickness=1, padx=16, pady=12)
        stats_card.pack(fill="x", pady=(0, 14))

        stat_col1 = tk.Frame(stats_card, bg=SURFACE)
        stat_col1.pack(side="left", fill="x", expand=True)
        stat_deliv_val = tk.StringVar(value="0")
        tk.Label(stat_col1, textvariable=stat_deliv_val, bg=SURFACE, fg=SUCCESS,
                 font=(FONT_FAMILY, 15, "bold")).pack(anchor="w")
        tk.Label(stat_col1, text="Session Delivered", bg=SURFACE, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 8)).pack(anchor="w")

        # Divider 1
        tk.Frame(stats_card, bg=SURFACE_BORDER, width=1, height=36).pack(side="left", padx=8)

        stat_col2 = tk.Frame(stats_card, bg=SURFACE)
        stat_col2.pack(side="left", fill="x", expand=True)
        stat_queue_val = tk.StringVar(value="0")
        stat_queue_label = tk.Label(stat_col2, textvariable=stat_queue_val, bg=SURFACE,
                                    fg=TEXT_MUTED, font=(FONT_FAMILY, 15, "bold"))
        stat_queue_label.pack(anchor="w")
        stat_queue_sublabel_val = tk.StringVar(value="Outbox Queued")
        stat_queue_sublabel = tk.Label(stat_col2, textvariable=stat_queue_sublabel_val, bg=SURFACE,
                                       fg=TEXT_MUTED, font=(FONT_FAMILY, 8))
        stat_queue_sublabel.pack(anchor="w")


        # Divider 2
        tk.Frame(stats_card, bg=SURFACE_BORDER, width=1, height=36).pack(side="left", padx=8)

        stat_col3 = tk.Frame(stats_card, bg=SURFACE)
        stat_col3.pack(side="left", fill="x", expand=True)
        stat_dev_val = tk.StringVar(value="3 Online")
        tk.Label(stat_col3, textvariable=stat_dev_val, bg=SURFACE, fg=TEXT,
                 font=(FONT_FAMILY, 15, "bold")).pack(anchor="w")
        tk.Label(stat_col3, text="Mesh Connected", bg=SURFACE, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 8)).pack(anchor="w")

        # 2. Section Header: Active Devices + Reconnect All
        dev_sec_header = tk.Frame(status_scroll_content, bg=BG)
        dev_sec_header.pack(fill="x", pady=(4, 8))
        tk.Label(dev_sec_header, text="CONNECTED MESH NODES", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(side="left")

        def reconnect_all_nodes():
            def _worker():
                cfg = _load_config_file()
                for dev in cfg.get("devices", []):
                    addr = dev.get("device", "")
                    if addr and dev.get("enabled", True):
                        try:
                            subprocess.run(["adb", "connect", addr], capture_output=True, timeout=5)
                        except Exception:
                            pass
            threading.Thread(target=_worker, daemon=True).start()
            messagebox.showinfo("Reconnecting", "Issued ADB connect commands to all enabled mesh nodes.")

        recon_all_btn = tk.Button(dev_sec_header, text="🔄 Reconnect All Nodes", command=reconnect_all_nodes,
                                  bg=SURFACE, fg=ACCENT_HOVER, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                                  font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=10, pady=3, cursor="hand2",
                                  highlightthickness=1, highlightbackground=SURFACE_BORDER)
        recon_all_btn.pack(side="right")
        _add_hover_effect(recon_all_btn, SURFACE, SURFACE_HOVER)

        device_cards_box = tk.Frame(status_scroll_content, bg=BG)
        device_cards_box.pack(fill="x")

        device_rows: dict[str, dict] = {}

        def create_modern_device_tile(name: str, is_domain: bool = False) -> dict:
            tile = tk.Frame(device_cards_box, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                            highlightthickness=1, padx=14, pady=12)
            tile.pack(fill="x", pady=4)

            # Left Badge Box
            badge_text, friendly_name, badge_color = _get_device_meta(name)
            if is_domain:
                badge_text = "SYNC"
                friendly_name = name
                badge_color = "#3b82f6"

            badge_box = tk.Frame(tile, bg=badge_color, width=44, height=44, padx=4, pady=4)
            badge_box.pack(side="left", padx=(0, 12))
            badge_box.pack_propagate(False)

            badge_lbl = tk.Label(badge_box, text=badge_text, bg=badge_color, fg="#ffffff",
                                 font=(FONT_FAMILY, 8, "bold"))
            badge_lbl.pack(expand=True)

            # Center Info Col
            info_col = tk.Frame(tile, bg=SURFACE)
            info_col.pack(side="left", fill="x", expand=True)

            title_row = tk.Frame(info_col, bg=SURFACE)
            title_row.pack(fill="x", anchor="w")

            name_lbl = tk.Label(title_row, text=friendly_name,
                                bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 10, "bold"))
            name_lbl.pack(side="left")

            # Status Badge Pill (Container with custom colors)
            badge_frame = tk.Frame(title_row, bg=SURFACE_CARD, padx=8, pady=2)
            badge_frame.pack(side="left", padx=10)

            dot_canvas = tk.Canvas(badge_frame, width=8, height=8, bg=SURFACE_CARD, highlightthickness=0)
            dot_canvas.pack(side="left", padx=(0, 5))
            dot_oval = dot_canvas.create_oval(1, 1, 7, 7, fill=TEXT_MUTED, outline="")

            status_text = tk.StringVar(value="Connecting...")
            status_lbl = tk.Label(badge_frame, textvariable=status_text, bg=SURFACE_CARD,
                                  fg=TEXT_MUTED, font=(FONT_FAMILY, 8, "bold"))
            status_lbl.pack(side="left")

            desc_lbl = tk.Label(info_col, text="", bg=SURFACE, fg=TEXT_MUTED, font=(FONT_FAMILY, 8))
            desc_lbl.pack(anchor="w", pady=(3, 0))

            # Right Action Buttons
            btn_box = tk.Frame(tile, bg=SURFACE)
            btn_box.pack(side="right")

            btn_var = tk.StringVar(value="Pause")
            action_btn = None
            recon_btn = None

            if not is_domain:
                def make_toggle(dname: str, bvar: tk.StringVar):
                    def do_toggle():
                        if hub.is_device_paused(dname):
                            hub.resume_device(dname)
                            bvar.set("Pause")
                        else:
                            hub.pause_device(dname)
                            bvar.set("Resume")
                    return do_toggle

                def make_reconnect(dname: str):
                    def do_recon():
                        cfg = _load_config_file()
                        for dev in cfg.get("devices", []):
                            if dev.get("name") == dname:
                                addr = dev.get("device")
                                if addr:
                                    def _bg_conn():
                                        try:
                                            subprocess.run(["adb", "connect", addr], capture_output=True, timeout=5)
                                        except Exception:
                                            pass
                                    threading.Thread(target=_bg_conn, daemon=True).start()
                                    messagebox.showinfo("Reconnect", f"Reconnecting to {dname} ({addr})...")
                    return do_recon

                recon_btn = tk.Button(
                    btn_box, text="🔄", command=make_reconnect(name),
                    bg=SURFACE_CARD, fg=TEXT_MUTED, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                    font=(FONT_FAMILY, 8), relief="flat", padx=8, pady=4, cursor="hand2",
                    highlightthickness=1, highlightbackground=SURFACE_BORDER
                )
                recon_btn.pack(side="left", padx=(0, 6))
                _add_hover_effect(recon_btn, SURFACE_CARD, SURFACE_HOVER)

                action_btn = tk.Button(
                    btn_box, textvariable=btn_var, command=make_toggle(name, btn_var),
                    bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                    font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=12, pady=4, cursor="hand2",
                    highlightthickness=1, highlightbackground=SURFACE_BORDER
                )
                action_btn.pack(side="left")
                _add_hover_effect(action_btn, SURFACE_CARD, SURFACE_HOVER)

            return {
                "tile": tile, "dot_canvas": dot_canvas, "dot_oval": dot_oval,
                "badge_frame": badge_frame, "status_text": status_text,
                "status_lbl": status_lbl, "desc_lbl": desc_lbl,
                "action_btn": action_btn, "btn_var": btn_var
            }

        # 3. Quick Screenshot Access Card
        ss_sec_header = tk.Frame(status_scroll_content, bg=BG)
        ss_sec_header.pack(fill="x", pady=(14, 8))
        tk.Label(ss_sec_header, text="QUICK SCREENSHOT FOLDERS", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(side="left")

        ss_quick_card = tk.Frame(status_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                                 highlightthickness=1, padx=14, pady=12)
        ss_quick_card.pack(fill="x", pady=(0, 10))

        def open_device_ss_dir(dname: str):
            cfg = _load_config_file()
            base_dir = cfg.get("screenshot_backup_dir") or os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots", "Temporary Screenshots")
            custom_paths = cfg.get("device_screenshot_paths", {})
            
            if dname and custom_paths.get(dname, "").strip():
                target = custom_paths[dname].strip()
            elif dname and cfg.get("screenshot_device_subfolders", False):
                target = os.path.join(base_dir, dname)
            else:
                target = base_dir

            try:
                os.makedirs(target, exist_ok=True)
                os.startfile(target)
            except Exception as e:
                messagebox.showerror("Folder Error", f"Could not open folder:\n{target}\n\nError: {e}")

        def open_camera_photos_dir():
            cfg = _load_config_file()
            base_dir = cfg.get("screenshot_backup_dir") or os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots", "Temporary Screenshots")
            target = cfg.get("camera_backup_dir") or os.path.join(base_dir, "Camera")
            try:
                os.makedirs(target, exist_ok=True)
                os.startfile(target)
            except Exception as e:
                messagebox.showerror("Folder Error", f"Could not open folder:\n{target}\n\nError: {e}")

        ss_btn_row = tk.Frame(ss_quick_card, bg=SURFACE)
        ss_btn_row.pack(fill="x")

        btn_all_ss = tk.Button(ss_btn_row, text="📁 All Screenshots", command=lambda: open_device_ss_dir(""),
                               bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                               font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=10, pady=5, cursor="hand2",
                               highlightthickness=1, highlightbackground=SURFACE_BORDER)
        btn_all_ss.pack(side="left", padx=(0, 6))
        _add_hover_effect(btn_all_ss, SURFACE_CARD, SURFACE_HOVER)

        btn_cam_photos = tk.Button(ss_btn_row, text="📷 Camera Photos", command=open_camera_photos_dir,
                                   bg=SURFACE_CARD, fg="#38bdf8", activebackground=SURFACE_HOVER, activeforeground="#38bdf8",
                                   font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=10, pady=5, cursor="hand2",
                                   highlightthickness=1, highlightbackground=SURFACE_BORDER)
        btn_cam_photos.pack(side="left", padx=(0, 6))
        _add_hover_effect(btn_cam_photos, SURFACE_CARD, SURFACE_HOVER)

        btn_phone_ss = tk.Button(ss_btn_row, text="📱 S24 Ultra", command=lambda: open_device_ss_dir("phone"),
                                 bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                                 font=(FONT_FAMILY, 8), relief="flat", padx=10, pady=5, cursor="hand2",
                                 highlightthickness=1, highlightbackground=SURFACE_BORDER)
        btn_phone_ss.pack(side="left", padx=4)
        _add_hover_effect(btn_phone_ss, SURFACE_CARD, SURFACE_HOVER)

        btn_tab_ss = tk.Button(ss_btn_row, text="📟 Tab S10", command=lambda: open_device_ss_dir("tab"),
                               bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                               font=(FONT_FAMILY, 8), relief="flat", padx=10, pady=5, cursor="hand2",
                               highlightthickness=1, highlightbackground=SURFACE_BORDER)
        btn_tab_ss.pack(side="left", padx=4)
        _add_hover_effect(btn_tab_ss, SURFACE_CARD, SURFACE_HOVER)

        btn_pixel_ss = tk.Button(ss_btn_row, text="📲 Pixel", command=lambda: open_device_ss_dir("pixel"),
                                 bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                                 font=(FONT_FAMILY, 8), relief="flat", padx=10, pady=5, cursor="hand2",
                                 highlightthickness=1, highlightbackground=SURFACE_BORDER)
        btn_pixel_ss.pack(side="left", padx=4)
        _add_hover_effect(btn_pixel_ss, SURFACE_CARD, SURFACE_HOVER)

        # 4. Actions Bar
        actions_card = tk.Frame(status_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                                highlightthickness=1, padx=14, pady=10)
        actions_card.pack(fill="x", pady=(6, 14))

        pause_all_var = tk.StringVar(value="⏸️ Pause All Sync")

        def toggle_pause_all() -> None:
            hub.paused = not hub.paused
            pause_all_var.set("▶️ Resume All Sync" if hub.paused else "⏸️ Pause All Sync")

        pause_all_btn = tk.Button(
            actions_card, textvariable=pause_all_var, command=toggle_pause_all,
            bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
            font=(FONT_FAMILY, 9, "bold"), relief="flat", padx=12, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=SURFACE_BORDER
        )
        pause_all_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        _add_hover_effect(pause_all_btn, SURFACE_CARD, SURFACE_HOVER)

        def show_log_viewer() -> None:
            _open_log_window(root)

        view_log_btn = tk.Button(
            actions_card, text="📜 Live Logs", command=show_log_viewer,
            bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
            font=(FONT_FAMILY, 9), relief="flat", padx=12, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=SURFACE_BORDER
        )
        view_log_btn.pack(side="left", padx=4)
        _add_hover_effect(view_log_btn, SURFACE_CARD, SURFACE_HOVER)

        def clear_logs() -> None:
            try:
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.write("")
                messagebox.showinfo("Logs Cleared", "copyclip.log cleared. Streaming fresh logs from now on.", parent=root)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to clear logs: {e}", parent=root)

        clear_log_btn = tk.Button(
            actions_card, text="🗑️ Clear Logs", command=clear_logs,
            bg=SURFACE_CARD, fg=TEXT_MUTED, activebackground=SURFACE_HOVER, activeforeground=TEXT,
            font=(FONT_FAMILY, 9), relief="flat", padx=12, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=SURFACE_BORDER
        )
        clear_log_btn.pack(side="left", padx=(4, 0))
        _add_hover_effect(clear_log_btn, SURFACE_CARD, SURFACE_HOVER)

        # ======================================================================
        # TAB 2: SETTINGS & DEVICES
        # ======================================================================
        config_canvas = tk.Canvas(tab_config_frame, bg=BG, highlightthickness=0)
        config_scrollbar = ttk.Scrollbar(tab_config_frame, orient="vertical", command=config_canvas.yview)
        config_scroll_content = tk.Frame(config_canvas, bg=BG)

        config_scroll_content.bind(
            "<Configure>",
            lambda e: config_canvas.configure(scrollregion=config_canvas.bbox("all"))
        )
        canvas_window_config = config_canvas.create_window((0, 0), window=config_scroll_content, anchor="nw")

        def _on_config_canvas_resize(event):
            config_canvas.itemconfig(canvas_window_config, width=event.width)

        config_canvas.bind("<Configure>", _on_config_canvas_resize)
        config_canvas.configure(yscrollcommand=config_scrollbar.set)

        config_canvas.pack(side="left", fill="both", expand=True)
        config_scrollbar.pack(side="right", fill="y")

        # 1. Device Manager Section
        dev_header = tk.Frame(config_scroll_content, bg=BG)
        dev_header.pack(fill="x", pady=(4, 8))

        tk.Label(dev_header, text="CONFIGURED MESH DEVICES", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(side="left")

        curr_cfg = _load_config_file()
        devices_list: list[dict] = list(curr_cfg.get("devices", []))

        devices_box = tk.Frame(config_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                               highlightthickness=1, padx=12, pady=8)
        devices_box.pack(fill="x", pady=(0, 10))

        def refresh_devices_ui():
            for child in devices_box.winfo_children():
                child.destroy()

            if not devices_list:
                tk.Label(devices_box, text="No devices configured in config.json",
                         bg=SURFACE, fg=TEXT_MUTED, font=(FONT_FAMILY, 9)).pack(pady=12)
                return

            for idx, dev in enumerate(devices_list):
                dname = dev.get("name", f"device_{idx}")
                daddr = dev.get("device", "unknown")
                dtrans = dev.get("transport", "adb")
                denabled = dev.get("enabled", True)

                row = tk.Frame(devices_box, bg=SURFACE_CARD if denabled else SURFACE,
                               highlightbackground=SURFACE_BORDER, highlightthickness=1,
                               padx=12, pady=8)
                row.pack(fill="x", pady=4)

                badge_text, friendly_name, badge_color = _get_device_meta(dname)
                badge_lbl = tk.Label(row, text=f" {badge_text} ", bg=badge_color, fg="#ffffff",
                                     font=(FONT_FAMILY, 8, "bold"))
                badge_lbl.pack(side="left", padx=(0, 10))

                info_f = tk.Frame(row, bg=row.cget("bg"))
                info_f.pack(side="left", fill="x", expand=True)

                status_suffix = "" if denabled else " (Disabled)"
                name_lbl = tk.Label(info_f, text=f"{friendly_name}{status_suffix}", bg=row.cget("bg"),
                                    fg=TEXT if denabled else TEXT_MUTED, font=(FONT_FAMILY, 9, "bold"))
                name_lbl.pack(anchor="w")

                addr_lbl = tk.Label(info_f, text=f"Node: {dname} · {daddr} · {dtrans.upper()}", bg=row.cget("bg"),
                                    fg=TEXT_MUTED, font=(FONT_FAMILY, 8))
                addr_lbl.pack(anchor="w")

                btn_f = tk.Frame(row, bg=row.cget("bg"))
                btn_f.pack(side="right")

                def make_edit(i: int):
                    def edit_dev():
                        _open_device_dialog(root, devices_list[i], lambda u: on_dev_updated(i, u))
                    return edit_dev

                def make_remove(i: int):
                    def remove_dev():
                        d = devices_list[i]
                        if messagebox.askyesno("Remove Device", f"Remove '{d.get('name')}' from mesh configuration?"):
                            devices_list.pop(i)
                            refresh_devices_ui()
                    return remove_dev

                edit_b = tk.Button(btn_f, text="Edit", command=make_edit(idx),
                                   bg=SURFACE, fg=TEXT, activebackground=SURFACE_HOVER,
                                   font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=10, pady=3,
                                   highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
                edit_b.pack(side="left", padx=2)
                _add_hover_effect(edit_b, SURFACE, SURFACE_HOVER)

                del_b = tk.Button(btn_f, text="Delete", command=make_remove(idx),
                                  bg=SURFACE, fg=ERROR_TEXT, activebackground=SURFACE_HOVER,
                                  font=(FONT_FAMILY, 8, "bold"), relief="flat", padx=8, pady=3,
                                  highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
                del_b.pack(side="left", padx=2)
                _add_hover_effect(del_b, SURFACE, SURFACE_HOVER)

        def on_dev_updated(index: int, updated: dict):
            if index >= 0:
                devices_list[index] = updated
            else:
                devices_list.append(updated)
            refresh_devices_ui()

        refresh_devices_ui()

        def add_device_dialog():
            _open_device_dialog(root, None, lambda c: on_dev_updated(-1, c))

        add_dev_btn = tk.Button(
            config_scroll_content, text="+ Add New Mesh Device", command=add_device_dialog,
            bg=SURFACE, fg=ACCENT_HOVER, activebackground=SURFACE_HOVER, activeforeground=TEXT,
            font=(FONT_FAMILY, 9, "bold"), relief="flat", padx=14, pady=8, cursor="hand2",
            highlightthickness=1, highlightbackground=SURFACE_BORDER
        )
        add_dev_btn.pack(anchor="w", pady=(0, 14))
        _add_hover_effect(add_dev_btn, SURFACE, SURFACE_HOVER)

        # 2. Screenshot Storage & Custom Paths Section
        tk.Label(config_scroll_content, text="SCREENSHOT STORAGE & CUSTOM DEVICE PATHS", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(4, 8))

        ss_card = tk.Frame(config_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                           highlightthickness=1, padx=16, pady=14)
        ss_card.pack(fill="x", pady=(0, 14))

        inc_device_ss_var = tk.BooleanVar(value=curr_cfg.get("screenshot_backup_include_devices", True))
        chk_ss_enable = tk.Checkbutton(ss_card, text="Auto-Save Synced Screenshots to Local Disk",
                                       variable=inc_device_ss_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                       activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 9, "bold"))
        chk_ss_enable.pack(anchor="w", pady=(0, 8))

        # Base Save Folder
        tk.Label(ss_card, text="Default Base Screenshot Folder:", bg=SURFACE, fg=TEXT,
                 font=(FONT_FAMILY, 8, "bold")).pack(anchor="w")

        base_ss_path_row = tk.Frame(ss_card, bg=SURFACE)
        base_ss_path_row.pack(fill="x", pady=(2, 8))

        default_ss_path = curr_cfg.get("screenshot_backup_dir") or os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots", "Temporary Screenshots")
        base_ss_entry = tk.Entry(base_ss_path_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                                 font=(FONT_FAMILY, 8), relief="flat",
                                 highlightthickness=1, highlightbackground=SURFACE_BORDER)
        base_ss_entry.insert(0, default_ss_path)
        base_ss_entry.pack(side="left", fill="x", expand=True, ipady=3)

        def browse_base_ss():
            picked = filedialog.askdirectory(title="Select Default Screenshot Folder", initialdir=base_ss_entry.get())
            if picked:
                base_ss_entry.delete(0, "end")
                base_ss_entry.insert(0, picked)

        browse_base_btn = tk.Button(base_ss_path_row, text="Browse...", command=browse_base_ss,
                                    bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER,
                                    font=(FONT_FAMILY, 8), relief="flat", padx=10, pady=3,
                                    highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
        browse_base_btn.pack(side="left", padx=(6, 0))
        _add_hover_effect(browse_base_btn, SURFACE_CARD, SURFACE_HOVER)

        # Organize into subfolders checkbox
        subfolders_var = tk.BooleanVar(value=curr_cfg.get("screenshot_device_subfolders", False))
        chk_sub = tk.Checkbutton(ss_card, text="Organize screenshots into device subfolders (\\phone, \\tab, \\pixel)",
                                 variable=subfolders_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                 activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 9))
        chk_sub.pack(anchor="w", pady=(0, 10))

        # Custom Paths per device
        dev_ss_paths = curr_cfg.get("device_screenshot_paths", {})
        tk.Label(ss_card, text="Custom Screenshot Paths per Device (Optional Overrides):",
                 bg=SURFACE, fg=TEXT_MUTED, font=(FONT_FAMILY, 8, "bold")).pack(anchor="w", pady=(4, 4))

        custom_entries: dict[str, tk.Entry] = {}

        for dkey, dlabel in [("phone", "Samsung S24 Ultra Path:"),
                             ("tab", "Samsung Tab S10 Path:"),
                             ("pixel", "Google Pixel 1 Path:")]:
            drow = tk.Frame(ss_card, bg=SURFACE)
            drow.pack(fill="x", pady=2)
            tk.Label(drow, text=dlabel, bg=SURFACE, fg=TEXT_DIM, font=(FONT_FAMILY, 8), width=24, anchor="w").pack(side="left")
            
            d_entry = tk.Entry(drow, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                               font=(FONT_FAMILY, 8), relief="flat",
                               highlightthickness=1, highlightbackground=SURFACE_BORDER)
            d_entry.insert(0, dev_ss_paths.get(dkey, ""))
            d_entry.pack(side="left", fill="x", expand=True, ipady=2)
            custom_entries[dkey] = d_entry

            def make_browse(entry_widget: tk.Entry, title: str):
                def _do_br():
                    cur = entry_widget.get().strip() or base_ss_entry.get().strip()
                    picked = filedialog.askdirectory(title=title, initialdir=cur)
                    if picked:
                        entry_widget.delete(0, "end")
                        entry_widget.insert(0, picked)
                return _do_br

            br_b = tk.Button(drow, text="Browse...", command=make_browse(d_entry, f"Select {dlabel} Folder"),
                             bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER,
                             font=(FONT_FAMILY, 8), relief="flat", padx=8, pady=2,
                             highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
            br_b.pack(side="left", padx=(6, 0))
            _add_hover_effect(br_b, SURFACE_CARD, SURFACE_HOVER)

        exc_history_var = tk.BooleanVar(value=curr_cfg.get("screenshot_exclude_from_history", True))
        chk_winv = tk.Checkbutton(ss_card, text="Exclude Synced Images from Windows Win+V History (Prevents eviction)",
                                  variable=exc_history_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                  activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 8))
        chk_winv.pack(anchor="w", pady=(8, 0))

        camera_backup_var = tk.BooleanVar(value=curr_cfg.get("camera_backup", True))
        chk_cam = tk.Checkbutton(ss_card, text="Auto-Backup Mobile & Tablet Camera Photos (DCIM/Camera to laptop)",
                                 variable=camera_backup_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                 activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 8))
        chk_cam.pack(anchor="w", pady=(6, 0))

        # Camera Backup Directory Picker
        cam_dir_frame = tk.Frame(ss_card, bg=SURFACE)
        cam_dir_frame.pack(fill="x", padx=(20, 0), pady=(4, 6))

        tk.Label(cam_dir_frame, text="Default Camera Backup Folder:", bg=SURFACE, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 8, "bold")).pack(anchor="w")

        cam_dir_row = tk.Frame(cam_dir_frame, bg=SURFACE)
        cam_dir_row.pack(fill="x", pady=(2, 0))

        camera_dir_entry = tk.Entry(cam_dir_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                                    font=(FONT_FAMILY, 8), relief="flat",
                                    highlightthickness=1, highlightbackground=SURFACE_BORDER)
        default_cam_dir = curr_cfg.get("camera_backup_dir", "")
        camera_dir_entry.insert(0, default_cam_dir)
        camera_dir_entry.pack(side="left", fill="x", expand=True, ipady=2)

        def _browse_cam_dir():
            cur = camera_dir_entry.get().strip() or os.path.join(base_ss_entry.get().strip(), "Camera")
            picked = filedialog.askdirectory(title="Select Camera Backup Folder", initialdir=cur)
            if picked:
                camera_dir_entry.delete(0, "end")
                camera_dir_entry.insert(0, picked)

        cam_br_btn = tk.Button(cam_dir_row, text="Browse...", command=_browse_cam_dir,
                               bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER,
                               font=(FONT_FAMILY, 8), relief="flat", padx=8, pady=2,
                               highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
        cam_br_btn.pack(side="left", padx=(6, 0))
        _add_hover_effect(cam_br_btn, SURFACE_CARD, SURFACE_HOVER)

        tk.Label(cam_dir_frame, text="Leave empty to default to Temporary Screenshots\\Camera",
                 bg=SURFACE, fg=TEXT_DIM, font=(FONT_FAMILY, 8)).pack(anchor="w")

        camera_subfolders_var = tk.BooleanVar(value=curr_cfg.get("camera_device_subfolders", False))
        chk_cam_sub = tk.Checkbutton(ss_card, text="Organize Camera Photos by Device Subfolders (Camera\\<device>\\)",
                                     variable=camera_subfolders_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                     activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 8))
        chk_cam_sub.pack(anchor="w", padx=(20, 0), pady=(2, 0))

        # 3. Clipboard Sync Policies Section
        tk.Label(config_scroll_content, text="CLIPBOARD MESH POLICIES & LIMITS", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(10, 8))

        policy_card = tk.Frame(config_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                               highlightthickness=1, padx=16, pady=14)
        policy_card.pack(fill="x", pady=(0, 14))

        allow_images_var = tk.BooleanVar(value=curr_cfg.get("allow_images", True))
        chk_img_sync = tk.Checkbutton(policy_card, text="Allow Image & Screenshot Clipboard Mesh Sync",
                                      variable=allow_images_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                      activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 9, "bold"))
        chk_img_sync.pack(anchor="w", pady=(0, 6))

        # Max MB Size & Max Chars
        limit_row = tk.Frame(policy_card, bg=SURFACE)
        limit_row.pack(fill="x", pady=4)

        max_bytes = curr_cfg.get("max_image_bytes", 25165824)
        max_mb_val = int(round(max_bytes / (1024 * 1024)))

        tk.Label(limit_row, text="Max Image Size (MB):", bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9)).pack(side="left")
        max_mb_entry = tk.Entry(limit_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                                font=(FONT_MONO, 9), width=6, relief="flat",
                                highlightthickness=1, highlightbackground=SURFACE_BORDER)
        max_mb_entry.insert(0, str(max_mb_val))
        max_mb_entry.pack(side="left", padx=(8, 20))

        tk.Label(limit_row, text="Max Text Payload (chars):", bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9)).pack(side="left")
        max_chars_entry = tk.Entry(limit_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                                   font=(FONT_MONO, 9), width=10, relief="flat",
                                   highlightthickness=1, highlightbackground=SURFACE_BORDER)
        max_chars_entry.insert(0, str(curr_cfg.get("max_chars", 800000)))
        max_chars_entry.pack(side="left", padx=8)

        adopt_var = tk.BooleanVar(value=curr_cfg.get("adopt_on_connect", False))
        chk_adopt = tk.Checkbutton(policy_card, text="Adopt device clipboard baseline on connect (overwrites laptop clip)",
                                   variable=adopt_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                   activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 8))
        chk_adopt.pack(anchor="w", pady=(6, 0))

        allow_recopy_var = tk.BooleanVar(value=curr_cfg.get("allow_recopy", True))
        chk_recopy = tk.Checkbutton(policy_card, text="Intentional Re-Copy Refresh (Allow repeat copies to refresh mesh)",
                                    variable=allow_recopy_var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_CARD,
                                    activebackground=SURFACE, activeforeground=TEXT, font=(FONT_FAMILY, 8))
        chk_recopy.pack(anchor="w", pady=(4, 0))
        tk.Label(policy_card, text="Re-copying the same text after 0.3s refreshes it across all devices immediately, bypassing the 90s dedup window.",
                 bg=SURFACE, fg=TEXT_DIM, font=(FONT_FAMILY, 8)).pack(anchor="w", padx=(20, 0), pady=(0, 2))

        # Sensitive credentials subsection container
        sens_subcard = tk.Frame(policy_card, bg=SURFACE_CARD, highlightbackground=SURFACE_BORDER,
                                highlightthickness=1, padx=14, pady=12)
        sens_subcard.pack(fill="x", pady=(14, 4))

        tk.Label(
            sens_subcard,
            text="SENSITIVE CREDENTIALS & PASSWORD SYNC (EXTRA_IS_SENSITIVE)",
            bg=SURFACE_CARD, fg="#f59e0b", font=(FONT_FAMILY, 9, "bold")
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            sens_subcard,
            text="Controls how 2FA OTPs, Authenticator codes, and password manager clips are handled across the mesh.",
            bg=SURFACE_CARD, fg=TEXT_MUTED, font=(FONT_FAMILY, 8)
        ).pack(anchor="w", pady=(0, 8))

        allow_sensitive_var = tk.BooleanVar(value=curr_cfg.get("allow_sensitive", False))
        direct_password_sync_var = tk.BooleanVar(value=curr_cfg.get("direct_password_sync", False))

        def _update_sensitive_ui_state():
            is_enabled = allow_sensitive_var.get()
            if is_enabled:
                chk_direct_pwd.config(state="normal", fg="#f59e0b", activeforeground="#f59e0b")
                lbl_direct_pwd.config(fg=TEXT_DIM)
                sensitive_wipe_entry.config(state="normal", bg=SURFACE, fg=TEXT)
                lbl_wipe_title.config(fg=TEXT)
                lbl_wipe_hint.config(fg=TEXT_DIM)
            else:
                direct_password_sync_var.set(False)
                chk_direct_pwd.config(state="disabled", fg=TEXT_MUTED, activeforeground=TEXT_MUTED)
                lbl_direct_pwd.config(fg=TEXT_MUTED)
                sensitive_wipe_entry.config(state="disabled", bg=SURFACE_BORDER, fg=TEXT_MUTED)
                lbl_wipe_title.config(fg=TEXT_MUTED)
                lbl_wipe_hint.config(fg=TEXT_MUTED)

        def _on_sensitive_toggle():
            if allow_sensitive_var.get():
                warn_msg = (
                    "Sync Sensitive Clips (OTPs & Credentials)\n\n"
                    "Enabling this feature will synchronize 2FA OTP codes, Google Authenticator "
                    "tiles, and password-manager clipboard copies across your mesh devices.\n\n"
                    "Security Protections Enforced:\n"
                    " - Auto-wipe timer (default 10s, configurable) across devices\n"
                    " - Excluded from Windows Clipboard History (Win+V)\n"
                    " - Blocked from Microsoft Cloud Clipboard sync\n"
                    " - Never persisted to outbox storage on SSD (volatile RAM only)\n\n"
                    "Important Caution:\n"
                    "Any background application running with standard user privileges on Windows "
                    "can read the Windows clipboard while a code is present.\n\n"
                    "Do you want to enable sensitive clipboard sync anyway?"
                )
                confirmed = messagebox.askyesno("Enable Sensitive Clipboard Sync", warn_msg, icon="warning")
                if not confirmed:
                    allow_sensitive_var.set(False)
            _update_sensitive_ui_state()

        chk_sens_sync = tk.Checkbutton(
            sens_subcard,
            text="Option 1: Sync Sensitive Clips (OTPs, Authenticator codes) [EXPERIMENTAL]",
            variable=allow_sensitive_var,
            command=_on_sensitive_toggle,
            bg=SURFACE_CARD, fg="#f59e0b", selectcolor=SURFACE,
            activebackground=SURFACE_CARD, activeforeground="#f59e0b",
            font=(FONT_FAMILY, 9, "bold")
        )
        chk_sens_sync.pack(anchor="w", pady=(2, 0))

        tk.Label(
            sens_subcard,
            text="Master gate. Enables sync with 10s auto-wipe timer, Win+V history exclusion, and cloud sync blocking.",
            bg=SURFACE_CARD, fg=TEXT_DIM, font=(FONT_FAMILY, 8)
        ).pack(anchor="w", padx=(24, 0), pady=(0, 4))

        wipe_row = tk.Frame(sens_subcard, bg=SURFACE_CARD)
        wipe_row.pack(anchor="w", padx=(24, 0), pady=(2, 6))
        lbl_wipe_title = tk.Label(wipe_row, text="Auto-Wipe Timer (seconds):", bg=SURFACE_CARD, fg=TEXT, font=(FONT_FAMILY, 9))
        lbl_wipe_title.pack(side="left")
        sensitive_wipe_entry = tk.Entry(wipe_row, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                                        font=(FONT_MONO, 9), width=5, relief="flat",
                                        highlightthickness=1, highlightbackground=SURFACE_BORDER)
        sensitive_wipe_entry.insert(0, str(int(round(curr_cfg.get("sensitive_wipe_s", 10.0)))))
        sensitive_wipe_entry.pack(side="left", padx=(8, 12))
        lbl_wipe_hint = tk.Label(wipe_row, text="(Default: 10s. Clears clipboard automatically on all devices)",
                                 bg=SURFACE_CARD, fg=TEXT_DIM, font=(FONT_FAMILY, 8))
        lbl_wipe_hint.pack(side="left")

        # Nested Option 2 Container
        opt2_frame = tk.Frame(sens_subcard, bg=SURFACE_CARD)
        opt2_frame.pack(fill="x", padx=(16, 0), pady=(6, 2))

        def _on_direct_password_toggle():
            if not allow_sensitive_var.get():
                direct_password_sync_var.set(False)
                return
            if direct_password_sync_var.get():
                warn_msg = (
                    "Direct Password Mode\n\n"
                    "Enabling this option will:\n"
                    " 1. Show passwords and credentials directly in Windows Clipboard History (Win+V)\n"
                    " 2. Keep passwords on the clipboard without auto-wiping after 10 seconds\n\n"
                    "Security Warning:\n"
                    "Passwords copied from Bitwarden or password managers will be stored in your Windows Clipboard History in plain text and will not be automatically deleted.\n\n"
                    "Do you want to enable direct password sync?"
                )
                confirmed = messagebox.askyesno("Enable Direct Password Mode", warn_msg, icon="warning")
                if not confirmed:
                    direct_password_sync_var.set(False)

        chk_direct_pwd = tk.Checkbutton(
            opt2_frame,
            text="└─ Option 2: Direct Password Mode (Show in Win+V History & Disable Auto-Wipe)",
            variable=direct_password_sync_var,
            command=_on_direct_password_toggle,
            bg=SURFACE_CARD, fg="#f59e0b", selectcolor=SURFACE,
            activebackground=SURFACE_CARD, activeforeground="#f59e0b",
            font=(FONT_FAMILY, 9, "bold")
        )
        chk_direct_pwd.pack(anchor="w")

        lbl_direct_pwd = tk.Label(
            opt2_frame,
            text="Requires Option 1. Relaxes lockdown for password managers: disables auto-wipe and shows credentials in Win+V history.",
            bg=SURFACE_CARD, fg=TEXT_DIM, font=(FONT_FAMILY, 8)
        )
        lbl_direct_pwd.pack(anchor="w", padx=(24, 0), pady=(0, 2))

        # Initialize dynamic state on window load
        _update_sensitive_ui_state()

        # 4. Network, Timers & Security Section
        tk.Label(config_scroll_content, text="NETWORK, TIMERS & CRYPTOGRAPHIC SECURITY", bg=BG, fg=TEXT_MUTED,
                 font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(10, 8))

        sec_card = tk.Frame(config_scroll_content, bg=SURFACE, highlightbackground=SURFACE_BORDER,
                            highlightthickness=1, padx=16, pady=14)
        sec_card.pack(fill="x", pady=(0, 16))

        # Port and Timers Row
        net_row = tk.Frame(sec_card, bg=SURFACE)
        net_row.pack(fill="x", pady=(0, 8))

        tk.Label(net_row, text="Push Port:", bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9)).pack(side="left")
        port_entry = tk.Entry(net_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                              font=(FONT_MONO, 9), width=6, relief="flat",
                              highlightthickness=1, highlightbackground=SURFACE_BORDER)
        port_entry.insert(0, str(curr_cfg.get("push_port", 8387)))
        port_entry.pack(side="left", padx=(6, 16))

        tk.Label(net_row, text="Ack Timeout (s):", bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9)).pack(side="left")
        ack_entry = tk.Entry(net_row, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                             font=(FONT_MONO, 9), width=5, relief="flat",
                             highlightthickness=1, highlightbackground=SURFACE_BORDER)
        ack_entry.insert(0, str(curr_cfg.get("ack_timeout_s", 4.0)))
        ack_entry.pack(side="left", padx=(6, 16))

        tk.Label(net_row, text="Log Level:", bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 9)).pack(side="left")
        log_level_combo = ttk.Combobox(net_row, values=["DEBUG", "INFO", "WARNING", "ERROR"], width=9, state="readonly")
        log_level_combo.set(curr_cfg.get("log_level", "INFO"))
        log_level_combo.pack(side="left", padx=6)

        # Cryptographic Key
        tk.Label(sec_card, text="Mutual HMAC-SHA256 Shared Key (`copyclip.key`):",
                 bg=SURFACE, fg=TEXT, font=(FONT_FAMILY, 8, "bold")).pack(anchor="w", pady=(8, 2))

        def copy_shared_key():
            try:
                if os.path.isfile(KEY_PATH):
                    with open(KEY_PATH, "r", encoding="utf-8") as f:
                        k = f.read().strip()
                    root.clipboard_clear()
                    root.clipboard_append(k)
                    messagebox.showinfo("Key Copied", "Shared cryptographic key copied to clipboard.")
                else:
                    messagebox.showwarning("Key Not Found", f"{KEY_PATH} does not exist.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to copy key: {e}")

        def rotate_shared_key():
            if messagebox.askyesno(
                "Rotate Secret Key",
                "Generate a new cryptographic shared key?\n\nConnected devices will automatically receive the updated key over secure ADB."
            ):
                try:
                    new_key = secrets.token_hex(32)
                    with open(KEY_PATH, "w", encoding="utf-8") as f:
                        f.write(new_key)
                    messagebox.showinfo("Key Rotated", "New key generated and saved. Devices will re-authenticate.")
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to rotate key: {e}")

        key_actions = tk.Frame(sec_card, bg=SURFACE)
        key_actions.pack(fill="x", pady=(4, 0))

        copy_key_btn = tk.Button(key_actions, text="📋 Copy Shared Key", command=copy_shared_key,
                                 bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER,
                                 font=(FONT_FAMILY, 8), relief="flat", padx=12, pady=4,
                                 highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
        copy_key_btn.pack(side="left", padx=(0, 6))
        _add_hover_effect(copy_key_btn, SURFACE_CARD, SURFACE_HOVER)

        rot_key_btn = tk.Button(key_actions, text="🔄 Rotate Key", command=rotate_shared_key,
                                bg=SURFACE_CARD, fg=TEXT, activebackground=SURFACE_HOVER,
                                font=(FONT_FAMILY, 8), relief="flat", padx=12, pady=4,
                                highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
        rot_key_btn.pack(side="left", padx=6)
        _add_hover_effect(rot_key_btn, SURFACE_CARD, SURFACE_HOVER)

        # 5. Sticky Save Button
        save_feedback_var = tk.StringVar(value="")
        save_feedback_lbl = tk.Label(config_scroll_content, textvariable=save_feedback_var,
                                     bg=BG, fg=SUCCESS, font=(FONT_FAMILY, 9, "bold"))
        save_feedback_lbl.pack(pady=(0, 6))

        def save_and_apply():
            try:
                max_c = int(max_chars_entry.get().strip())
                max_mb = int(max_mb_entry.get().strip())
                push_p = int(port_entry.get().strip())
                ack_t = float(ack_entry.get().strip())
                sens_wipe = float(sensitive_wipe_entry.get().strip())
                if sens_wipe < 1.0:
                    sens_wipe = 1.0
            except ValueError:
                messagebox.showerror("Validation Error", "Please verify numeric fields (Max chars, Max MB, Port, Ack timeout, Auto-Wipe Timer).")
                return

            new_cfg = dict(curr_cfg)
            new_cfg["devices"] = devices_list
            new_cfg["allow_images"] = allow_images_var.get()
            new_cfg["allow_recopy"] = allow_recopy_var.get()
            new_cfg["allow_sensitive"] = allow_sensitive_var.get()
            new_cfg["direct_password_sync"] = direct_password_sync_var.get()
            new_cfg["sensitive_wipe_s"] = sens_wipe
            new_cfg["max_image_bytes"] = max_mb * 1024 * 1024
            new_cfg["screenshot_backup_dir"] = base_ss_entry.get().strip()
            new_cfg["screenshot_backup_include_devices"] = inc_device_ss_var.get()
            new_cfg["screenshot_device_subfolders"] = subfolders_var.get()
            new_cfg["device_screenshot_paths"] = {
                k: e.get().strip() for k, e in custom_entries.items()
            }
            new_cfg["screenshot_exclude_from_history"] = exc_history_var.get()
            new_cfg["camera_backup"] = camera_backup_var.get()
            new_cfg["camera_backup_dir"] = camera_dir_entry.get().strip()
            new_cfg["camera_device_subfolders"] = camera_subfolders_var.get()
            new_cfg["max_chars"] = max_c
            new_cfg["adopt_on_connect"] = adopt_var.get()
            new_cfg["push_port"] = push_p
            new_cfg["ack_timeout_s"] = ack_t
            new_cfg["log_level"] = log_level_combo.get()

            try:
                _save_config_file(new_cfg)
                # Dynamic runtime hot-reload on the active Hub
                if hub is not None and hasattr(hub, "reload_config"):
                    hub.reload_config(new_cfg)
                save_feedback_var.set("✓ Configuration Saved & Hot-Reloaded Live!")
                root.after(3000, lambda: save_feedback_var.set(""))
            except Exception as e:
                messagebox.showerror("Save Error", f"Could not save configuration: {e}")

        save_btn = tk.Button(
            config_scroll_content, text="💾  Save & Apply Mesh Configuration", command=save_and_apply,
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOVER, activeforeground="#ffffff",
            font=(FONT_FAMILY, 10, "bold"), relief="flat", padx=16, pady=10, cursor="hand2"
        )
        save_btn.pack(fill="x", pady=(0, 24))
        _add_hover_effect(save_btn, ACCENT, ACCENT_HOVER, "#ffffff", "#ffffff")

        # ======================================================================
        # RECURSIVE MOUSEWHEEL BINDING (FIXES SCROLL EVERYWHERE)
        # ======================================================================
        _bind_mousewheel(status_scroll_content, status_canvas)
        _bind_mousewheel(config_scroll_content, config_canvas)

        # Show initial tab
        switch_tab(initial_tab)

        # ======================================================================
        # LIVE REFRESH LOOP
        # ======================================================================
        def refresh() -> None:
            try:
                status = hub.status()
            except Exception:
                root.after(2000, refresh)
                return

            # Update Header & Pause state
            is_globally_paused = getattr(hub, "paused", False)
            pause_all_var.set("▶️ Resume All Sync" if is_globally_paused else "⏸️ Pause All Sync")

            # Update stats
            stats = status.get("stats", {})
            stat_deliv_val.set(str(stats.get("delivered", 0)))
            outbox_keys = list(status.get("outbox", []))
            active_queued = len(outbox_keys)
            stat_queue_val.set(str(active_queued))
            stat_queue_label.config(fg=WARNING if active_queued > 0 else TEXT_MUTED)
            if active_queued > 0:
                targets = []
                for k in outbox_keys:
                    _, _, ident = k.partition(":")
                    target_name = ident.capitalize() if ident else k
                    if target_name.lower() == "phone":
                        target_name = "S24"
                    elif target_name.lower() == "tab":
                        target_name = "Tab S10"
                    elif target_name.lower() == "pixel":
                        target_name = "Pixel"
                    targets.append(target_name)
                targets_str = ", ".join(targets)
                stat_queue_sublabel_val.set(f"Queued ({targets_str})")
            else:
                stat_queue_sublabel_val.set("Outbox Queued")


            # Count online nodes
            online_count = sum(1 for n, inf in status.get("nodes", {}).items() if inf.get("available") and n != "laptop")
            total_devs = len(status.get("nodes", {})) - (1 if "laptop" in status.get("nodes", {}) else 0)
            stat_dev_val.set(f"{online_count}/{total_devs} Online")

            if is_globally_paused:
                status_dot.itemconfig(status_dot_oval, fill=PAUSED)
                global_status_text.set("Mesh Paused")
            elif online_count == total_devs and total_devs > 0:
                status_dot.itemconfig(status_dot_oval, fill=SUCCESS)
                global_status_text.set("All Online")
            elif online_count > 0:
                status_dot.itemconfig(status_dot_oval, fill=WARNING)
                global_status_text.set(f"{online_count} Online")
            else:
                status_dot.itemconfig(status_dot_oval, fill=ERROR)
                global_status_text.set("Connecting...")

            # Update node cards
            for name, info in status["nodes"].items():
                if name == "laptop":
                    continue
                if name not in device_rows:
                    device_rows[name] = create_modern_device_tile(name, is_domain=False)
                    _bind_mousewheel(device_rows[name]["tile"], status_canvas)
                row = device_rows[name]

                is_paused = info.get("paused", False) or is_globally_paused
                if is_paused:
                    color = PAUSED
                    bg_color = PAUSED_BG
                    text = "Paused"
                elif info["available"]:
                    color = SUCCESS_TEXT
                    bg_color = SUCCESS_BG
                    text = "Connected"
                else:
                    color = ERROR_TEXT
                    bg_color = ERROR_BG
                    text = "Offline"

                if info.get("locked"):
                    text += " · Locked"

                row["dot_canvas"].itemconfig(row["dot_oval"], fill=color)
                row["status_text"].set(text)
                row["status_lbl"].configure(fg=color, bg=bg_color)
                row["badge_frame"].configure(bg=bg_color)
                row["dot_canvas"].configure(bg=bg_color)

                desc_text = "Tailscale Mesh Node · ADB Transport"
                row["desc_lbl"].configure(text=desc_text)

                if row.get("btn_var"):
                    row["btn_var"].set("Resume" if is_paused else "Pause")

            for domain in status.get("domains", []):
                key = f"domain:{domain['domain']}"
                if key not in device_rows:
                    device_rows[key] = create_modern_device_tile(f"{domain['domain']} (Continuity)", is_domain=True)
                    _bind_mousewheel(device_rows[key]["tile"], status_canvas)
                row = device_rows[key]
                healthy = domain.get("healthy", True)
                color = SUCCESS_TEXT if healthy else WARNING_TEXT
                bg_color = SUCCESS_BG if healthy else WARNING_BG
                row["dot_canvas"].itemconfig(row["dot_oval"], fill=color)
                row["status_text"].set("Healthy" if healthy else "Degraded")
                row["status_lbl"].configure(fg=color, bg=bg_color)
                row["badge_frame"].configure(bg=bg_color)
                row["dot_canvas"].configure(bg=bg_color)
                row["desc_lbl"].configure(
                    text=f"Sync Domain · Representative: {domain.get('representative', 'unknown')}"
                )

            root.after(1500, refresh)

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


def _open_device_dialog(parent: tk.Tk, existing: dict | None, on_save_callback) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title("Edit Mesh Device" if existing else "Add Mesh Device")
    dlg.configure(bg=BG)
    dlg.geometry("440x360")
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()
    _apply_win11_attributes(dlg)

    frame = tk.Frame(dlg, bg=BG, padx=24, pady=20)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="Device Identifier (e.g. phone, tab, pixel):", bg=BG, fg=TEXT,
             font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(0, 2))
    name_entry = tk.Entry(frame, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                          font=(FONT_FAMILY, 9), relief="flat",
                          highlightthickness=1, highlightbackground=SURFACE_BORDER)
    name_entry.pack(fill="x", pady=(0, 10), ipady=3)
    if existing:
        name_entry.insert(0, existing.get("name", ""))

    tk.Label(frame, text="Network Address (Tailscale IP:5555):", bg=BG, fg=TEXT,
             font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(0, 2))
    addr_entry = tk.Entry(frame, bg=SURFACE_CARD, fg=TEXT, insertbackground=TEXT,
                          font=(FONT_FAMILY, 9), relief="flat",
                          highlightthickness=1, highlightbackground=SURFACE_BORDER)
    addr_entry.pack(fill="x", pady=(0, 10), ipady=3)
    if existing:
        addr_entry.insert(0, existing.get("device", ""))

    tk.Label(frame, text="Transport Protocol:", bg=BG, fg=TEXT,
             font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(0, 2))
    transport_combo = ttk.Combobox(frame, values=["adb", "tcp"], state="readonly")
    transport_combo.pack(fill="x", pady=(0, 10))
    transport_combo.set(existing.get("transport", "adb") if existing else "adb")

    enabled_var = tk.BooleanVar(value=existing.get("enabled", True) if existing else True)
    tk.Checkbutton(frame, text="Device Enabled in Mesh", variable=enabled_var,
                   bg=BG, fg=TEXT, selectcolor=SURFACE_CARD,
                   activebackground=BG, activeforeground=TEXT,
                   font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 14))

    def on_submit():
        name = name_entry.get().strip()
        addr = addr_entry.get().strip()
        trans = transport_combo.get().strip()
        if not name or not addr:
            messagebox.showerror("Error", "Name and Address are required.", parent=dlg)
            return

        res = {
            "name": name,
            "transport": trans,
            "device": addr,
            "enabled": enabled_var.get(),
        }
        on_save_callback(res)
        dlg.destroy()

    btn_frame = tk.Frame(frame, bg=BG)
    btn_frame.pack(fill="x", pady=(6, 0))

    cancel_btn = tk.Button(btn_frame, text="Cancel", command=dlg.destroy,
                           bg=SURFACE, fg=TEXT_MUTED, relief="flat", padx=14, pady=6,
                           highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
    cancel_btn.pack(side="right", padx=(8, 0))
    _add_hover_effect(cancel_btn, SURFACE, SURFACE_HOVER)

    save_dev_btn = tk.Button(btn_frame, text="Save Device", command=on_submit,
                             bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOVER,
                             font=(FONT_FAMILY, 9, "bold"), relief="flat", padx=16, pady=6, cursor="hand2")
    save_dev_btn.pack(side="right")
    _add_hover_effect(save_dev_btn, ACCENT, ACCENT_HOVER, "#ffffff", "#ffffff")


def _open_log_window(parent: tk.Tk) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title("CopyClip Live System Logs")
    dlg.configure(bg=BG)
    dlg.geometry("740x560")
    dlg.minsize(480, 360)
    _apply_win11_attributes(dlg)

    frame = tk.Frame(dlg, bg=BG, padx=16, pady=14)
    frame.pack(fill="both", expand=True)

    text_frame = tk.Frame(frame, bg=SURFACE, highlightbackground=SURFACE_BORDER, highlightthickness=1)
    text_frame.pack(fill="both", expand=True)

    text_widget = tk.Text(text_frame, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                          font=(FONT_MONO, 9), wrap="none", borderwidth=0, padx=10, pady=10)
    scrollbar_y = ttk.Scrollbar(text_frame, orient="vertical", command=text_widget.yview)
    scrollbar_x = ttk.Scrollbar(text_frame, orient="horizontal", command=text_widget.xview)
    text_widget.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

    scrollbar_y.pack(side="right", fill="y")
    scrollbar_x.pack(side="bottom", fill="x")
    text_widget.pack(side="left", fill="both", expand=True)

    last_size = [0]
    auto_scroll_var = tk.BooleanVar(value=True)
    is_active = [True]

    def load_logs(full: bool = False):
        if not os.path.isfile(LOG_PATH):
            text_widget.delete("1.0", "end")
            text_widget.insert("end", "Log file not found.")
            return

        try:
            curr_size = os.path.getsize(LOG_PATH)
            if full or curr_size < last_size[0]:
                text_widget.delete("1.0", "end")
                with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    tail = lines[-400:] if len(lines) > 400 else lines
                    text_widget.insert("end", "".join(tail))
                last_size[0] = curr_size
                if auto_scroll_var.get():
                    text_widget.see("end")
            elif curr_size > last_size[0]:
                with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size[0])
                    chunk = f.read()
                    text_widget.insert("end", chunk)
                last_size[0] = curr_size
                if auto_scroll_var.get():
                    text_widget.see("end")
        except Exception as e:
            text_widget.insert("end", f"\nError reading log file: {e}")

    load_logs(full=True)

    # Auto-tail loop every 1.2 seconds while window exists
    def _auto_tail():
        if not is_active[0]:
            return
        try:
            if dlg.winfo_exists():
                load_logs(full=False)
                dlg.after(1200, _auto_tail)
        except Exception:
            pass

    dlg.after(1200, _auto_tail)

    def _on_dlg_close():
        is_active[0] = False
        dlg.destroy()

    dlg.protocol("WM_DELETE_WINDOW", _on_dlg_close)

    btn_frame = tk.Frame(frame, bg=BG)
    btn_frame.pack(fill="x", pady=(12, 0))

    ref_btn = tk.Button(btn_frame, text="🔄 Refresh", command=lambda: load_logs(full=True),
                        bg=SURFACE, fg=TEXT, activebackground=SURFACE_HOVER,
                        font=(FONT_FAMILY, 9), relief="flat", padx=12, pady=6,
                        highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
    ref_btn.pack(side="left")
    _add_hover_effect(ref_btn, SURFACE, SURFACE_HOVER)

    def clear_logs_action():
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
            last_size[0] = 0
            text_widget.delete("1.0", "end")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            text_widget.insert("end", f"[{ts}] --- Log cleared. Listening for fresh incoming events... ---\n\n")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear logs: {e}", parent=dlg)

    clear_btn = tk.Button(btn_frame, text="🗑️ Clear Logs", command=clear_logs_action,
                          bg=SURFACE, fg=ERROR_TEXT, activebackground=SURFACE_HOVER, activeforeground=TEXT,
                          font=(FONT_FAMILY, 9, "bold"), relief="flat", padx=12, pady=6,
                          highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
    clear_btn.pack(side="left", padx=8)
    _add_hover_effect(clear_btn, SURFACE, SURFACE_HOVER)

    def open_log_folder():
        log_dir = os.path.dirname(LOG_PATH)
        if os.path.isdir(log_dir):
            os.startfile(log_dir)

    folder_btn = tk.Button(btn_frame, text="📂 Open Folder", command=open_log_folder,
                           bg=SURFACE, fg=TEXT_MUTED, activebackground=SURFACE_HOVER,
                           font=(FONT_FAMILY, 9), relief="flat", padx=12, pady=6,
                           highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
    folder_btn.pack(side="left", padx=(0, 8))
    _add_hover_effect(folder_btn, SURFACE, SURFACE_HOVER)

    chk_autoscroll = tk.Checkbutton(btn_frame, text="Auto-scroll", variable=auto_scroll_var,
                                    bg=BG, fg=TEXT_MUTED, selectcolor=SURFACE_CARD,
                                    activebackground=BG, activeforeground=TEXT, font=(FONT_FAMILY, 8))
    chk_autoscroll.pack(side="left", padx=4)

    close_btn = tk.Button(btn_frame, text="Close", command=_on_dlg_close,
                          bg=SURFACE, fg=TEXT, activebackground=SURFACE_HOVER,
                          font=(FONT_FAMILY, 9), relief="flat", padx=14, pady=6,
                          highlightthickness=1, highlightbackground=SURFACE_BORDER, cursor="hand2")
    close_btn.pack(side="right")
    _add_hover_effect(close_btn, SURFACE, SURFACE_HOVER)
