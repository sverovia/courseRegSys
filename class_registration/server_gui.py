#!/usr/bin/env python3
"""
server_gui.py - Class Registration System: server side console.

Starts the JSON API from server.py inside a background thread and shows it in a
desktop console:

    * Overview      - live counters and per-course fill bars
    * Course Sheet  - the full sheet (code, time, venue, candidate limit)
    * Registrations - every registration the server has received, searchable,
                      with CSV export and the ability to cancel a registration
    * Activity Log  - the server's request log

    python3 server_gui.py                 # starts API on http://0.0.0.0:8000
    python3 server_gui.py --port 9000 --demo
"""

from __future__ import annotations

import argparse
import csv
import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import server as srv
import theme

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def start_api(host: str, port: int, db_path: str, courses: str, log_sink) -> srv.ThreadingHTTPServer:
    """Create the HTTP server and serve it from a daemon thread."""
    httpd = srv.create_server(host, port, db_path, courses, log_callback=log_sink)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True, name="api-server").start()
    return httpd


class ServerConsole(tk.Tk):
    def __init__(self, host: str, port: int, demo: bool = False):
        super().__init__()
        self.fam = theme.apply(self)
        self.title("Class Registration - Server Console")
        self.geometry("1240x770")
        self.minsize(1060, 660)

        self.host, self.port = host, port
        self.log_lines: list[str] = []
        self.log_sink = self._enqueue_log
        self.httpd: srv.ThreadingHTTPServer | None = None
        self.db: srv.CourseDB | None = None

        self._build_header()
        self._build_notebook()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(200, lambda: self._start(host, port, demo))
        self.after(500, self._pump_log)
        self.after(1500, self._auto_refresh)

    # -- header ------------------------------------------------------------
    def _build_header(self) -> None:
        bar = tk.Frame(self, background=theme.HEADER, height=84)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, background=theme.HEADER)
        left.pack(side="left", padx=22, pady=14)
        tk.Label(left, text="Registration Server", background=theme.HEADER,
                 foreground="#ffffff", font=(self.fam, 17, "bold")).pack(anchor="w")
        self.lbl_endpoint = tk.Label(left, text="starting ...", background=theme.HEADER,
                                     foreground=theme.HEADER_SUB, font=(self.fam, 9))
        self.lbl_endpoint.pack(anchor="w")

        right = tk.Frame(bar, background=theme.HEADER)
        right.pack(side="right", padx=22)
        self.dot = theme.status_dot(right, theme.WARN, bg=theme.HEADER)
        self.dot.pack(side="left", padx=(0, 8))
        self.lbl_state = tk.Label(right, text="starting", background=theme.HEADER,
                                  foreground="#ffd9a0", font=(self.fam, 10, "bold"))
        self.lbl_state.pack(side="left")
        self.btn_power = tk.Button(right, text="Stop server", relief="flat", bd=0,
                                   background="#2c3a61", foreground="#ffffff",
                                   activebackground="#43548a", activeforeground="#ffffff",
                                   font=(self.fam, 9, "bold"), padx=14, pady=6,
                                   command=self._toggle_server, cursor="hand2")
        self.btn_power.pack(side="left", padx=(14, 0))

    # -- notebook ----------------------------------------------------------
    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(14, 8))
        self.tab_over = ttk.Frame(self.nb)
        self.tab_sheet = ttk.Frame(self.nb)
        self.tab_regs = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_over, text="Overview")
        self.nb.add(self.tab_sheet, text="Course Sheet")
        self.nb.add(self.tab_regs, text="Registrations")
        self.nb.add(self.tab_log, text="Activity Log")
        self._build_overview()
        self._build_sheet()
        self._build_regs()
        self._build_log()

    # ---- overview --------------------------------------------------------
    def _build_overview(self) -> None:
        wrap = tk.Frame(self.tab_over, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)

        cards = tk.Frame(wrap, background=theme.BG)
        cards.pack(fill="x")
        self.stat_labels: dict[str, tk.Label] = {}
        specs = (("registrations", "Registrations", theme.PRIMARY),
                 ("students", "Unique students", "#7a4fd1"),
                 ("filled", "Places filled", theme.OK),
                 ("full", "Full courses", theme.DANGER))
        for i, (key, title, colour) in enumerate(specs):
            cards.columnconfigure(i, weight=1, uniform="stat")
            outer = theme.card(cards)
            outer.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 12, 0))
            inner = outer.inner
            tk.Frame(inner, background=colour, height=4).pack(fill="x")
            tk.Label(inner, text=title, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.fam, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
            lbl = tk.Label(inner, text="0", background=theme.CARD, foreground=theme.INK,
                           font=(self.fam, 24, "bold"))
            lbl.pack(anchor="w", padx=16, pady=(0, 14))
            self.stat_labels[key] = lbl

        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True, pady=(14, 0))
        inner = outer.inner
        head = tk.Frame(inner, background=theme.CARD)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="Seats filled by course", background=theme.CARD,
                 foreground=theme.INK, font=(self.fam, 12, "bold")).pack(side="left")
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_all).pack(side="right")

        canvas = tk.Canvas(inner, background=theme.CARD, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True, padx=18, pady=(0, 14))
        self.bars_frame = tk.Frame(canvas, background=theme.CARD)
        canvas.create_window((0, 0), window=self.bars_frame, anchor="nw",
                             tags="bars")
        self.bars_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure("bars", width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.bars_canvas = canvas

    # ---- course sheet ----------------------------------------------------
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
        tk.Label(head, text="Course Sheet  (source: courses.csv)", background=theme.CARD,
                 foreground=theme.INK, font=(self.fam, 12, "bold")).pack(side="left")
        self.lbl_sheet_count = tk.Label(head, text="", background=theme.CARD,
                                        foreground=theme.MUTED, font=(self.fam, 9))
        self.lbl_sheet_count.pack(side="right", padx=(0, 12))
        ttk.Button(head, text="Reload from CSV", style="Ghost.TButton",
                   command=self.reload_courses).pack(side="right")

        cols = ("code", "title", "instructor", "dept", "time", "venue", "cap", "enr")
        self.tree_sheet = ttk.Treeview(inner, columns=cols, show="headings")
        spec = (("code", "Code", 66, "w"), ("title", "Course title", 170, "w"),
                ("instructor", "Instructor", 142, "w"), ("dept", "Department", 146, "w"),
                ("time", "Time", 140, "w"), ("venue", "Venue", 146, "w"),
                ("cap", "Limit", 58, "center"), ("enr", "Enrolled", 80, "center"))
        for key, text, width, anchor in spec:
            self.tree_sheet.heading(key, text=text)
            self.tree_sheet.column(key, width=width, anchor=anchor,
                                   stretch=(key == "title"))
        self.tree_sheet.tag_configure("full", foreground="#9aa5b8")
        self.tree_sheet.tag_configure("low", foreground=theme.WARN)
        self.tree_sheet.grid(row=1, column=0, sticky="nsew", padx=(18, 0), pady=(0, 16))
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.tree_sheet.yview)
        sb.grid(row=1, column=1, sticky="ns", padx=(0, 18), pady=(0, 16))
        self.tree_sheet.configure(yscrollcommand=sb.set)

    # ---- registrations ---------------------------------------------------
    def _build_regs(self) -> None:
        wrap = tk.Frame(self.tab_regs, background=theme.BG)
        wrap.pack(fill="both", expand=True, padx=4, pady=10)
        outer = theme.card(wrap)
        outer.pack(fill="both", expand=True)
        inner = outer.inner
        inner.rowconfigure(3, weight=1)
        inner.columnconfigure(1, weight=1)

        tk.Label(inner, text="Registrations received", background=theme.CARD,
                 foreground=theme.INK, font=(self.fam, 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 2))
        tk.Label(inner, text="Everything the API has stored. Search, export to CSV, "
                             "or cancel a single registration.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.fam, 9)).grid(row=1, column=0, columnspan=2, sticky="w",
                                          padx=18, pady=(0, 8))

        tools = tk.Frame(inner, background=theme.CARD)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10))
        tk.Label(tools, text="Search", background=theme.CARD, foreground=theme.MUTED,
                 font=(self.fam, 9, "bold")).pack(side="left", padx=(0, 8))
        self.var_reg_search = tk.StringVar()
        self.var_reg_search.trace_add("write", lambda *_: self.refresh_regs())
        ttk.Entry(tools, textvariable=self.var_reg_search, width=26).pack(side="left")
        ttk.Button(tools, text="Export CSV", style="Ghost.TButton",
                   command=self.export_csv).pack(side="right", padx=(8, 0))
        ttk.Button(tools, text="Cancel selected", style="Danger.TButton",
                   command=self.cancel_selected).pack(side="right", padx=(8, 0))
        ttk.Button(tools, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_regs).pack(side="right")

        cols = ("id", "code", "title", "sid", "name", "email", "when", "created")
        self.tree_regs = ttk.Treeview(inner, columns=cols, show="headings")
        spec = (("id", "#", 44, "center"), ("code", "Course", 76, "w"),
                ("title", "Title", 172, "w"), ("sid", "Student ID", 94, "w"),
                ("name", "Name", 128, "w"), ("email", "E-mail", 168, "w"),
                ("when", "Class time", 130, "w"), ("created", "Registered at", 138, "w"))
        for key, text, width, anchor in spec:
            self.tree_regs.heading(key, text=text)
            self.tree_regs.column(key, width=width, minwidth=44, anchor=anchor,
                                  stretch=(key == "title"))
        self.tree_regs.grid(row=3, column=0, sticky="nsew", padx=(18, 0))
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.tree_regs.yview)
        sb.grid(row=3, column=1, sticky="ns", padx=(0, 18))
        self.tree_regs.configure(yscrollcommand=sb.set)

        self.lbl_regs_count = tk.Label(inner, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.fam, 9))
        self.lbl_regs_count.grid(row=4, column=0, columnspan=2, sticky="w",
                                 padx=18, pady=(8, 14))

    # ---- log -------------------------------------------------------------
    def _build_log(self) -> None:
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
                 font=(self.fam, 12, "bold")).pack(side="left")
        ttk.Button(head, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")

        self.txt_log = tk.Text(inner, background="#111a2b", foreground="#c8d4ea",
                               insertbackground="#fff", relief="flat", bd=0,
                               font=("DejaVu Sans Mono", 9), wrap="none", padx=12, pady=10)
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=(18, 0), pady=(0, 16))
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.txt_log.yview)
        sb.grid(row=1, column=1, sticky="ns", padx=(0, 18), pady=(0, 16))
        self.txt_log.configure(yscrollcommand=sb.set, state="disabled")

    # -- status bar --------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, background="#dfe5f2", height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.var_status = tk.StringVar(value="Starting the API ...")
        tk.Label(bar, textvariable=self.var_status, background="#dfe5f2",
                 foreground=theme.MUTED, font=(self.fam, 9), anchor="w").pack(
            side="left", padx=16)
        self.lbl_db = tk.Label(bar, text="", background="#dfe5f2", foreground=theme.MUTED,
                               font=(self.fam, 9))
        self.lbl_db.pack(side="right", padx=16)

    # -- server lifecycle --------------------------------------------------
    def _start(self, host: str, port: int, demo: bool) -> None:
        try:
            self.httpd = start_api(host, port, srv.DEFAULT_DB, srv.DEFAULT_COURSES,
                                   self.log_sink)
        except OSError as err:
            self._set_state("error", "cannot bind port")
            self.set_status(f"Could not start the server: {err}")
            messagebox.showerror("Server did not start",
                                 f"Port {port} could not be opened.\n\n{err}\n\n"
                                 "Try a different port:  python3 server_gui.py --port 9000")
            return
        self.db = self.httpd.db
        self.lbl_endpoint.configure(
            text=f"http://{host}:{port}   -   /api/courses  /api/registrations  /api/stats")
        self.lbl_db.configure(text=os.path.relpath(srv.DEFAULT_DB, BASE_DIR))
        self._set_state("running", f"running on port {port}")
        if demo:
            added = self.db.load_demo_data()
            self._log(f"demo data: {added} registrations inserted")
        self.set_status(f"Server listening on port {port}. Client can connect now.")
        self._log(f"server started on http://{host}:{port}")
        self.refresh_all()

    def _stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self._log("server stopped")
        self._set_state("stopped", "stopped")

    def _toggle_server(self) -> None:
        if self.httpd:
            self._stop()
            self.btn_power.configure(text="Start server")
        else:
            self._start(self.host, self.port, False)
            self.btn_power.configure(text="Stop server")

    def _set_state(self, state: str, text: str) -> None:
        colours = {"running": (theme.OK, "#8ff0c8", "Stop server"),
                   "stopped": (theme.DANGER, "#ffb4b4", "Start server"),
                   "error": (theme.DANGER, "#ffb4b4", "Start server")}
        dot, fg, btn = colours.get(state, (theme.WARN, "#ffd9a0", "Start server"))
        self.dot.set_colour(dot)
        self.lbl_state.configure(text=text, foreground=fg)
        self.btn_power.configure(text=btn)

    def set_status(self, text: str) -> None:
        self.var_status.set(text)

    # -- logging -----------------------------------------------------------
    def _enqueue_log(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {line}")

    def _log(self, text: str) -> None:
        self._enqueue_log(f"[console] {text}")

    def _pump_log(self) -> None:
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

    # -- data refresh ------------------------------------------------------
    def refresh_all(self) -> None:
        if not self.db:
            return
        self.refresh_sheet()
        self.refresh_regs()
        self.refresh_overview()

    def refresh_overview(self) -> None:
        if not self.db:
            return
        stats = self.db.stats()
        self.stat_labels["registrations"].configure(text=str(stats["total_registrations"]))
        self.stat_labels["students"].configure(text=str(stats["unique_students"]))
        self.stat_labels["filled"].configure(
            text=f"{stats['seats_filled']}/{stats['seats_total']}")
        self.stat_labels["full"].configure(text=str(stats["full_courses"]))
        self._render_bars(self.db.courses())

    def _render_bars(self, courses: list[dict]) -> None:
        for child in self.bars_frame.winfo_children():
            child.destroy()
        for c in courses:
            row = tk.Frame(self.bars_frame, background=theme.CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"{c['code']}", background=theme.CARD, foreground=theme.INK,
                     font=(self.fam, 9, "bold"), width=8, anchor="w").pack(side="left")
            tk.Label(row, text=c["title"][:34], background=theme.CARD,
                     foreground=theme.MUTED, font=(self.fam, 9), width=36,
                     anchor="w").pack(side="left")
            pct = c["enrolled"] / c["capacity"] if c["capacity"] else 0
            colour = theme.DANGER if c["is_full"] else (
                theme.WARN if pct >= 0.8 else theme.PRIMARY)
            bar = ttk.Progressbar(row, style="TProgressbar", maximum=100,
                                  value=pct * 100, length=260)
            bar.pack(side="left", padx=10)
            tk.Label(row, text=f"{c['enrolled']}/{c['capacity']}",
                     background=theme.CARD, foreground=colour,
                     font=(self.fam, 9, "bold"), width=8, anchor="e").pack(side="left")

    def refresh_sheet(self) -> None:
        if not self.db:
            return
        self.tree_sheet.delete(*self.tree_sheet.get_children())
        courses = self.db.courses()
        for c in courses:
            tag = ("full",) if c["is_full"] else (("low",) if c["seats_left"] <= 3 else ())
            self.tree_sheet.insert("", "end", tags=tag, values=(
                c["code"], c["title"], c["instructor"], c["department"],
                f"{c['day']} {c['start']}-{c['end']}", c["venue"],
                c["capacity"], c["enrolled"]))
        self.lbl_sheet_count.configure(text=f"{len(courses)} courses")

    def refresh_regs(self) -> None:
        if not self.db:
            return
        rows = self.db.registrations(q=self.var_reg_search.get().strip())
        self.tree_regs.delete(*self.tree_regs.get_children())
        for r in rows:
            email = r["email"] if len(r["email"]) <= 26 else r["email"][:25] + "\u2026"
            self.tree_regs.insert("", "end", iid=str(r["id"]), values=(
                r["id"], r["course_code"], r["title"], r["student_id"],
                r["student_name"], email,
                f"{r['day']} {r['start']}-{r['end']}", r["created_at"][:16]))
        self.lbl_regs_count.configure(
            text=f"{len(rows)} registration{'s' if len(rows) != 1 else ''}")

    def _auto_refresh(self) -> None:
        try:
            self.refresh_overview()
            if self.nb.index(self.nb.select()) in (1, 2):
                self.refresh_all()
        finally:
            self.after(2000, self._auto_refresh)

    # -- actions -----------------------------------------------------------
    def reload_courses(self) -> None:
        if not self.db:
            return
        n = self.db.sync_courses()
        self._log(f"course sheet reloaded from courses.csv ({n} rows)")
        self.refresh_all()
        self.set_status(f"Course sheet reloaded: {n} courses.")

    def cancel_selected(self) -> None:
        if not self.db:
            return
        sel = self.tree_regs.selection()
        if not sel:
            messagebox.showinfo("Nothing selected", "Select a registration row first.")
            return
        reg_id = int(sel[0])
        values = self.tree_regs.item(sel[0], "values")
        if not messagebox.askyesno("Cancel registration",
                                   f"Cancel registration #{reg_id} ({values[1]} - {values[4]})?"):
            return
        try:
            self.db.cancel(reg_id)
        except srv.ApiError as err:
            messagebox.showerror("Could not cancel", err.message)
            return
        self._log(f"registration #{reg_id} cancelled from console")
        self.set_status(f"Registration #{reg_id} cancelled.")
        self.refresh_all()

    def export_csv(self) -> None:
        if not self.db:
            return
        rows = self.db.registrations(q=self.var_reg_search.get().strip())
        if not rows:
            messagebox.showinfo("Nothing to export", "There are no registrations to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export registrations", defaultextension=".csv",
            initialfile="registrations.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        self._log(f"exported {len(rows)} registrations to {path}")
        self.set_status(f"Exported {len(rows)} registrations to {os.path.basename(path)}")
        messagebox.showinfo("Export complete", f"{len(rows)} registrations written to\n{path}")

    # -- close -------------------------------------------------------------
    def _on_close(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.destroy()


def main() -> None:
    ap = argparse.ArgumentParser(description="Class Registration - server console")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="insert sample registrations")
    args = ap.parse_args()
    if args.demo:
        srv.CourseDB(srv.DEFAULT_DB, srv.DEFAULT_COURSES)   # make sure schema exists
    ServerConsole(args.host, args.port, args.demo).mainloop()


if __name__ == "__main__":
    main()
