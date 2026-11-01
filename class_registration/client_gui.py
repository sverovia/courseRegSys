#!/usr/bin/env python3
"""
client_gui.py - Class Registration System: client side.

A desktop GUI (Tkinter) where a student browses the course sheet, picks a
course and registers against the server, then reviews or cancels their own
registrations.  It talks to server.py over plain HTTP/JSON.

    python3 client_gui.py
    python3 client_gui.py --server http://192.168.1.20:8000

Nothing beyond the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

import theme

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "client_settings.json")
DEFAULT_SERVER = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ApiClient:
    """Thin JSON wrapper around the registration server."""

    def __init__(self, base_url: str = DEFAULT_SERVER):
        self.base_url = base_url.rstrip("/")

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            raise ApiError(err.code, body.get("code", "HTTP_ERROR"),
                           body.get("error", f"Server returned HTTP {err.code}")) from None
        except urllib.error.URLError as err:
            raise ApiError(0, "NO_SERVER",
                           f"Cannot reach the server at {self.base_url}.\n"
                           f"Start it with:  python3 server.py\n({err.reason})") from None
        except TimeoutError:
            raise ApiError(0, "TIMEOUT", "The server did not answer in time.") from None

    # -- endpoints ---------------------------------------------------------
    def courses(self) -> list[dict]:
        return self._call("GET", "/api/courses")["courses"]

    def register(self, course_code: str, student_id: str, student_name: str,
                 email: str, notes: str = "") -> dict:
        payload = {"course_code": course_code, "student_id": student_id,
                   "student_name": student_name, "email": email, "notes": notes}
        return self._call("POST", "/api/registrations", payload)["registration"]

    def registrations(self, student_id: str = "") -> list[dict]:
        qs = f"?student_id={urllib.parse.quote(student_id)}" if student_id else ""
        return self._call("GET", "/api/registrations" + qs)["registrations"]

    def cancel(self, registration_id: int) -> dict:
        return self._call("DELETE", f"/api/registrations/{registration_id}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class RegistrationClient(tk.Tk):
    def __init__(self, server: str = DEFAULT_SERVER):
        super().__init__()
        self.fam = theme.apply(self)
        self.title("Class Registration - Student Client")
        self.geometry("1240x760")
        self.minsize(1080, 660)

        self.api = ApiClient(server)
        self.courses: list[dict] = []
        self.selected: dict | None = None
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self._msg_code: str | None = None   # course the on-screen form message is about
        self._load_settings()

        self._build_header()
        self._build_body()
        self._build_statusbar()

        self.after(120, self.refresh_courses)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- settings ----------------------------------------------------------
    def _load_settings(self) -> None:
        self.settings = {"server": self.api.base_url, "student_id": "",
                         "student_name": "", "email": ""}
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                self.settings.update(json.load(fh))
                self.api = ApiClient(self.settings["server"])
        except (OSError, ValueError):
            pass

    def _save_settings(self) -> None:
        self.settings.update(server=self.api.base_url,
                             student_id=self.var_sid.get().strip(),
                             student_name=self.var_name.get().strip(),
                             email=self.var_email.get().strip())
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, indent=2)
        except OSError:
            pass

    # -- header ------------------------------------------------------------
    def _build_header(self) -> None:
        bar = tk.Frame(self, background=theme.HEADER, height=84)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, background=theme.HEADER)
        left.pack(side="left", padx=22, pady=14)
        tk.Label(left, text="Course Registration", background=theme.HEADER,
                 foreground="#ffffff", font=(self.fam, 17, "bold")).pack(anchor="w")
        tk.Label(left, text="Student client  -  browse the course sheet and register",
                 background=theme.HEADER, foreground=theme.HEADER_SUB,
                 font=(self.fam, 9)).pack(anchor="w")

        right = tk.Frame(bar, background=theme.HEADER)
        right.pack(side="right", padx=22)
        tk.Label(right, text="SERVER", background=theme.HEADER, foreground=theme.HEADER_SUB,
                 font=(self.fam, 8, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.var_server = tk.StringVar(value=self.api.base_url)
        entry = tk.Entry(right, textvariable=self.var_server, width=30, relief="flat",
                         background="#2c3a61", foreground="#ffffff", insertbackground="#fff",
                         font=(self.fam, 10), highlightthickness=1,
                         highlightbackground="#43548a", highlightcolor=theme.HEADER_SUB)
        entry.grid(row=1, column=0, ipady=4)
        entry.bind("<Return>", lambda _e: self.connect())
        self.btn_connect = ttk.Button(right, text="Connect", style="Ghost.TButton",
                                      command=self.connect)
        self.btn_connect.grid(row=1, column=1, padx=(8, 0))
        self.dot = theme.status_dot(right, theme.MUTED, bg=theme.HEADER)
        self.dot.grid(row=1, column=2, padx=(10, 0))
        self.lbl_conn = tk.Label(right, text="not connected", background=theme.HEADER,
                                 foreground=theme.HEADER_SUB, font=(self.fam, 8))
        self.lbl_conn.grid(row=2, column=0, columnspan=3, sticky="w", pady=(3, 0))

    # -- body --------------------------------------------------------------
    def _build_body(self) -> None:
        body = tk.Frame(self, background=theme.BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)
        body.columnconfigure(0, weight=3, minsize=560)
        body.columnconfigure(1, weight=2, minsize=400)
        body.rowconfigure(0, weight=1)

        self._build_course_panel(body)
        self._build_side_panel(body)

    # ---- left: course sheet ---------------------------------------------
    def _build_course_panel(self, parent: tk.Misc) -> None:
        outer = theme.card(parent)
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        card = outer.inner

        head = tk.Frame(card, background=theme.CARD)
        head.pack(fill="x", padx=18, pady=(16, 10))
        tk.Label(head, text="Course Sheet", background=theme.CARD, foreground=theme.INK,
                 font=(self.fam, 13, "bold")).pack(side="left")
        self.lbl_course_count = tk.Label(head, text="", background=theme.CARD,
                                         foreground=theme.MUTED, font=(self.fam, 9))
        self.lbl_course_count.pack(side="right")

        tools = tk.Frame(card, background=theme.CARD)
        tools.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(tools, text="Search", background=theme.CARD, foreground=theme.MUTED,
                 font=(self.fam, 9, "bold")).pack(side="left", padx=(0, 8))
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self._render_courses())
        search = ttk.Entry(tools, textvariable=self.var_search, width=26)
        search.pack(side="left")
        self.var_hide_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="Hide full courses", variable=self.var_hide_full,
                        command=self._render_courses,
                        style="Card.TCheckbutton").pack(side="right")

        table = tk.Frame(card, background=theme.CARD)
        table.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        cols = ("code", "title", "time", "venue", "places")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
        headings = {"code": ("Code", 58, "w"), "title": ("Course title", 120, "w"),
                    "time": ("Time", 138, "w"), "venue": ("Venue", 152, "w"),
                    "places": ("Places", 72, "center")}
        for key, (text, width, anchor) in headings.items():
            self.tree.heading(key, text=text)
            # only the title column grows, so the rest always stays visible
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=(key == "title"))
        self.tree.tag_configure("full", foreground="#9aa5b8")
        self.tree.tag_configure("low", foreground=theme.WARN)
        self.tree.bind("<<TreeviewSelect>>", self._on_course_select)
        self.tree.bind("<Double-1>", lambda _e: self._focus_name_field())
        self.tree.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        legend = tk.Frame(card, background=theme.CARD)
        legend.pack(fill="x", padx=18, pady=(6, 14))
        tk.Label(legend, text="Grey = full   /   amber = 3 places or fewer",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.fam, 8)).pack(side="left")
        ttk.Button(legend, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_courses).pack(side="right")

    # ---- right: register + my registrations ------------------------------
    def _build_side_panel(self, parent: tk.Misc) -> None:
        side = tk.Frame(parent, background=theme.BG)
        side.grid(row=0, column=1, sticky="nsew")
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)

        # ---------------- register card ----------------
        outer = theme.card(side)
        outer.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        card = outer.inner

        tk.Label(card, text="Register for a course", background=theme.CARD,
                 foreground=theme.INK, font=(self.fam, 13, "bold")).pack(
            anchor="w", padx=18, pady=(16, 2))

        self.lbl_sel_title = tk.Label(card, text="No course selected",
                                      background=theme.CARD, foreground=theme.PRIMARY_DARK,
                                      font=(self.fam, 11, "bold"), anchor="w",
                                      justify="left", wraplength=340)
        self.lbl_sel_title.pack(fill="x", padx=18, pady=(8, 0))
        self.lbl_sel_meta = tk.Label(card, text="Pick a row in the course sheet to begin.",
                                     background=theme.CARD, foreground=theme.MUTED,
                                     font=(self.fam, 9), anchor="w", justify="left",
                                     wraplength=340)
        self.lbl_sel_meta.pack(fill="x", padx=18, pady=(2, 10))

        form = tk.Frame(card, background=theme.CARD)
        form.pack(fill="x", padx=18)
        form.columnconfigure(1, weight=1)

        self.var_sid = tk.StringVar(value=self.settings["student_id"])
        self.var_name = tk.StringVar(value=self.settings["student_name"])
        self.var_email = tk.StringVar(value=self.settings["email"])
        self.var_notes = tk.StringVar()

        rows = (("Student ID *", self.var_sid, "e.g. S1042"),
                ("Full name *", self.var_name, "e.g. Amy Wong"),
                ("E-mail *", self.var_email, "name@student.edu"),
                ("Notes", self.var_notes, "optional"))
        self.entries: dict[str, ttk.Entry] = {}
        for i, (label, var, hint) in enumerate(rows):
            tk.Label(form, text=label, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.fam, 9, "bold")).grid(row=i, column=0, sticky="w",
                                                      pady=4, padx=(0, 10))
            ent = ttk.Entry(form, textvariable=var)
            ent.grid(row=i, column=1, sticky="ew", pady=4)
            self.entries[label] = ent
            if label == "Student ID *":
                ent.bind("<FocusOut>", lambda _e: self.load_my_registrations())
                self.ent_sid = ent
            if label == "Full name *":
                self.ent_name = ent
        self.var_notes.set("")

        self.btn_register = ttk.Button(card, text="Register", style="Primary.TButton",
                                       command=self.do_register)
        self.btn_register.pack(fill="x", padx=18, pady=(14, 6))
        self.lbl_form_msg = tk.Label(card, text="", background=theme.CARD,
                                     foreground=theme.MUTED, font=(self.fam, 9),
                                     anchor="w", justify="left", wraplength=340)
        self.lbl_form_msg.pack(fill="x", padx=18, pady=(0, 14))

        # ---------------- my registrations card ----------------
        outer2 = theme.card(side)
        outer2.grid(row=1, column=0, sticky="nsew")
        card2 = outer2.inner
        card2.rowconfigure(2, weight=1)
        card2.columnconfigure(0, weight=1)

        head2 = tk.Frame(card2, background=theme.CARD)
        head2.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 4))
        tk.Label(head2, text="My registrations", background=theme.CARD,
                 foreground=theme.INK, font=(self.fam, 13, "bold")).pack(side="left")
        ttk.Button(head2, text="Refresh", style="Ghost.TButton",
                   command=self.load_my_registrations).pack(side="right")

        self.lbl_my_hint = tk.Label(card2, text="Enter a student ID to see its registrations.",
                                    background=theme.CARD, foreground=theme.MUTED,
                                    font=(self.fam, 9), anchor="w")
        self.lbl_my_hint.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 6))

        wrap = tk.Frame(card2, background=theme.CARD)
        wrap.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=18)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols2 = ("code", "title", "when")
        self.tree_mine = ttk.Treeview(wrap, columns=cols2, show="headings",
                                      selectmode="browse", height=6)
        for key, text, width, anchor in (("code", "Code", 62, "w"),
                                         ("title", "Course", 120, "w"),
                                         ("when", "When / Venue", 190, "w")):
            self.tree_mine.heading(key, text=text)
            self.tree_mine.column(key, width=width, minwidth=50, anchor=anchor,
                                  stretch=(key == "title"))
        self.tree_mine.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_mine.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.tree_mine.configure(yscrollcommand=sb2.set)

        foot = tk.Frame(card2, background=theme.CARD)
        foot.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(8, 14))
        self.lbl_my_count = tk.Label(foot, text="", background=theme.CARD,
                                     foreground=theme.MUTED, font=(self.fam, 9))
        self.lbl_my_count.pack(side="left")
        self.btn_cancel = ttk.Button(foot, text="Cancel selected", style="Danger.TButton",
                                     command=self.do_cancel, state="disabled")
        self.btn_cancel.pack(side="right")
        self.tree_mine.bind("<<TreeviewSelect>>", lambda _e: self.btn_cancel.configure(
            state="normal" if self.tree_mine.selection() else "disabled"))

    # -- status bar --------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, background="#dfe5f2", height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.var_status = tk.StringVar(value="Ready.")
        tk.Label(bar, textvariable=self.var_status, background="#dfe5f2",
                 foreground=theme.MUTED, font=(self.fam, 9), anchor="w").pack(
            side="left", padx=16)
        self.lbl_stats = tk.Label(bar, text="", background="#dfe5f2",
                                  foreground=theme.MUTED, font=(self.fam, 9))
        self.lbl_stats.pack(side="right", padx=16)

    # ------------------------------------------------------------------
    # Async helpers: HTTP work runs in a thread, UI updates on the main loop
    # ------------------------------------------------------------------
    def _run(self, work, on_done, on_error=None) -> None:
        def runner():
            try:
                result = work()
            except ApiError as err:
                self.results.put(("error", on_error, err))
            except Exception as err:                      # noqa: BLE001
                self.results.put(("error", on_error, ApiError(0, "CLIENT_ERROR", str(err))))
            else:
                self.results.put(("ok", on_done, result))
        threading.Thread(target=runner, daemon=True).start()
        self.after(60, self._drain)

    def _drain(self) -> None:
        try:
            while True:
                kind, callback, payload = self.results.get_nowait()
                if kind == "ok":
                    if callback:
                        callback(payload)
                else:
                    self._handle_error(payload, callback)
        except queue.Empty:
            pass
        if not self.results.empty():
            self.after(60, self._drain)

    def _handle_error(self, err: ApiError, callback) -> None:
        if callback:
            callback(err)
            return
        if err.code == "NO_SERVER":
            self._set_conn(False, "offline")
            self.set_status("Server unreachable - press Connect once server.py is running.")
        else:
            messagebox.showerror("Request failed", err.message)

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        self.api = ApiClient(self.var_server.get().strip() or DEFAULT_SERVER)
        self.var_server.set(self.api.base_url)
        self.settings["server"] = self.api.base_url
        self.set_status(f"Connecting to {self.api.base_url} ...")
        self._run(lambda: ApiClient(self.api.base_url).courses(), self._on_connected)

    def _on_connected(self, courses: list[dict]) -> None:
        self._set_conn(True, "connected")
        self._save_settings()
        self.refresh_courses()

    def _set_conn(self, ok: bool, text: str) -> None:
        self.dot.set_colour(theme.OK if ok else theme.DANGER)
        self.lbl_conn.configure(text=text,
                                foreground=theme.HEADER_SUB if ok else "#ffb4b4")

    def set_status(self, text: str) -> None:
        self.var_status.set(text)

    # -- data --------------------------------------------------------------
    def refresh_courses(self) -> None:
        self.set_status("Loading course sheet ...")

        def done(courses: list[dict]) -> None:
            self.courses = courses
            self._render_courses()
            self._set_conn(True, "connected")
            stats = self.api
            total = sum(c["capacity"] for c in courses)
            filled = sum(c["enrolled"] for c in courses)
            self.lbl_stats.configure(
                text=f"{len(courses)} courses   |   {filled}/{total} places taken")
            self.set_status(f"Course sheet loaded from {self.api.base_url}")
            if self.var_sid.get().strip():
                self.load_my_registrations()

        self._run(self.api.courses, done)

    def _visible_courses(self) -> list[dict]:
        term = self.var_search.get().strip().lower()
        hide_full = self.var_hide_full.get()
        out = []
        for c in self.courses:
            if hide_full and c["is_full"]:
                continue
            haystack = " ".join(str(c[k]) for k in
                                ("code", "title", "instructor", "venue", "department")).lower()
            if term and term not in haystack:
                continue
            out.append(c)
        return out

    def _render_courses(self) -> None:
        keep = self.selected["code"] if self.selected else None
        self.tree.delete(*self.tree.get_children())
        shown = self._visible_courses()
        for c in shown:
            tags = ()
            if c["is_full"]:
                tags = ("full",)
            elif c["seats_left"] <= 3:
                tags = ("low",)
            self.tree.insert("", "end", iid=c["code"], tags=tags, values=(
                c["code"], c["title"], f"{c['day']}  {c['start']}-{c['end']}",
                c["venue"], f"{c['enrolled']}/{c['capacity']}"))
        self.lbl_course_count.configure(
            text=f"{len(shown)} of {len(self.courses)} courses shown")
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)

    # -- selection ---------------------------------------------------------
    def _on_course_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        self.selected = next((c for c in self.courses if c["code"] == code), None)
        if not self.selected:
            return
        c = self.selected
        self.lbl_sel_title.configure(text=f"{c['code']}  -  {c['title']}")
        places = ("FULL - no places left" if c["is_full"]
                  else f"{c['seats_left']} of {c['capacity']} places left")
        self.lbl_sel_meta.configure(
            text=(f"{c['day']}  {c['start']}-{c['end']}   |   {c['venue']}\n"
                  f"{c['instructor']}   |   {c['credits']} credits   |   {places}"))
        if code != self._msg_code:          # a new pick clears the old feedback
            self.lbl_form_msg.configure(text="")
            self._msg_code = None
        self.btn_register.configure(state="disabled" if c["is_full"] else "normal")
        self.set_status(f"Selected {c['code']} - {c['title']}")

    def _focus_name_field(self) -> None:
        (self.ent_sid if not self.var_sid.get().strip() else self.ent_name).focus_set()

    # -- register ----------------------------------------------------------
    def do_register(self) -> None:
        if not self.selected:
            messagebox.showinfo("No course selected",
                                "Choose a course row in the course sheet first.")
            return
        payload = {
            "course_code": self.selected["code"],
            "student_id": self.var_sid.get().strip(),
            "student_name": self.var_name.get().strip(),
            "email": self.var_email.get().strip(),
            "notes": self.var_notes.get().strip(),
        }
        self.btn_register.configure(state="disabled", text="Registering ...")
        self.lbl_form_msg.configure(text="Sending registration ...", foreground=theme.MUTED)
        self._run(lambda: self.api.register(**payload), self._on_registered, self._on_reg_failed)

    def _on_registered(self, reg: dict) -> None:
        self.btn_register.configure(text="Register",
                                    state="normal" if not (self.selected and self.selected["is_full"])
                                    else "disabled")
        self._msg_code = reg["course_code"]
        self.lbl_form_msg.configure(
            text=f"Registered in {reg['course_code']} - {reg['title']}\n"
                 f"{reg['day']} {reg['start']}-{reg['end']}, {reg['venue']}   "
                 f"(ref #{reg['id']})", foreground=theme.OK)
        self.var_notes.set("")
        self._save_settings()
        self.set_status(f"Registration #{reg['id']} confirmed for {reg['student_id']}")
        self.refresh_courses()
        self.load_my_registrations()

    def _on_reg_failed(self, err: ApiError) -> None:
        self.btn_register.configure(text="Register", state="normal")
        self._msg_code = self.selected["code"] if self.selected else None
        colours = {"FULL": theme.DANGER, "DUPLICATE": theme.WARN,
                   "TIME_CONFLICT": theme.WARN}
        self.lbl_form_msg.configure(text=err.message,
                                    foreground=colours.get(err.code, theme.DANGER))
        self.set_status(err.message.splitlines()[0])
        if err.code in ("FULL", "DUPLICATE", "TIME_CONFLICT"):
            self.refresh_courses()
        if err.code == "NO_SERVER":
            self._set_conn(False, "offline")

    # -- my registrations --------------------------------------------------
    def load_my_registrations(self) -> None:
        sid = self.var_sid.get().strip()
        self.tree_mine.delete(*self.tree_mine.get_children())
        if not sid:
            self.lbl_my_hint.configure(text="Enter a student ID to see its registrations.")
            self.lbl_my_count.configure(text="")
            self.btn_cancel.configure(state="disabled")
            return
        self.lbl_my_hint.configure(text=f"Registrations held by {sid.upper()}")
        self._run(lambda: self.api.registrations(sid), self._render_mine)

    def _render_mine(self, rows: list[dict]) -> None:
        self.tree_mine.delete(*self.tree_mine.get_children())
        for r in rows:
            self.tree_mine.insert("", "end", iid=str(r["id"]), values=(
                r["course_code"], r["title"],
                f"{r['day']} {r['start']}  \u00b7  {r['venue']}"))
        self.lbl_my_count.configure(
            text=f"{len(rows)} registration{'s' if len(rows) != 1 else ''}")
        self.btn_cancel.configure(state="disabled")

    def do_cancel(self) -> None:
        sel = self.tree_mine.selection()
        if not sel:
            return
        reg_id = int(sel[0])
        values = self.tree_mine.item(sel[0], "values")
        if not messagebox.askyesno("Cancel registration",
                                   f"Cancel registration #{reg_id}\n{values[0]} - {values[1]}?"):
            return
        self._run(lambda: self.api.cancel(reg_id), self._on_cancelled, self._on_cancel_failed)

    def _on_cancelled(self, res: dict) -> None:
        self.set_status(f"Registration #{res['deleted']} cancelled.")
        self.refresh_courses()
        self.load_my_registrations()

    def _on_cancel_failed(self, err: ApiError) -> None:
        messagebox.showerror("Could not cancel", err.message)

    # -- misc --------------------------------------------------------------
    def _on_close(self) -> None:
        self._save_settings()
        self.destroy()


def main() -> None:
    ap = argparse.ArgumentParser(description="Class Registration - student client")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="server base URL")
    args = ap.parse_args()
    app = RegistrationClient(args.server)
    app.mainloop()


if __name__ == "__main__":
    main()
