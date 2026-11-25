#!/usr/bin/env python3
"""
server_gui.py - Class Registration System: server side console (version 2).

============================================================================
WHAT THIS FILE IS
============================================================================

The staff-facing desktop console.  It does not implement any rules of its own:
it starts the API from `server.py` in a background thread and then *shows* that
server's state, so whatever the console displays is exactly what clients get.

    python3 server_gui.py                  # API on http://0.0.0.0:8000
    python3 server_gui.py --port 9000 --demo

============================================================================
THE FOUR TABS
============================================================================

Overview       live counters plus a fill bar per section of the chosen term
Course Sheet   the full sheet, with the term selector and "Reload from CSV"
Registrations  every registration received: search, CSV export, cancel
Students       the student dictionary (one row per student, no duplicates)
Activity Log   the API's request log

A term selector in the header applies to the Overview, the Course Sheet and the
Registrations tab at the same time, mirroring the client's selector.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports.
#
# Tkinter is the only GUI toolkit used: ttk for themed widgets (tables, combo
# boxes, progress bars) and plain tk for coloured containers and the canvas,
# because plain tk widgets accept a background colour everywhere.
# ---------------------------------------------------------------------------
import argparse                     # --host / --port / --demo flags
import csv                          # writing the CSV export
import os                           # paths and file names
import threading                    # the API runs in its own thread
import tkinter as tk                # GUI toolkit
from datetime import datetime       # timestamps for the log
from tkinter import filedialog, messagebox, ttk

import server as srv                # ITEM 3: the API + database this console drives
import theme                        # shared colours and widget styles

# The console only needs the folder for display purposes (the database path is
# shown relative to the project so screenshots stay readable).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def start_api(host: str, port: int, db_path: str, courses: str, students: str,
              log_sink) -> srv.ThreadingHTTPServer:
    """Create the HTTP server and serve it from a daemon thread.

    The console and the API live in one process but in two threads: the GUI
    thread draws windows, this one answers HTTP requests.  `daemon=True` means
    the thread cannot keep the program alive after the window is closed.
    """
    httpd = srv.create_server(host, port, db_path, courses, students,
                              log_callback=log_sink)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True, name="api-server").start()
    return httpd


class ServerConsole(tk.Tk):
    """The whole server window.

    Version 3 additions on the Students tab: a "New student ..." button that
    opens a modal form (requirement 2) and Allow / Block registration buttons
    for the selected account (requirement 3).  Both call the same functions of
    CourseDB that the HTTP API uses, so the console cannot implement a rule
    differently from the server.

    Responsibilities, in order of importance:

        1. start / stop the API from server.py
        2. show its state (counts, sheet, registrations, students, log)
        3. let staff act on it (cancel a registration, export, reload the CSV)

    Note what is *not* here: no rule is implemented in this file.  Capacity,
    duplicates, the one-session rule and the clash check all live in
    server.py, so the console cannot drift away from what clients experience.
    """

    def __init__(self, host: str, port: int, demo: bool = False):
        super().__init__()
        # theme.apply() installs all ttk styles and returns the font family
        # that the plain tk widgets should use; keeping the name in self.family
        # avoids repeating the lookup in every widget constructor.
        self.family = theme.apply(self)
        self.title("Class Registration - Server Console")
        self.geometry("1300x840")
        self.minsize(1120, 720)

        # ---- state ----
        # host/port are kept so the power button can restart the API with the
        # same settings after a stop.
        self.host, self.port = host, port
        self.log_lines: list[str] = []      # buffer drained by _pump_log
        self.httpd: srv.ThreadingHTTPServer | None = None
        self.db: srv.CourseDB | None = None
        # The term filter: 0/0 means "all terms", which is what the console
        # starts with so staff see everything before narrowing down.
        self.year = 0                        # selected term (0 = all terms)
        self.term = 0
        self.terms: list[dict] = []

        self._build_header()
        self._build_notebook()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start the API shortly after the window appears, so any bind error can
        # be shown in a normal dialog instead of a crash during construction.
        self.after(200, lambda: self._start(host, port, demo))
        self.after(500, self._pump_log)       # keep moving log lines into the Text
        self.after(1500, self._auto_refresh)  # refresh the visible numbers

    # ------------------------------------------------------------------
    # Header: title, endpoint, run state, term selector, start/stop button
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        # A fixed-height dark bar across the top.  pack_propagate(False) stops
        # the children from shrinking it to their own height.
        bar = tk.Frame(self, background=theme.HEADER, height=92)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, background=theme.HEADER)
        left.pack(side="left", padx=22, pady=14)
        tk.Label(left, text="Registration Server", background=theme.HEADER,
                 foreground="#ffffff", font=(self.family, 17, "bold")).pack(anchor="w")
        self.lbl_endpoint = tk.Label(left, text="starting ...", background=theme.HEADER,
                                     foreground=theme.HEADER_SUB, font=(self.family, 9))
        self.lbl_endpoint.pack(anchor="w")

        right = tk.Frame(bar, background=theme.HEADER)
        right.pack(side="right", padx=22)

        # ---- term selector (same idea as the client's) ----
        # Index 0 is a synthetic "All terms" entry, so the console can show the
        # whole database as well as one term at a time.
        tk.Label(right, text="TERM / YEAR", background=theme.HEADER,
                 foreground=theme.HEADER_SUB, font=(self.family, 8, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        self.var_term = tk.StringVar(value="all terms")
        self.cmb_term = ttk.Combobox(right, textvariable=self.var_term,
                                     state="readonly", width=24,
                                     font=(self.family, 10))
        self.cmb_term.grid(row=1, column=0, sticky="w")
        self.cmb_term.bind("<<ComboboxSelected>>", self._on_term_change)

        # ---- run state light and the start/stop button ----
        # Green = the API is answering, red = it was stopped or could not bind,
        # amber = still starting.
        state = tk.Frame(bar, background=theme.HEADER)
        state.pack(side="right", padx=(0, 6))
        self.dot = theme.status_dot(state, theme.WARN, bg=theme.HEADER)
        self.dot.pack(side="left", padx=(0, 8))
        self.lbl_state = tk.Label(state, text="starting", background=theme.HEADER,
                                  foreground="#ffd9a0", font=(self.family, 10, "bold"))
        self.lbl_state.pack(side="left")
        self.btn_power = tk.Button(state, text="Stop server", relief="flat", bd=0,
                                   background="#2c3a61", foreground="#ffffff",
                                   activebackground="#43548a", activeforeground="#ffffff",
                                   font=(self.family, 9, "bold"), padx=14, pady=6,
                                   command=self._toggle_server, cursor="hand2")
        self.btn_power.pack(side="left", padx=(14, 0))

    # ------------------------------------------------------------------
    # Notebook with the four tabs
    # ------------------------------------------------------------------
    def _build_notebook(self) -> None:
        # One tab per question staff ask.  The tabs are built once here and only
        # refreshed afterwards, so switching tabs is instant.
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(14, 8))
        self.tab_over = ttk.Frame(self.nb)
        self.tab_sheet = ttk.Frame(self.nb)
        self.tab_regs = ttk.Frame(self.nb)
        self.tab_students = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_over, text="Overview")
        self.nb.add(self.tab_sheet, text="Course Sheet")
        self.nb.add(self.tab_regs, text="Registrations")
        self.nb.add(self.tab_students, text="Students")
        self.nb.add(self.tab_log, text="Activity Log")
        self._build_overview()
        self._build_sheet()
        self._build_regs()
        self._build_students()
        self._build_log()

    # ---- Overview --------------------------------------------------------
    # The Overview answers the two questions staff ask most often: "how many
    # places are taken in this term" and "which sections are nearly full".
    def _build_overview(self) -> None:
        wrap = tk.Frame(self.tab_over, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)

        # Four counter cards across the top: registrations, students, seats
        # filled and full sections.  The colours match the fill-bar colours used
        # further down, so a red card and red bars mean the same thing.
        cards = tk.Frame(wrap, background=theme.BG)
        cards.pack(fill="x")
        self.stat_labels: dict[str, tk.Label] = {}
        specs = (("registrations", "Registrations", theme.PRIMARY),
                 ("students", "Unique students", "#7a4fd1"),
                 ("filled", "Places filled", theme.OK),
                 ("full", "Full sections", theme.DANGER))
        for index, (key, title, colour) in enumerate(specs):
            # `uniform="stat"` keeps all four cards exactly the same width,
            # even when one of the numbers is much longer than the others.
            cards.columnconfigure(index, weight=1, uniform="stat")
            outer = theme.card(cards)
            outer.grid(row=0, column=index, sticky="ew",
                       padx=(0 if index == 0 else 12, 0))
            inner = outer.inner
            tk.Frame(inner, background=colour, height=4).pack(fill="x")
            tk.Label(inner, text=title, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.family, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
            label = tk.Label(inner, text="0", background=theme.CARD,
                             foreground=theme.INK, font=(self.family, 24, "bold"))
            label.pack(anchor="w", padx=16, pady=(0, 14))
            self.stat_labels[key] = label

        # Fill bars, one per section of the selected term.  A bar is read much
        # faster than a table when the question is "which classes are filling
        # up", so the overview uses bars and the Course Sheet tab keeps the
        # exact numbers.
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True, pady=(14, 0))
        inner = outer.inner
        head = tk.Frame(inner, background=theme.CARD)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="Seats filled by section", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 12, "bold")).pack(side="left")
        self.lbl_over_scope = tk.Label(head, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_over_scope.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_all).pack(side="right")

        # A scrollable canvas: the number of sections can be large.
        # A Canvas is used as a scroll container: a Tkinter Frame cannot scroll
        # by itself, so an inner frame is hosted on the canvas and the canvas
        # viewport is moved instead.
        canvas = tk.Canvas(inner, background=theme.CARD, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True, padx=18, pady=(0, 14))
        self.bars_frame = tk.Frame(canvas, background=theme.CARD)
        canvas.create_window((0, 0), window=self.bars_frame, anchor="nw", tags="bars")
        # Keep the inner frame as wide as the canvas, and the scroll region as
        # tall as its contents, whenever either changes.
        self.bars_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda event: canvas.itemconfigure("bars", width=event.width))
        canvas.bind_all("<MouseWheel>",
                        lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.bars_canvas = canvas

    # ---- Course Sheet ----------------------------------------------------
    # Shows the sheet exactly as the server loaded it, one row per section, so
    # repeated codes (different sessions of one course) are visible rather than
    # hidden behind a count.
    def _build_sheet(self) -> None:
        wrap = tk.Frame(self.tab_sheet, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True)
        inner = outer.inner
        inner.rowconfigure(1, weight=1)
        inner.columnconfigure(0, weight=1)

        head = tk.Frame(inner, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=14)
        tk.Label(head, text="Course Sheet  (source: courses.csv)",
                 background=theme.CARD, foreground=theme.INK,
                 font=(self.family, 12, "bold")).pack(side="left")
        self.lbl_sheet_count = tk.Label(head, text="", background=theme.CARD,
                                        foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_sheet_count.pack(side="right", padx=(0, 12))
        ttk.Button(head, text="Reload from CSV", style="Ghost.TButton",
                   command=self.reload_courses).pack(side="right")

        # One row per SECTION - a course code may repeat here, which is exactly
        # how several sessions of the same course are represented.  The Section
        # ID column (2026-T1-CS101-A) is the key the API expects, so staff can
        # copy it into a curl command if they need to.
        columns = ("section_id", "code", "section", "title", "instructor",
                   "dept", "time", "venue", "cap", "enr")
        self.tree_sheet = ttk.Treeview(inner, columns=columns, show="headings")
        spec = (("section_id", "Section ID", 118, "w"), ("code", "Code", 60, "w"),
                ("section", "Sec", 40, "center"), ("title", "Course title", 168, "w"),
                ("instructor", "Instructor", 132, "w"), ("dept", "Department", 118, "w"),
                ("time", "Time", 138, "w"), ("venue", "Venue", 132, "w"),
                ("cap", "Limit", 50, "center"), ("enr", "Enrolled", 82, "center"))
        for key, text, width, anchor in spec:
            self.tree_sheet.heading(key, text=text)
            self.tree_sheet.column(key, width=width, minwidth=42, anchor=anchor,
                                   stretch=(key == "title"))
        # Row tags: grey text for a full section, amber for "almost full".
        # The 'group' tag shades alternating blocks so repeated sessions of one
        # course read as a group instead of looking like duplicate rows.
        self.tree_sheet.tag_configure("full", foreground="#9aa5b8")
        self.tree_sheet.tag_configure("low", foreground=theme.WARN)
        # Alternating shading per course code makes repeated sessions obvious.
        self.tree_sheet.tag_configure("group", background="#f6f8fd")
        self.tree_sheet.grid(row=1, column=0, sticky="nsew", padx=(18, 0), pady=(0, 16))
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.tree_sheet.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 18), pady=(0, 16))
        self.tree_sheet.configure(yscrollcommand=scroll.set)

    # ---- Registrations ---------------------------------------------------
    # Every registration the API has stored for the selected term.  This is the
    # "server side retrieval" requirement made visible, with search, CSV export
    # and a cancel action.
    def _build_regs(self) -> None:
        wrap = tk.Frame(self.tab_regs, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True)
        inner = outer.inner
        inner.rowconfigure(3, weight=1)
        inner.columnconfigure(0, weight=1)

        tk.Label(inner, text="Registrations received", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 2))
        tk.Label(inner, text="Everything the API has stored for the selected term. "
                             "Search, export to CSV, or cancel a single registration.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9)).grid(row=1, column=0, columnspan=2,
                                             sticky="w", padx=18, pady=(0, 8))

        # Toolbar on its own row so nothing can be clipped by the table; this
        # was a real bug in version 1 when the window was narrow.
        tools = tk.Frame(inner, background=theme.CARD)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10))
        tk.Label(tools, text="Search", background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9, "bold")).pack(side="left", padx=(0, 8))
        self.var_reg_search = tk.StringVar()
        self.var_reg_search.trace_add("write", lambda *_: self.refresh_regs())
        ttk.Entry(tools, textvariable=self.var_reg_search, width=26).pack(side="left")
        ttk.Button(tools, text="Export CSV", style="Ghost.TButton",
                   command=self.export_csv).pack(side="right", padx=(8, 0))
        ttk.Button(tools, text="Cancel selected", style="Danger.TButton",
                   command=self.cancel_selected).pack(side="right", padx=(8, 0))
        ttk.Button(tools, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_regs).pack(side="right")

        # The registration id is the row iid (see refresh_regs) so cancelling a
        # row never has to search for the record again.
        # The "term" column matters now that one database holds several terms.
        columns = ("id", "term", "label", "title", "sid", "name", "email", "when")
        self.tree_regs = ttk.Treeview(inner, columns=columns, show="headings")
        spec = (("id", "#", 44, "center"), ("term", "Term", 96, "w"),
                ("label", "Section", 82, "w"), ("title", "Title", 172, "w"),
                ("sid", "Student ID", 92, "w"), ("name", "Name", 126, "w"),
                ("email", "E-mail", 168, "w"), ("when", "Class time", 128, "w"))
        for key, text, width, anchor in spec:
            self.tree_regs.heading(key, text=text)
            self.tree_regs.column(key, width=width, minwidth=44, anchor=anchor,
                                  stretch=(key == "title"))
        self.tree_regs.grid(row=3, column=0, sticky="nsew", padx=(18, 0))
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.tree_regs.yview)
        scroll.grid(row=3, column=1, sticky="ns", padx=(0, 18))
        self.tree_regs.configure(yscrollcommand=scroll.set)

        self.lbl_regs_count = tk.Label(inner, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_regs_count.grid(row=4, column=0, columnspan=2, sticky="w",
                                 padx=18, pady=(8, 14))

    # ---- Students --------------------------------------------------------
    # The student dictionary as a table: one row per student id.  If details
    # were stored per registration, this list would contain duplicates.
    def _build_students(self) -> None:
        wrap = tk.Frame(self.tab_students, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True)
        inner = outer.inner
        inner.rowconfigure(3, weight=1)
        inner.columnconfigure(0, weight=1)

        head = tk.Frame(inner, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        tk.Label(head, text="Student dictionary", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 12, "bold")).pack(side="left")
        self.lbl_students_count = tk.Label(head, text="", background=theme.CARD,
                                           foreground=theme.MUTED,
                                           font=(self.family, 9))
        self.lbl_students_count.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_students).pack(side="right")

        tk.Label(inner, text="One row per student id (server side dictionary). "
                             "The first registration creates the record; later ones "
                             "reuse it, so details are never stored twice.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9)).grid(row=1, column=0, columnspan=2,
                                             sticky="w", padx=18, pady=(0, 6))

        # Account actions: add a student (requirement 2) and switch a student's
        # ability to register on or off (requirement 3).
        tools = tk.Frame(inner, background=theme.CARD)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 8))
        ttk.Button(tools, text="New student ...", style="Primary.TButton",
                   command=self.open_add_student).pack(side="left")
        self.btn_allow = ttk.Button(tools, text="Allow registration",
                                    style="Ghost.TButton", state="disabled",
                                    command=lambda: self.set_student_access(True))
        self.btn_allow.pack(side="left", padx=(8, 0))
        self.btn_block = ttk.Button(tools, text="Block registration",
                                    style="Danger.TButton", state="disabled",
                                    command=lambda: self.set_student_access(False))
        self.btn_block.pack(side="left", padx=(8, 0))
        self.lbl_student_action = tk.Label(tools, text="", background=theme.CARD,
                                           foreground=theme.MUTED,
                                           font=(self.family, 9))
        self.lbl_student_action.pack(side="right")

        # "regs" is the number of registrations that point at this one student
        # record - visible proof that details are stored once, not per course.
        # "Access" is the requirement 3 switch; "Classes" proves the student
        # dictionary stores one row per person rather than one per registration.
        columns = ("sid", "name", "email", "role", "access", "since", "regs")
        self.tree_students = ttk.Treeview(inner, columns=columns, show="headings")
        for key, text, width, anchor in (("sid", "Student ID", 100, "w"),
                                         ("name", "Name", 190, "w"),
                                         ("email", "E-mail", 240, "w"),
                                         ("role", "Role", 80, "w"),
                                         ("access", "Registration", 110, "w"),
                                         ("since", "First seen", 140, "w"),
                                         ("regs", "Classes", 70, "center")):
            self.tree_students.heading(key, text=text)
            self.tree_students.column(key, width=width, minwidth=60, anchor=anchor,
                                      stretch=(key in ("name", "email")))
        # Blocked accounts are painted red so a suspended student is obvious.
        self.tree_students.tag_configure("blocked", foreground=theme.DANGER)
        self.tree_students.bind("<<TreeviewSelect>>", self._on_student_select)
        self.tree_students.grid(row=3, column=0, sticky="nsew", padx=(18, 0))
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.tree_students.yview)
        scroll.grid(row=3, column=1, sticky="ns", padx=(0, 18))
        self.tree_students.configure(yscrollcommand=scroll.set)
        tk.Label(inner, text="", background=theme.CARD).grid(row=4, column=0, pady=10)

    # ---- Activity log ----------------------------------------------------
    # The API's own request log, the same lines that `python3 server.py` would
    # print in a terminal.  Useful when a client reports "it did not work".
    def _build_log(self) -> None:
        # A Text widget rather than a Listbox: new lines can be appended and the
        # view scrolled to the end, which is what a log tail has to do.
        wrap = tk.Frame(self.tab_log, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True)
        inner = outer.inner
        inner.rowconfigure(1, weight=1)
        inner.columnconfigure(0, weight=1)

        head = tk.Frame(inner, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=12)
        tk.Label(head, text="Activity log", background=theme.CARD, foreground=theme.INK,
                 font=(self.family, 12, "bold")).pack(side="left")
        ttk.Button(head, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")

        # The log is read-only ("disabled") and only switched to "normal"
        # briefly while new lines are inserted, otherwise the user could type
        # into what is supposed to be a transcript.
        self.txt_log = tk.Text(inner, background="#111a2b", foreground="#c8d4ea",
                               insertbackground="#ffffff", relief="flat", bd=0,
                               font=("DejaVu Sans Mono", 9), wrap="none",
                               padx=12, pady=10)
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=(18, 0), pady=(0, 16))
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.txt_log.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 18), pady=(0, 16))
        self.txt_log.configure(yscrollcommand=scroll.set, state="disabled")

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, background="#dfe5f2", height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.var_status = tk.StringVar(value="Starting the API ...")
        tk.Label(bar, textvariable=self.var_status, background="#dfe5f2",
                 foreground=theme.MUTED, font=(self.family, 9), anchor="w").pack(
            side="left", padx=16)
        self.lbl_db = tk.Label(bar, text="", background="#dfe5f2",
                               foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_db.pack(side="right", padx=16)

    def set_status(self, text: str) -> None:
        """Write a line into the status bar."""
        self.var_status.set(text)

    # ------------------------------------------------------------------
    # Start / stop the API
    # ------------------------------------------------------------------
    def _start(self, host: str, port: int, demo: bool) -> None:
        """Bind the API and wire the console to its database."""
        try:
            self.httpd = start_api(host, port, srv.DEFAULT_DB, srv.DEFAULT_COURSES,
                                   srv.DEFAULT_STUDENTS, self._enqueue_log)
        except OSError as err:
            # The port is taken or not permitted - explain and let the user
            # pick another one instead of dying.
            self._set_state("error", "cannot bind port")
            self.set_status(f"Could not start the server: {err}")
            messagebox.showerror(
                "Server did not start",
                f"Port {port} could not be opened.\n\n{err}\n\n"
                "Try another port:  python3 server_gui.py --port 9000")
            return

        # The console and the API share one CourseDB object, so the tabs show
        # exactly the state the API serves - no second copy of the data.
        self.db = self.httpd.db
        self.lbl_endpoint.configure(
            text=f"http://{host}:{port}   -   /api/terms  /api/courses  "
                 f"/api/registrations  /api/students")
        self.lbl_db.configure(text=os.path.relpath(srv.DEFAULT_DB, BASE_DIR))
        self._set_state("running", f"running on port {port}")

        # Report how the CSV was read: the "skipped" counter is the number of
        # repeated section rows that were refused while loading.
        report = self.db.sheet_report
        self._log(f"course sheet: {report['loaded']} sections, "
                  f"{report['skipped']} duplicate rows skipped")
        accounts = self.db.student_report
        self._log(f"student accounts: {accounts['loaded']} loaded from "
                  f"students.csv ({accounts['added']} new, "
                  f"{accounts['skipped']} duplicates skipped)")
        if self.db.admin_account:
            # Loud on purpose: a hard-coded password must never ship unnoticed.
            self._log(f"WARNING dev-only admin account enabled: "
                      f"{self.db.admin_account['student_id']}")
        if demo:
            added = self.db.load_demo_data()
            self._log(f"demo data: {added} registrations inserted")

        # A short line in the status bar tells staff the connection details
        # without opening a terminal.
        self.set_status(f"Server listening on port {port}. Clients can connect now.")
        self._log(f"server started on http://{host}:{port}")
        self.load_terms()
        self.refresh_all()

    def _stop(self) -> None:
        """Shut the API down but keep the window open.

        `shutdown()` stops the serve_forever() loop, `server_close()` releases
        the port.  Both are needed, otherwise restarting on the same port fails
        with "address already in use".
        """
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self._log("server stopped")
        self._set_state("stopped", "stopped")

    def _toggle_server(self) -> None:
        """The header button: stop a running server, or start a stopped one."""
        if self.httpd:
            self._stop()
            self.btn_power.configure(text="Start server")
        else:
            self._start(self.host, self.port, False)
            self.btn_power.configure(text="Stop server")

    def _set_state(self, state: str, text: str) -> None:
        """Colour the run-state light and relabel the power button.

        One method handles every state (running / stopped / error) so the light,
        the caption and the button text can never disagree.
        """
        colours = {"running": (theme.OK, "#8ff0c8", "Stop server"),
                   "stopped": (theme.DANGER, "#ffb4b4", "Start server"),
                   "error": (theme.DANGER, "#ffb4b4", "Start server")}
        dot, foreground, button = colours.get(state,
                                              (theme.WARN, "#ffd9a0", "Start server"))
        self.dot.set_colour(dot)
        self.lbl_state.configure(text=text, foreground=foreground)
        self.btn_power.configure(text=button)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    # Thread-safety note: the API thread only ever appends to `self.log_lines`,
    # a plain list append which is atomic in CPython.  All widget updates happen
    # in _pump_log on the GUI thread.
    def _enqueue_log(self, line: str) -> None:
        """Called from the API thread - must not touch the GUI directly.

        The line is only appended to a list here; `_pump_log` moves buffered
        lines into the Text widget from the GUI thread, which is the only
        thread allowed to change Tkinter widgets.
        """
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {line}")

    def _log(self, text: str) -> None:
        """Add a console-generated line to the same log."""
        self._enqueue_log(f"[console] {text}")

    def _pump_log(self) -> None:
        """Move buffered log lines into the Text widget, twice a second."""
        if self.log_lines:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "\n".join(self.log_lines) + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
            self.log_lines.clear()
        self.after(500, self._pump_log)

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Term selector
    # ------------------------------------------------------------------
    def load_terms(self) -> None:
        """Fill the term selector from the server's own term list."""
        if not self.db:
            return
        self.terms = self.db.terms()
        labels = ["All terms"] + [f"{t['label']}  ({t['sections']} sections)"
                                  for t in self.terms]
        self.cmb_term.configure(values=labels)
        self.cmb_term.current(0)          # the console starts on "All terms"
        self.year = self.term = 0
        self.var_term.set("All terms")

    def _on_term_change(self, _event=None) -> None:
        """Apply the chosen term to every tab at once.

        Index 0 is the "All terms" entry, so the term list is offset by one;
        index 1 therefore means self.terms[0].
        """
        index = self.cmb_term.current()
        if index <= 0:
            self.year = self.term = 0
        elif index - 1 < len(self.terms):
            self.year = self.terms[index - 1]["year"]
            self.term = self.terms[index - 1]["term"]
        scope = "all terms" if not self.year else f"{self.year} Term {self.term}"
        self.set_status(f"Showing {scope}.")
        self.refresh_all()

    def _term_args(self) -> tuple[int | None, int | None]:
        """The (year, term) filter to pass to the database, or (None, None)."""
        return (self.year, self.term) if self.year and self.term else (None, None)

    # ------------------------------------------------------------------
    # Refreshing the screens
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        """Redraw every tab that depends on the current data."""
        if not self.db:
            return
        self.refresh_overview()
        self.refresh_sheet()
        self.refresh_regs()
        self.refresh_students()

    def refresh_overview(self) -> None:
        """Update the counters and the seat bars.

        `known_students` counts the whole dictionary, while `unique_students`
        counts only those registered in the selected term; the difference is
        normal when a term filter is active.
        """
        if not self.db:
            return
        year, term = self._term_args()
        stats = self.db.stats(year, term)
        self.stat_labels["registrations"].configure(
            text=str(stats["total_registrations"]))
        self.stat_labels["students"].configure(
            text=f"{stats['unique_students']}")
        self.stat_labels["filled"].configure(
            text=f"{stats['seats_filled']}/{stats['seats_total']}")
        self.stat_labels["full"].configure(text=str(stats["full_courses"]))
        self.lbl_over_scope.configure(
            text="all terms" if year is None else f"{year} Term {term}")
        self._render_bars(self.db.courses(year, term))

    def _render_bars(self, courses: list[dict]) -> None:
        """Draw one labelled progress bar per section.

        Rebuilding the whole list on every refresh is acceptable here: the
        course sheet has tens of rows, and destroying the children is far
        simpler than trying to reconcile old and new rows.
        """
        for child in self.bars_frame.winfo_children():
            child.destroy()               # simplest correct redraw
        for course in courses:
            row = tk.Frame(self.bars_frame, background=theme.CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=course["label"], background=theme.CARD,
                     foreground=theme.INK, font=(self.family, 9, "bold"),
                     width=10, anchor="w").pack(side="left")
            tk.Label(row, text=f"{course['year']} T{course['term']}",
                     background=theme.CARD, foreground=theme.MUTED,
                     font=(self.family, 8), width=8, anchor="w").pack(side="left")
            tk.Label(row, text=course["title"][:34], background=theme.CARD,
                     foreground=theme.MUTED, font=(self.family, 9), width=34,
                     anchor="w").pack(side="left")
            # A course with capacity 0 would divide by zero, so it is guarded
            # here rather than relying on the CSV being sensible.
            ratio = course["enrolled"] / course["capacity"] if course["capacity"] else 0
            colour = theme.DANGER if course["is_full"] else (
                theme.WARN if ratio >= 0.8 else theme.PRIMARY)
            ttk.Progressbar(row, style="TProgressbar", maximum=100,
                            value=ratio * 100, length=250).pack(side="left", padx=10)
            tk.Label(row, text=f"{course['enrolled']}/{course['capacity']}",
                     background=theme.CARD, foreground=colour,
                     font=(self.family, 9, "bold"), width=8,
                     anchor="e").pack(side="left")

    def refresh_sheet(self) -> None:
        """Redraw the course sheet table for the selected term.

        Rows are re-grouped on every refresh (last_code/group) so the shading
        stays correct after the term or the CSV changes.
        """
        if not self.db:
            return
        year, term = self._term_args()
        courses = self.db.courses(year, term)
        self.tree_sheet.delete(*self.tree_sheet.get_children())
        last_code, group = None, False
        for course in courses:
            # Alternate the background each time the code changes, so repeated
            # sessions of one course are easy to spot.
            if course["code"] != last_code:
                group = not group
                last_code = course["code"]
            tags = ["group"] if group else []
            if course["is_full"]:
                tags.append("full")
            elif course["seats_left"] <= 3:
                tags.append("low")
            self.tree_sheet.insert("", "end", tags=tuple(tags), values=(
                course["section_id"], course["code"], course["section"],
                course["title"], course["instructor"], course["department"],
                f"{course['day']} {course['start']}-{course['end']}",
                course["venue"], course["capacity"], course["enrolled"]))
        scope = "all terms" if year is None else f"{year} Term {term}"
        self.lbl_sheet_count.configure(
            text=f"{len(courses)} sections / {scope}")

    def refresh_regs(self) -> None:
        """Redraw the registration table, honouring the search box and term.

        The search is done by the database (SQL LIKE) because this table can
        hold thousands of rows, unlike the client's smaller per-term sheet.
        """
        if not self.db:
            return
        year, term = self._term_args()
        rows = self.db.registrations(year=year, term=term,
                                     q=self.var_reg_search.get().strip())
        self.tree_regs.delete(*self.tree_regs.get_children())
        for row in rows:
            email = row["email"] if len(row["email"]) <= 26 else row["email"][:25] + "…"
            self.tree_regs.insert("", "end", iid=str(row["id"]), values=(
                row["id"], f"{row['year']} T{row['term']}", row["label"],
                row["title"], row["student_id"], row["student_name"], email,
                f"{row['day']} {row['start']}-{row['end']}"))
        self.lbl_regs_count.configure(
            text=f"{len(rows)} registration{'s' if len(rows) != 1 else ''}")

    def refresh_students(self) -> None:
        """Redraw the student dictionary tab.

        Columns come straight from the `students` dictionary: id, name, e-mail,
        role and the `can_register` flag that requirement 3 switches.

        The count of registrations per student is worked out here from a single
        query, so the tab shows how the dictionary saves rows: many
        registrations, one student record.
        """
        if not self.db:
            return
        students = self.db.list_students()
        with self.db.session() as conn:
            counts = {r["student_id"]: r["total"] for r in conn.execute(
                "SELECT student_id, COUNT(*) AS total FROM registrations "
                "GROUP BY student_id")}
        self.tree_students.delete(*self.tree_students.get_children())
        for student in students:
            allowed = student.get("can_register", True)
            self.tree_students.insert(
                "", "end", iid=student["student_id"],
                tags=() if allowed else ("blocked",),
                values=(student["student_id"], student["name"], student["email"],
                        student["role"], "allowed" if allowed else "blocked",
                        student["created_at"], counts.get(student["student_id"], 0)))
        blocked = sum(1 for s in students if not s.get("can_register", True))
        self.lbl_students_count.configure(
            text=f"{len(students)} accounts / {blocked} blocked / "
                 f"{sum(counts.values())} registrations")
        self._on_student_select()

    # -- student account actions -----------------------------------------
    # These four methods are the whole of the v3 console work: selection
    # handling, the access switch, opening the add dialog, and refreshing after
    # it closes.
    def _on_student_select(self, _event=None) -> None:
        """Enable the allow/block buttons only while a row is selected.

        Calling this from refresh_students() as well means the buttons return to
        "disabled" after a refresh that cleared the selection, instead of
        staying clickable with nothing selected.
        """
        state = "normal" if self.tree_students.selection() else "disabled"
        self.btn_allow.configure(state=state)
        self.btn_block.configure(state=state)

    def set_student_access(self, allowed: bool) -> None:
        """Allow or block registration for the selected student (requirement 3).

        This is the same CourseDB call the admin API uses, so the console and
        the client's admin panel cannot behave differently.
        """
        selection = self.tree_students.selection()
        if not selection or not self.db:
            return
        student_id = selection[0]
        try:
            student = self.db.set_registration_access(student_id, allowed)
        except srv.ApiError as err:
            messagebox.showerror("Could not change access", err.message)
            return
        self.lbl_student_action.configure(
            text=f"{student_id}: registration "
                 f"{'allowed' if allowed else 'blocked'}",
            foreground=theme.OK if allowed else theme.DANGER)
        self._log(f"registration access for {student_id} set to "
                  f"{'allowed' if allowed else 'blocked'}")
        self.refresh_students()
        self.set_status(f"{student_id} can {'now' if allowed else 'no longer'} "
                        f"register for courses.")

    def open_add_student(self) -> None:
        """Pop up the small form that adds a student to the dictionary.

        The dialog is modal and reports back through `on_added`, so this method
        never has to wait for it: the refresh happens when the user submits.
        """
        if not self.db:
            return
        AddStudentDialog(self, self.db, self.family, on_added=self._after_add_student)

    def _after_add_student(self, student: dict) -> None:
        """Refresh the list and report the new account after the dialog closes."""
        # Two signals for one action: the inline label next to the buttons, and
        # a line in the activity log so there is a permanent record of it.
        self.lbl_student_action.configure(
            text=f"added {student['student_id']}", foreground=theme.OK)
        self._log(f"student account created: {student['student_id']} "
                  f"({student['name']})")
        self.refresh_students()
        self.refresh_overview()
        self.set_status(f"Student {student['student_id']} added.")

    def _auto_refresh(self) -> None:
        """Refresh the visible tab every two seconds, then re-arm the timer.

        Only the tab in front is redrawn: refreshing hidden tables would waste
        work and could make the visible one flicker.
        """
        try:
            if self.db:
                self.refresh_overview()
                index = self.nb.index(self.nb.select())
                # Tab 1 = Course Sheet, 2 = Registrations, 3 = Students.
                if index == 1:
                    self.refresh_sheet()
                elif index == 2:
                    self.refresh_regs()
                elif index == 3:
                    self.refresh_students()
        finally:
            # `finally` guarantees the timer is re-armed even if a redraw fails,
            # otherwise auto refresh would silently stop for good.
            self.after(2000, self._auto_refresh)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def reload_courses(self) -> None:
        """Re-read courses.csv into the database and rebuild the dictionaries.

        This is the only way the sheet changes at run time: edit the CSV, press
        the button, and both the APIs and every connected client see the new
        sections.  Existing registrations survive because they reference
        section ids, not row numbers.
        """
        if not self.db:
            return
        report = self.db.reload_courses()
        self._log(f"course sheet reloaded: {report['loaded']} sections, "
                  f"{report['skipped']} duplicates skipped")
        self.load_terms()
        self.refresh_all()
        self.set_status(f"Course sheet reloaded: {report['loaded']} sections "
                        f"({report['skipped']} duplicate rows skipped).")

    def cancel_selected(self) -> None:
        """Cancel the highlighted registration after a confirmation.

        Cancelling from the console and cancelling from the client both go
        through CourseDB.cancel(), so both free the seat in exactly the same
        way and both are logged.
        """
        if not self.db:
            return
        selection = self.tree_regs.selection()
        if not selection:
            messagebox.showinfo("Nothing selected", "Select a registration row first.")
            return
        reg_id = int(selection[0])
        values = self.tree_regs.item(selection[0], "values")
        if not messagebox.askyesno(
                "Cancel registration",
                f"Cancel registration #{reg_id}\n{values[2]} - {values[3]}\n"
                f"{values[5]} ({values[4]})?"):
            return
        try:
            self.db.cancel(reg_id)
        except srv.ApiError as err:
            messagebox.showerror("Could not cancel", err.message)
            return
        self._log(f"registration #{reg_id} cancelled from the console")
        self.set_status(f"Registration #{reg_id} cancelled.")
        self.refresh_all()

    def export_csv(self) -> None:
        """Write the currently visible registrations to a CSV file."""
        if not self.db:
            return
        year, term = self._term_args()
        rows = self.db.registrations(year=year, term=term,
                                     q=self.var_reg_search.get().strip())
        if not rows:
            messagebox.showinfo("Nothing to export",
                                "There are no registrations to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export registrations", defaultextension=".csv",
            initialfile="registrations.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")])
        if not path:
            return                       # the user cancelled the dialog
        # The export uses exactly the rows that are on screen (term filter and
        # search included), which is what "export what I am looking at" means.
        
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        self._log(f"exported {len(rows)} registrations to {path}")
        self.set_status(f"Exported {len(rows)} registrations to "
                        f"{os.path.basename(path)}")
        messagebox.showinfo("Export complete",
                            f"{len(rows)} registrations written to\n{path}")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        """Stop the API thread cleanly before the window disappears."""
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.destroy()


