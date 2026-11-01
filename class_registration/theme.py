"""
theme.py - shared look & feel for the Class Registration GUIs.

Both the student client (client_gui.py) and the server console (server_gui.py)
import this module so the two programs look like parts of one product.

Pure standard library (tkinter / tkinter.ttk only).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ----------------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------------
BG = "#eef1f8"          # window background
CARD = "#ffffff"        # panel background
INK = "#16202e"         # primary text
MUTED = "#66748a"       # secondary text
LINE = "#d8dfec"        # borders / separators
PRIMARY = "#3355d1"     # brand blue
PRIMARY_DARK = "#2743a8"
PRIMARY_SOFT = "#e6ebfb"
OK = "#0f9d6e"
OK_SOFT = "#e2f7ef"
WARN = "#c77700"
WARN_SOFT = "#fff3df"
DANGER = "#d43a3a"
DANGER_SOFT = "#fdeaea"
HEADER = "#1e2a4a"      # dark header bar
HEADER_SUB = "#9fb0d4"

FONT = "DejaVu Sans"     # falls back gracefully on macOS / Windows


def _pick_font(root: tk.Misc) -> str:
    """Choose a clean sans-serif that exists on this machine."""
    available = set(tkfont.families(root))
    for name in ("Segoe UI", "SF Pro Text", "DejaVu Sans", "Noto Sans", "Arial", "Helvetica"):
        if name in available:
            return name
    return "TkDefaultFont"


def apply(root: tk.Misc) -> str:
    """Configure ttk styles and return the chosen font family."""
    fam = _pick_font(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(background=BG)
    style.configure(".", font=(fam, 10), background=BG, foreground=INK)

    # --- containers -----------------------------------------------------
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Header.TFrame", background=HEADER)
    style.configure("Soft.TFrame", background=PRIMARY_SOFT)

    # --- labels ---------------------------------------------------------
    style.configure("TLabel", background=BG, foreground=INK, font=(fam, 10))
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(fam, 9))
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(fam, 9))
    style.configure("H1.TLabel", background=HEADER, foreground="#ffffff", font=(fam, 16, "bold"))
    style.configure("H2.TLabel", background=BG, foreground=INK, font=(fam, 13, "bold"))
    style.configure("CardH2.TLabel", background=CARD, foreground=INK, font=(fam, 12, "bold"))
    style.configure("HeaderSub.TLabel", background=HEADER, foreground=HEADER_SUB, font=(fam, 9))
    style.configure("FieldLabel.TLabel", background=CARD, foreground=MUTED, font=(fam, 9, "bold"))
    style.configure("Badge.TLabel", background=PRIMARY_SOFT, foreground=PRIMARY_DARK,
                    font=(fam, 9, "bold"), padding=(10, 4))

    # --- inputs ---------------------------------------------------------
    style.configure("TEntry", fieldbackground="#ffffff", bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, insertcolor=INK,
                    padding=6, relief="flat")
    style.map("TEntry", bordercolor=[("focus", PRIMARY)],
              lightcolor=[("focus", PRIMARY)], darkcolor=[("focus", PRIMARY)])
    style.configure("TCombobox", fieldbackground="#ffffff", bordercolor=LINE,
                    arrowcolor=MUTED, padding=5)
    style.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=LINE, padding=5)

    # --- buttons --------------------------------------------------------
    style.configure("TButton", font=(fam, 10), padding=(14, 7), relief="flat",
                    background="#e7ebf5", foreground=INK, bordercolor=LINE,
                    lightcolor="#e7ebf5", darkcolor="#e7ebf5", focuscolor=BG)
    style.map("TButton",
              background=[("active", "#dbe1ef"), ("disabled", "#f1f3f8")],
              foreground=[("disabled", MUTED)])

    style.configure("Primary.TButton", font=(fam, 10, "bold"), padding=(16, 9),
                    background=PRIMARY, foreground="#ffffff", bordercolor=PRIMARY,
                    lightcolor=PRIMARY, darkcolor=PRIMARY, focuscolor=PRIMARY)
    style.map("Primary.TButton",
              background=[("active", PRIMARY_DARK), ("disabled", "#b9c4e8")],
              lightcolor=[("active", PRIMARY_DARK)], darkcolor=[("active", PRIMARY_DARK)],
              foreground=[("disabled", "#f0f2fa")])

    style.configure("Ghost.TButton", background=CARD, foreground=PRIMARY_DARK,
                    bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, padding=(12, 6))
    style.map("Ghost.TButton", background=[("active", PRIMARY_SOFT)],
              lightcolor=[("active", PRIMARY_SOFT)], darkcolor=[("active", PRIMARY_SOFT)])

    style.configure("Danger.TButton", background=CARD, foreground=DANGER,
                    bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, padding=(12, 6))
    style.map("Danger.TButton", background=[("active", DANGER_SOFT)],
              lightcolor=[("active", DANGER_SOFT)], darkcolor=[("active", DANGER_SOFT)])

    # --- notebook -------------------------------------------------------
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", font=(fam, 10, "bold"), padding=(16, 8),
                    background="#e2e7f2", foreground=MUTED, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", PRIMARY_DARK)],
              expand=[("selected", (0, 0, 0, 0))])

    style.configure("Card.TCheckbutton", background=CARD, foreground=INK,
                    font=(fam, 9), focuscolor=CARD)
    style.map("Card.TCheckbutton", background=[("active", CARD)],
              indicatorcolor=[("selected", PRIMARY), ("!selected", "#ffffff")])

    # --- treeview -------------------------------------------------------
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=INK, rowheight=27, font=(fam, 10), borderwidth=0)
    style.configure("Treeview.Heading", font=(fam, 9, "bold"), background="#e2e7f2",
                    foreground=MUTED, relief="flat", padding=(8, 7))
    style.map("Treeview.Heading", background=[("active", "#d5dcec")])
    style.map("Treeview", background=[("selected", PRIMARY_SOFT)],
              foreground=[("selected", PRIMARY_DARK)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # --- misc -----------------------------------------------------------
    style.configure("TSeparator", background=LINE)
    style.configure("TProgressbar", background=PRIMARY, troughcolor="#e2e7f2",
                    borderwidth=0, thickness=8)
    style.configure("TPanedwindow", background=BG)
    return fam


def card(parent: tk.Misc, **kw) -> tk.Frame:
    """A white panel with a 1px border and inner padding."""
    outer = tk.Frame(parent, background=LINE, bd=0, highlightthickness=0, **kw)
    inner = tk.Frame(outer, background=CARD, bd=0, highlightthickness=0)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    outer.inner = inner  # type: ignore[attr-defined]
    return outer


def status_dot(parent: tk.Misc, colour: str = MUTED, bg: str = CARD) -> tk.Canvas:
    """A little round indicator light."""
    c = tk.Canvas(parent, width=14, height=14, highlightthickness=0, bd=0, bg=bg)
    c.create_oval(3, 3, 11, 11, fill=colour, outline=colour, tags="dot")
    c.set_colour = lambda col: (c.itemconfigure("dot", fill=col, outline=col))  # type: ignore
    return c