class AddStudentDialog(tk.Toplevel):
    """Modal form that adds one student account to the dictionary.

    The password entry is masked, and the password is hashed by CourseDB before
    it reaches SQLite, so the plain text exists only inside this widget.

    Requirement 2 asks for a way to add students on the server side; this is the
    console's version of it.  It calls the very same `CourseDB.add_student()`
    that the admin API uses, so validation, hashing and the duplicate guard are
    identical however the account was created.
    """

    FIELDS = (("Student ID", "student_id", ""),
              ("Full name", "name", ""),
              ("E-mail", "email", ""),
              ("Password", "password", "\u2022"))

    def __init__(self, parent: tk.Misc, db: "srv.CourseDB", font_family: str,
                 on_added=None):
        super().__init__(parent)
        self.family = font_family
        self.db = db
        self.on_added = on_added
        self.title("New student account")
        self.configure(background=theme.BG)
        self.geometry("480x400")
        self.resizable(False, False)
        # transiet + grab_set make it properly modal: the console cannot be
        # clicked until the dialog is answered.
        self.transient(parent)
        self.grab_set()

        outer = theme.card(self)
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        card = outer.inner
        form = tk.Frame(card, background=theme.CARD)
        form.pack(fill="both", expand=True, padx=20, pady=18)
        form.columnconfigure(1, weight=1)

        tk.Label(form, text="Add a student", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        tk.Label(form, text="The password is hashed before it is stored.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9)).grid(row=1, column=0, columnspan=2,
                                             sticky="w", pady=(2, 12))

        self.vars: dict[str, tk.StringVar] = {}
        for index, (label, key, mask) in enumerate(self.FIELDS, start=2):
            tk.Label(form, text=label, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.family, 9, "bold")).grid(row=index, column=0,
                                                         sticky="w", pady=5,
                                                         padx=(0, 12))
            var = tk.StringVar()
            self.vars[key] = var
            entry = ttk.Entry(form, textvariable=var, show=mask)
            entry.grid(row=index, column=1, sticky="ew", pady=5)
            if key == "student_id":
                entry.focus_set()

        # "Registration allowed" is on by default; unticking it creates the
        # account already blocked, which is useful for a suspended student or
        # for preparing accounts before the registration window opens.
        self.var_allowed = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Allow this student to register straight away",
                        variable=self.var_allowed,
                        style="Card.TCheckbutton").grid(row=6, column=0,
                                                        columnspan=2, sticky="w",
                                                        pady=(8, 0))

        self.lbl_msg = tk.Label(form, text="", background=theme.CARD,
                                foreground=theme.MUTED, font=(self.family, 9),
                                anchor="w", justify="left", wraplength=380)
        self.lbl_msg.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        buttons = tk.Frame(form, background=theme.CARD)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton",
                   command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Add student", style="Primary.TButton",
                   command=self.submit).pack(side="left")

        self.bind("<Return>", lambda _e: self.submit())
        self.bind("<Escape>", lambda _e: self.destroy())

    def submit(self) -> None:
        """Validate through CourseDB and close on success.

        Validation errors stay in the dialog (rather than raising a pop-up over
        it) so the user can correct the offending field without losing what they
        already typed.
        """
        try:
            student = self.db.add_student({
                "student_id": self.vars["student_id"].get(),
                "name": self.vars["name"].get(),
                "email": self.vars["email"].get(),
                "password": self.vars["password"].get(),
                "can_register": self.var_allowed.get(),
            })
        except srv.ApiError as err:
            # Errors stay in the dialog so the user can correct the field.
            self.lbl_msg.configure(text=err.message, foreground=theme.DANGER)
            return
        if self.on_added:
            self.on_added(student)
        self.destroy()


def main() -> None:
    """Command line entry point for the console.

    The flags mirror `python3 server.py` so the two can be swapped freely:
        --host   interface to bind (default 0.0.0.0)
        --port   port to listen on (default 8000)
        --demo   insert sample registrations at start-up
    """
    parser = argparse.ArgumentParser(description="Class Registration - server console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true",
                        help="insert sample registrations on start")
    args = parser.parse_args()
    ServerConsole(args.host, args.port, args.demo).mainloop()


if __name__ == "__main__":
    main()
