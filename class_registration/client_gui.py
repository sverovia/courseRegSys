#!/usr/bin/env python3
"""
client_gui.py - Class Registration System: client side (version 2).

============================================================================
WHAT THIS FILE IS
============================================================================

The student's desktop program, written with Tkinter (part of the standard
library) so nothing has to be installed.  It is the "client" half of the
project: it never touches the database, it only calls the JSON API that
`server.py` exposes.

The window has three working areas:

    +---------------------------------------------------------------+
    |  header:  title      term selector      server address  status |
    +---------------------------------+-----------------------------+
    |  COURSE SHEET                   |  REGISTER FOR A COURSE      |
    |  - hash-index search box        |  - section + student form   |
    |  - results table                |  - confirmation / errors    |
    |                                 +-----------------------------+
    |                                 |  MY TIMETABLE               |
    |                                 |  - weekly grid (Canvas)     |
    |                                 |  - click a block to drop it |
    +---------------------------------+-----------------------------+
    |  status bar                                                    |
    +---------------------------------------------------------------+

============================================================================
THE THREE FEATURES THAT ARE NEW IN VERSION 2
============================================================================

1. TERM / YEAR SELECTOR (top right of the header)
   The server can hold several terms at once.  The dropdown asks the server
   for `/api/terms` and every later request carries the chosen `?year=&term=`,
   so the course list *and* the timetable always describe the same term.

2. HASH-TABLE SEARCH (the search box above the table)
   See the `SearchIndex` class below.  Instead of scanning the course list for
   every keystroke, an inverted index `{word: {section ids}}` is built once per
   term.  A query then costs a few dictionary lookups plus a set
   intersection, which is why the status line can show a millisecond figure.

3. CLICKABLE WEEKLY TIMETABLE (bottom right)
   The "my registrations" list of version 1 is replaced by a timetable drawn on
   a Tkinter Canvas: days across, half-hour rows down.  Each registered section
   is a coloured block; clicking a block selects it and the "Drop selected"
   button cancels that registration on the server.

============================================================================
RUNNING IT
============================================================================

    python3 client_gui.py                       # talks to 127.0.0.1:8000
    python3 client_gui.py --server http://host:8000
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports only.
# ---------------------------------------------------------------------------
import argparse                     # --server flag
import json                         # request bodies and saved settings
import os                           # settings file path
import queue                        # results coming back from worker threads
import threading                    # so the window never freezes on HTTP
import time                         # measuring how fast the hash search is
import tkinter as tk                # the GUI toolkit
import urllib.error                 # HTTP errors (404, 409, ...)
import urllib.parse                 # quoting query parameters
import urllib.request               # plain HTTP requests
from tkinter import messagebox, ttk

import theme                        # shared colours / widget styles

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "data", "client_settings.json")
DEFAULT_SERVER = "http://127.0.0.1:8000"

# Colours used for the timetable blocks.  A course code is mapped onto one of
# these by a stable hash, so CS101 is always the same colour on every redraw.
BLOCK_COLOURS = (
    ("#dfe7ff", "#22357a"), ("#e2f7ef", "#0b6b4c"), ("#fdeee0", "#8a4b00"),
    ("#f3e6ff", "#5b2b8a"), ("#e0f4fb", "#0b5a72"), ("#ffe6ef", "#8a1f45"),
    ("#eef7d9", "#4a6410"), ("#e8e9f5", "#333a63"),
)

# Days shown in the timetable.  Saturday and Sunday are added automatically
# when the student actually has a class that day.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
ALL_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# The timetable draws one row per 30 minutes.
SLOT = 30


def hhmm_to_minutes(value: str) -> int:
    """'09:30' -> 570.  Small helper used by the timetable maths."""
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute)


def minutes_to_hhmm(value: int) -> str:
    """570 -> '09:30'.  The inverse of the helper above."""
    return f"{value // 60:02d}:{value % 60:02d}"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """A failure from the server (or from the network) with a code attached.

    The GUI uses `code` to decide *where* to show a message: validation
    problems belong next to the form, connection problems belong in the header
    and the status bar.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiClient:
    """All calls to server.py, one method per endpoint.

    Every method returns plain Python data (lists / dictionaries) so the GUI
    code never sees JSON or HTTP details.
    """

    def __init__(self, base_url: str = DEFAULT_SERVER):
        # Strip any trailing slash so paths can simply be appended.
        self.base_url = base_url.rstrip("/")

    # -- plumbing ----------------------------------------------------------
    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Perform one request and translate failures into ApiError.

        urllib raises HTTPError for 4xx/5xx, which still carries a JSON body -
        the server always sends {"error", "code"}, so the message shown to the
        user is written by the server, not invented here.
        """
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            # The body of an error response is still JSON in this API.
            raw = err.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            raise ApiError(err.code, body.get("code", "HTTP_ERROR"),
                           body.get("error", f"Server returned HTTP {err.code}")) from None
        except urllib.error.URLError as err:
            # Server not running / wrong address - a very common situation, so
            # the message tells the user exactly what to do about it.
            raise ApiError(0, "NO_SERVER",
                           f"Cannot reach the server at {self.base_url}.\n"
                           f"Start it with:  python3 server_gui.py\n({err.reason})"
                           ) from None
        except TimeoutError:
            raise ApiError(0, "TIMEOUT", "The server did not answer in time.") from None

    # -- endpoints ---------------------------------------------------------
    def terms(self) -> list[dict]:
        """Every (year, term) the server knows about, for the selector."""
        return self._call("GET", "/api/terms")["terms"]

    def courses(self, year: int, term: int) -> list[dict]:
        """The course sheet for one term (default term when values are 0)."""
        query = f"?year={year}&term={term}" if year and term else ""
        return self._call("GET", "/api/courses" + query)["courses"]

    def student(self, student_id: str) -> dict | None:
        """Look a student up in the server's dictionary (None when unknown)."""
        try:
            return self._call("GET", f"/api/students/{urllib.parse.quote(student_id)}")
        except ApiError as err:
            if err.code == "NO_SUCH_STUDENT":
                return None          # not an error, just a new student
            raise

    def register(self, section_id: str, student_id: str, student_name: str,
                 email: str, notes: str = "") -> dict:
        """Create a registration.  The server applies every rule."""
        return self._call("POST", "/api/registrations", {
            "section_id": section_id, "student_id": student_id,
            "student_name": student_name, "email": email, "notes": notes,
        })["registration"]

    def registrations(self, student_id: str = "", year: int = 0,
                      term: int = 0) -> list[dict]:
        """Registrations for a student (optionally limited to one term)."""
        params = {"student_id": student_id}
        if year and term:
            params.update(year=year, term=term)
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        return self._call("GET", f"/api/registrations?{query}")["registrations"]

    def cancel(self, registration_id: int) -> dict:
        """Remove one registration by its id."""
        return self._call("DELETE", f"/api/registrations/{registration_id}")


# ---------------------------------------------------------------------------
# The hash table that powers the search box
# ---------------------------------------------------------------------------
class SearchIndex:
    """An inverted index (a dictionary of sets) over one term's sections.

    WHY THIS EXISTS
    ---------------
    The brief asks for a search system built on a dictionary / hash table.
    Scanning every course for every keystroke is O(n) per character.  Instead
    the index is built once when a term is loaded:

        index["mon"]   = {"2026-T1-CS101-A", "2026-T1-CS101-B", ...}
        index["cs101"] = {"2026-T1-CS101-A", "2026-T1-CS101-B", "2026-T1-CS101-C"}
        index["science hall"] = {...}

    A query is then split into words, each word is a dictionary lookup (O(1)
    on average), and the resulting sets are intersected.  That makes searching
    proportional to the number of matches rather than the size of the sheet.

    FIELDS
    ------
    index     : dict[str, set[str]]  word -> set of section ids
    tokens    : dict[str, dict]      section id -> the course itself (for output)
    prefix    : dict[str, set[str]]  first two letters -> section ids, so the
                                     user gets partial matches while typing
    built_at  : float                timestamp, shown in the status bar
    """

    # Fields of a course that should be searchable.  Adding a field here makes
    # it searchable everywhere, because tokens are generated from this tuple.
    SEARCHABLE = ("code", "section", "title", "instructor", "venue",
                  "department", "day", "label")

    def __init__(self) -> None:
        self.index: dict[str, set[str]] = {}
        self.tokens: dict[str, dict] = {}
        self.prefix: dict[str, set[str]] = {}
        self.built_at = 0.0
        self.size = 0

    # -- building ----------------------------------------------------------
    @staticmethod
    def _words(course: dict) -> set[str]:
        """Break one course record into lower-case searchable words.

        Multi-word values such as "Science Hall R101" are split on spaces,
        commas, dashes and slashes, so searching for "hall" or "r101" both
        work.  The full code with and without the section letter is included
        ("cs101", "cs101-b").
        """
        words: set[str] = set()
        for field in SearchIndex.SEARCHABLE:
            raw = str(course.get(field, "")).lower()
            for chunk in raw.replace(",", " ").replace("-", " ").replace("/", " ").split():
                words.add(chunk)
        # Also index the code+section pair and the joined title as a phrase.
        words.add(f"{course['code'].lower()}{course['section'].lower()}")
        words.add(f"{course['code'].lower()} {course['section'].lower()}")
        return words

    def build(self, courses: list[dict]) -> None:
        """(Re)build the index from scratch for the courses of one term.

        Cost is O(total words), paid once per term instead of once per
        keystroke.  `tokens` keeps the course records so a result set of ids can
        be turned back into rows instantly.
        """
        self.index.clear()
        self.prefix.clear()
        self.tokens = {c["section_id"]: c for c in courses}
        self.size = len(self.tokens)

        for course in courses:
            section_id = course["section_id"]
            for word in self._words(course):
                # bucket for the whole word
                self.index.setdefault(word, set()).add(section_id)
                # buckets for its one- and two-letter prefixes
                for length in (1, 2):
                    if len(word) >= length:
                        self.prefix.setdefault(word[:length], set()).add(section_id)
        self.built_at = time.perf_counter()
        # Keep the memory report tidy: the number of distinct keys is what makes
        # this a hash table worth having.
        self.key_count = len(self.index)

    # -- querying ----------------------------------------------------------
    def search(self, query: str) -> tuple[list[dict], float]:
        """Return (matching courses, seconds spent).

        Strategy:
            * empty query  -> everything (an empty set means "no filter")
            * every word   -> must match (intersection), compared exactly first
            * if a word matched nothing exactly, fall back to its two-letter
              prefix bucket so "prog" still finds "programming" while typing.
        """
        started = time.perf_counter()
        text = (query or "").strip().lower()

        if not text:
            # No filter: hand back every section, still in course order so the
            # table does not jump about between refreshes.
            everything = sorted(self.tokens.values(),
                                key=lambda c: (c["code"], c["section"], c["day"]))
            return everything, time.perf_counter() - started

        words = [w for w in text.replace("-", " ").split() if w]
        result: set[str] | None = None

        for word in words:
            exact = self.index.get(word)
            if exact is None:
                # No exact bucket: use the prefix table (partial word).
                exact = set()
                for key, bucket in self.prefix.items():
                    if key == word[:2]:
                        exact |= bucket
                # Last resort: keep only ids whose course really contains it.
                exact = {sid for sid in exact
                         if any(word in str(self.tokens[sid].get(f, "")).lower()
                                for f in self.SEARCHABLE)}
            if result is None:
                result = set(exact)
            else:
                result &= exact            # intersection: all words must match
            if not result:
                break

        matches = [self.tokens[sid] for sid in (result or set())]
        # Keep a predictable order: by course code, then section, then day.
        matches.sort(key=lambda c: (c["code"], c["section"], c["day"]))
        return matches, time.perf_counter() - started


# ---------------------------------------------------------------------------
# The weekly timetable widget
# ---------------------------------------------------------------------------
class Timetable(tk.Frame):
    """A weekly timetable drawn on a Canvas.

    Days run across the top, half-hour slots down the side, and every
    registration becomes a coloured block.  The widget does not talk to the
    server: the parent hands it the registration rows and is told back, through
    the callbacks, when a block is clicked.
    """

    def __init__(self, parent: tk.Misc, font_family: str,
                 on_select=None, on_open=None):
        super().__init__(parent, background=theme.CARD)
        self.family = font_family
        self.on_select = on_select        # called with a registration or None
        self.on_open = on_open            # called on double-click

        self.rows: list[dict] = []        # registrations currently drawn
        self.selected: dict | None = None # the block the user clicked
        self.blocks: dict[str, int] = {}  # registration id -> canvas item tag

        # The canvas fills the widget; it is redrawn whenever the size changes,
        # so the grid always fits no matter how the window is resized.
        self.canvas = tk.Canvas(self, background=theme.CARD, highlightthickness=0,
                                bd=0, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)

    # -- data --------------------------------------------------------------
    def set_rows(self, rows: list[dict]) -> None:
        """Replace the timetable contents (called after every refresh)."""
        self.rows = rows
        self.selected = None
        self.redraw()
        if self.on_select:
            self.on_select(None)

    def clear_selection(self) -> None:
        """Un-highlight the current block."""
        self.selected = None
        self.redraw()
        if self.on_select:
            self.on_select(None)

    # -- geometry helpers --------------------------------------------------
    def _days(self) -> tuple[str, ...]:
        """Which day columns to draw: Mon-Fri, plus Sat/Sun when used.

        Weekend columns are only added when the student actually has a weekend
        class, which keeps the weekday grid wide and readable for everybody
        else.
        """
        used = {r["day"] for r in self.rows}
        extras = tuple(d for d in ALL_DAYS[5:] if d in used)
        return WEEKDAYS + extras

    # -- geometry helpers --------------------------------------------------
    # Everything in the timetable is expressed in "minutes after midnight".
    # That turns an overlap test into a simple comparison and makes the
    # conversion to canvas pixels a single division by the slot height.
    def _window(self) -> tuple[int, int]:
        """The vertical time window in minutes.

        Built from the earliest start and latest end of the registrations,
        rounded outwards to the hour and never shorter than 08:00-16:00, so a
        single class does not produce a stretched, unreadable grid.
        """
        if not self.rows:
            return 8 * 60, 16 * 60
        first = min(hhmm_to_minutes(r["start"]) for r in self.rows)
        last = max(hhmm_to_minutes(r["end"]) for r in self.rows)
        # Never show a grid that starts later than 09:00 or ends before 15:00,
        # otherwise a single morning class would be stretched over the whole
        # canvas and look wrong.
        first = min(first, 9 * 60)
        last = max(last, 15 * 60)
        # Round outwards to whole hours so the time labels read cleanly.
        return (first // 60) * 60, -(-last // 60) * 60

    # -- drawing -----------------------------------------------------------
    def redraw(self) -> None:
        """Repaint the whole grid.  Cheap enough to call on every resize.

        A full redraw (delete("all") then draw again) is used deliberately: a
        term has at most a few dozen blocks, so this is faster to reason about
        than trying to patch individual canvas items.
        """
        canvas = self.canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 60 or height < 60:
            return                                # not laid out yet

        days = self._days()
        start, end = self._window()
        slots = max((end - start) // SLOT, 4)

        # Layout: a gutter for the time labels, a header strip for the days.
        gutter = 44
        header = 20
        grid_w = width - gutter - 4
        grid_h = height - header - 4
        col_w = grid_w / len(days)
        row_h = grid_h / slots

        # ---- day headers ----
        for i, day in enumerate(days):
            x = gutter + i * col_w
            canvas.create_rectangle(x, 0, x + col_w, header,
                                   fill="#eef1f8", outline=theme.LINE)
            canvas.create_text(x + col_w / 2, header / 2, text=day,
                               font=(self.family, 8, "bold"), fill=theme.MUTED)

        # ---- hour lines and time labels ----
        for slot in range(slots + 1):
            y = header + slot * row_h
            minute = start + slot * SLOT
            on_hour = minute % 60 == 0
            canvas.create_line(gutter, y, width, y,
                               fill=theme.LINE if on_hour else "#f0f3f9")
            if on_hour:
                canvas.create_text(gutter - 6, y, text=minutes_to_hhmm(minute),
                                   anchor="e", font=(self.family, 7),
                                   fill=theme.MUTED)

        # ---- vertical separators ----
        for i in range(len(days) + 1):
            x = gutter + i * col_w
            canvas.create_line(x, 0, x, height, fill=theme.LINE)

        # ---- the blocks ----
        # A block's vertical position comes straight from its start/end time:
        # (start - window_start) / SLOT rows down, and its height is the number
        # of half-hour rows it spans.  Nothing is positioned by hand.
        self.blocks.clear()
        for reg in self.rows:
            if reg["day"] not in days:
                continue
            column = days.index(reg["day"])
            top = header + (hhmm_to_minutes(reg["start"]) - start) / SLOT * row_h
            bottom = header + (hhmm_to_minutes(reg["end"]) - start) / SLOT * row_h
            x0 = gutter + column * col_w + 2
            x1 = gutter + (column + 1) * col_w - 2

            # Stable colour: the same course code always gets the same pair.
            fill, ink = BLOCK_COLOURS[sum(map(ord, reg["code"])) % len(BLOCK_COLOURS)]
            # The selected block gets a thick outline plus a small marker so it
            # is obvious which class the "Drop selected" button will remove.
            chosen = self.selected and self.selected["id"] == reg["id"]
            tag = f"block-{reg['id']}"

            canvas.create_rectangle(
                x0, top, x1, bottom, fill=fill, outline=ink,
                width=3 if chosen else 1, tags=(tag, "block"))
            if chosen:
                canvas.create_rectangle(
                    x1 - 9, top + 3, x1 - 3, top + 9, fill=ink, outline=ink,
                    tags=(tag, "block"))
            # Two short lines fit in almost any block height.
            canvas.create_text(
                x0 + 5, top + 5, anchor="nw", tags=(tag, "block"),
                text=f"{reg['code']}-{reg['section']}",
                font=(self.family, 8, "bold"), fill=ink,
                width=max(x1 - x0 - 10, 20))
            # The second line is only drawn when the block is tall enough to
            # hold it (two short rows); it is a single, truncated line so the
            # text can never spill outside the rectangle.
            if bottom - top > 36:
                venue = reg["venue"]
                if len(venue) > 20:
                    venue = venue[:19] + "\u2026"
                canvas.create_text(
                    x0 + 5, top + 18, anchor="nw", tags=(tag, "block"),
                    text=f"{reg['start']}  {venue}",
                    font=(self.family, 7), fill=ink)
            self.blocks[str(reg["id"])] = column

        # ---- friendly empty state ----
        if not self.rows:
            canvas.create_text(
                width / 2, height / 2,
                text="No classes yet for this term.\n"
                     "Register for a course and it appears here.",
                justify="center", font=(self.family, 9), fill=theme.MUTED)

    # -- interaction -------------------------------------------------------
    def _on_click(self, event) -> None:
        """Find the block under the pointer and notify the parent.

        Every item belonging to a registration carries a "block-<id>" tag, so
        clicking either the rectangle or its text works, and the id can be read
        back without keeping a separate geometry table.
        """
        # `find_overlapping` returns every item at that point; the tags tell us
        # which registration it belongs to.
        for item in self.canvas.find_overlapping(event.x, event.y, event.x, event.y):
            for tag in self.canvas.gettags(item):
                if tag.startswith("block-"):
                    reg_id = int(tag.split("-", 1)[1])
                    self.selected = next((r for r in self.rows
                                          if r["id"] == reg_id), None)
                    self.redraw()
                    if self.on_select:
                        self.on_select(self.selected)
                    return
        # Clicking empty space clears the selection.
        self.clear_selection()


# ---------------------------------------------------------------------------
# The main window
# ---------------------------------------------------------------------------
class RegistrationClient(tk.Tk):
    """The whole client application.

    One class holds the window, the widgets and the small amount of state:
    the chosen term, the loaded courses, the selected section and the
    registration rows shown in the timetable.
    """

    def __init__(self, server: str = DEFAULT_SERVER):
        super().__init__()
        # theme.apply() sets every ttk style and returns the font family to use
        # for the classic tk widgets (which ttk styles cannot reach).
        self.family = theme.apply(self)
        self.title("Class Registration - Student Client")
        self.geometry("1280x820")
        self.minsize(1120, 720)

        # ---- state ----
        self.api = ApiClient(server)
        self.terms: list[dict] = []          # from /api/terms
        self.year = 0                        # currently selected term
        self.term = 0
        self.courses: list[dict] = []        # sections of the selected term
        self.index = SearchIndex()           # the hash table for the search box
        self.selected: dict | None = None    # section picked in the table
        self.my_rows: list[dict] = []        # registrations of the student
        self.results: queue.Queue = queue.Queue()   # worker-thread results
        self._msg_section: str | None = None # section the form message is about

        self._load_settings()
        self._build_header()
        self._build_body()
        self._build_statusbar()

        # Start by asking the server which terms exist.
        self.after(120, self.load_terms)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Saved settings (server address, last term, student details)
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        """Read data/client_settings.json if it exists."""
        self.settings = {
            "server": self.api.base_url, "student_id": "", "student_name": "",
            "email": "", "year": 0, "term": 0,
        }
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as handle:
                self.settings.update(json.load(handle))
                self.api = ApiClient(self.settings["server"])
        except (OSError, ValueError):
            pass                          # first run - defaults are fine

    def _save_settings(self) -> None:
        """Persist the current choices so the next start is quicker."""
        self.settings.update(
            server=self.api.base_url,
            student_id=self.var_sid.get().strip(),
            student_name=self.var_name.get().strip(),
            email=self.var_email.get().strip(),
            year=self.year, term=self.term)
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
                json.dump(self.settings, handle, indent=2)
        except OSError:
            pass                          # never break the app over settings

    # ------------------------------------------------------------------
    # Header: title, term selector, server address, connection light
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        bar = tk.Frame(self, background=theme.HEADER, height=88)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # ---- left: product name ----
        left = tk.Frame(bar, background=theme.HEADER)
        left.pack(side="left", padx=22, pady=14)
        tk.Label(left, text="Course Registration", background=theme.HEADER,
                 foreground="#ffffff", font=(self.family, 17, "bold")).pack(anchor="w")
        tk.Label(left, text="Student client  -  choose a term, search the sheet, "
                            "register, see your week",
                 background=theme.HEADER, foreground=theme.HEADER_SUB,
                 font=(self.family, 9)).pack(anchor="w")

        # ---- right: term selector and server box ----
        right = tk.Frame(bar, background=theme.HEADER)
        right.pack(side="right", padx=22)

        tk.Label(right, text="TERM / YEAR", background=theme.HEADER,
                 foreground=theme.HEADER_SUB, font=(self.family, 8, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2))

        # The combobox starts empty and is filled from the server's answer to
        # /api/terms; `readonly` stops free typing of an invalid term.
        self.var_term = tk.StringVar(value="loading ...")
        self.cmb_term = ttk.Combobox(right, textvariable=self.var_term,
                                     state="readonly", width=22,
                                     font=(self.family, 10))
        self.cmb_term.grid(row=1, column=0, sticky="w")
        self.cmb_term.bind("<<ComboboxSelected>>", self._on_term_change)

        # Server address + connect button, same pattern as version 1.
        tk.Label(right, text="SERVER", background=theme.HEADER,
                 foreground=theme.HEADER_SUB, font=(self.family, 8, "bold")).grid(
            row=0, column=1, sticky="w", padx=(16, 0), pady=(0, 2))
        self.var_server = tk.StringVar(value=self.api.base_url)
        entry = tk.Entry(right, textvariable=self.var_server, width=26, relief="flat",
                         background="#2c3a61", foreground="#ffffff",
                         insertbackground="#ffffff", font=(self.family, 10),
                         highlightthickness=1, highlightbackground="#43548a",
                         highlightcolor=theme.HEADER_SUB)
        entry.grid(row=1, column=1, padx=(16, 0), ipady=4)
        entry.bind("<Return>", lambda _e: self.connect())
        self.btn_connect = ttk.Button(right, text="Connect", style="Ghost.TButton",
                                      command=self.connect)
        self.btn_connect.grid(row=1, column=2, padx=(8, 0))

        # Connection indicator: a coloured dot plus a short label.
        self.dot = theme.status_dot(right, theme.MUTED, bg=theme.HEADER)
        self.dot.grid(row=1, column=3, padx=(10, 0))
        self.lbl_conn = tk.Label(right, text="not connected", background=theme.HEADER,
                                 foreground=theme.HEADER_SUB, font=(self.family, 8))
        self.lbl_conn.grid(row=2, column=0, columnspan=4, sticky="w", pady=(3, 0))

    # ------------------------------------------------------------------
    # Body: course sheet on the left, form + timetable on the right
    # ------------------------------------------------------------------
    def _build_body(self) -> None:
        body = tk.Frame(self, background=theme.BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)
        # Two columns: the sheet takes three fifths of the width, the right
        # hand side keeps a usable minimum so the timetable never collapses.
        body.columnconfigure(0, weight=3, minsize=620)
        body.columnconfigure(1, weight=2, minsize=460)
        body.rowconfigure(0, weight=1)

        self._build_course_panel(body)
        self._build_side_panel(body)

    # ---- left: the course sheet + hash search ---------------------------
    def _build_course_panel(self, parent: tk.Misc) -> None:
        outer = theme.card(parent)
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        card = outer.inner

        # Heading row: title on the left, term summary on the right.
        head = tk.Frame(card, background=theme.CARD)
        head.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(head, text="Course Sheet", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(side="left")
        self.lbl_sheet_info = tk.Label(head, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_sheet_info.pack(side="right")

        # ---- the search bar --------------------------------------------
        search_row = tk.Frame(card, background=theme.CARD)
        search_row.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(search_row, text="SEARCH", background=theme.CARD,
                 foreground=theme.MUTED, font=(self.family, 8, "bold")).pack(
            side="left", padx=(0, 8))
        self.var_search = tk.StringVar()
        # `trace_add` fires on every keystroke, which is exactly what a hash
        # index is for: the query is cheap enough to run live.
        self.var_search.trace_add("write", lambda *_: self._render_courses())
        self.ent_search = ttk.Entry(search_row, textvariable=self.var_search)
        self.ent_search.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="Clear", style="Ghost.TButton",
                   command=lambda: self.var_search.set("")).pack(side="left", padx=(6, 0))
        self.var_hide_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_row, text="Hide full", variable=self.var_hide_full,
                        command=self._render_courses,
                        style="Card.TCheckbutton").pack(side="left", padx=(10, 0))

        # A second line explains the search syntax and reports its speed.
        hint = tk.Frame(card, background=theme.CARD)
        hint.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(hint, text="Try: cs101, programming, mon, science hall, lam",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 8)).pack(side="left")
        self.lbl_index = tk.Label(hint, text="", background=theme.CARD,
                                  foreground=theme.PRIMARY_DARK,
                                  font=(self.family, 8, "bold"))
        self.lbl_index.pack(side="right")

        # ---- the results table -----------------------------------------
        table = tk.Frame(card, background=theme.CARD)
        table.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        columns = ("label", "title", "time", "venue", "places")
        self.tree = ttk.Treeview(table, columns=columns, show="headings",
                                 selectmode="browse")
        # Only the title column stretches; the fixed ones always stay visible.
        spec = {"label": ("Code", 72, "w"), "title": ("Course title", 110, "w"),
                "time": ("Time", 130, "w"), "venue": ("Venue", 164, "w"),
                "places": ("Places", 70, "center")}
        for key, (text, width, anchor) in spec.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=(key == "title"))
        # Rows for full sections are grey; nearly full ones are amber.
        self.tree.tag_configure("full", foreground="#9aa5b8")
        self.tree.tag_configure("low", foreground=theme.WARN)
        self.tree.bind("<<TreeviewSelect>>", self._on_course_select)
        self.tree.bind("<Double-1>", lambda _e: self.ent_name.focus_set())
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        # Footer: legend on the left, refresh on the right.
        legend = tk.Frame(card, background=theme.CARD)
        legend.pack(fill="x", padx=18, pady=(6, 14))
        tk.Label(legend, text="Grey = full   /   amber = 3 places or fewer   /   "
                              "several sessions of one course share its code",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 8)).pack(side="left")
        ttk.Button(legend, text="Refresh", style="Ghost.TButton",
                   command=self.refresh_courses).pack(side="right")

    # ---- right: registration form + timetable ---------------------------
    def _build_side_panel(self, parent: tk.Misc) -> None:
        side = tk.Frame(parent, background=theme.BG)
        side.grid(row=0, column=1, sticky="nsew")
        side.rowconfigure(1, weight=1)     # the timetable gets the spare space
        side.columnconfigure(0, weight=1)

        self._build_form_card(side)
        self._build_timetable_card(side)

    def _build_form_card(self, parent: tk.Misc) -> None:
        """The registration form: chosen section, student details, submit."""
        outer = theme.card(parent)
        outer.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        card = outer.inner

        tk.Label(card, text="Register for a course", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(
            anchor="w", padx=18, pady=(14, 2))

        # The chosen section is described here, so the user can double-check
        # *which session* they are about to join.
        self.lbl_sel_title = tk.Label(card, text="No course selected",
                                      background=theme.CARD,
                                      foreground=theme.PRIMARY_DARK,
                                      font=(self.family, 11, "bold"), anchor="w",
                                      justify="left", wraplength=380)
        self.lbl_sel_title.pack(fill="x", padx=18, pady=(6, 0))
        self.lbl_sel_meta = tk.Label(card, text="Pick a row in the course sheet to begin.",
                                     background=theme.CARD, foreground=theme.MUTED,
                                     font=(self.family, 9), anchor="w",
                                     justify="left", wraplength=380)
        self.lbl_sel_meta.pack(fill="x", padx=18, pady=(2, 8))

        # ---- the form fields ----
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
        for row, (label, var, _hint) in enumerate(rows):
            tk.Label(form, text=label, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.family, 9, "bold")).grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            entry = ttk.Entry(form, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            if label == "Student ID *":
                self.ent_sid = entry
                # Leaving the id field triggers the student dictionary lookup:
                # a returning student gets their name and e-mail filled in, and
                # their timetable loaded, without retyping anything.
                entry.bind("<FocusOut>", lambda _e: self.lookup_student())
                entry.bind("<Return>", lambda _e: self.lookup_student())
            elif label == "Full name *":
                self.ent_name = entry
        self.var_notes.set("")

        # ---- submit + feedback ----
        self.btn_register = ttk.Button(card, text="Register", style="Primary.TButton",
                                       command=self.do_register)
        self.btn_register.pack(fill="x", padx=18, pady=(12, 6))
        self.lbl_form_msg = tk.Label(card, text="", background=theme.CARD,
                                     foreground=theme.MUTED, font=(self.family, 9),
                                     anchor="w", justify="left", wraplength=380)
        self.lbl_form_msg.pack(fill="x", padx=18, pady=(0, 12))

    def _build_timetable_card(self, parent: tk.Misc) -> None:
        """The weekly timetable that replaces the old registrations list."""
        outer = theme.card(parent)
        outer.grid(row=1, column=0, sticky="nsew")
        card = outer.inner
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)

        # ---- heading ----
        head = tk.Frame(card, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        tk.Label(head, text="My timetable", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(side="left")
        self.lbl_term_badge = tk.Label(head, text="", background=theme.CARD,
                                       foreground=theme.MUTED,
                                       font=(self.family, 9))
        self.lbl_term_badge.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.load_my_registrations).pack(side="right")

        # ---- explanation / selection line ----
        self.lbl_tt_hint = tk.Label(
            card, text="Enter a student ID to see their week.",
            background=theme.CARD, foreground=theme.MUTED, font=(self.family, 9),
            anchor="w", justify="left", wraplength=420)
        self.lbl_tt_hint.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=18, pady=(0, 6))

        # ---- the canvas widget itself ----
        self.timetable = Timetable(card, self.family, on_select=self._on_block_select)
        self.timetable.grid(row=2, column=0, columnspan=2, sticky="nsew",
                            padx=18, pady=(0, 6))

        # ---- footer: count on the left, drop button on the right ----
        foot = tk.Frame(card, background=theme.CARD)
        foot.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 14))
        self.lbl_tt_count = tk.Label(foot, text="", background=theme.CARD,
                                     foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_tt_count.pack(side="left")
        self.btn_drop = ttk.Button(foot, text="Drop selected", style="Danger.TButton",
                                   command=self.do_drop, state="disabled")
        self.btn_drop.pack(side="right")

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, background="#dfe5f2", height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.var_status = tk.StringVar(value="Ready.")
        tk.Label(bar, textvariable=self.var_status, background="#dfe5f2",
                 foreground=theme.MUTED, font=(self.family, 9), anchor="w").pack(
            side="left", padx=16)
        self.lbl_stats = tk.Label(bar, text="", background="#dfe5f2",
                                  foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_stats.pack(side="right", padx=16)

    def set_status(self, text: str) -> None:
        """Write a line into the status bar."""
        self.var_status.set(text)

    # ------------------------------------------------------------------
    # Worker threads
    #
    # Tkinter must only be touched from the main thread, so network calls run
    # in a short-lived worker and post their result into a queue that the main
    # thread drains a few milliseconds later.
    # ------------------------------------------------------------------
    def _run(self, work, on_done, on_error=None) -> None:
        """Run `work()` in a thread, then call `on_done(result)` on success."""
        def runner() -> None:
            try:
                result = work()
            except ApiError as err:
                self.results.put(("error", on_error, err))
            except Exception as err:                       # noqa: BLE001
                self.results.put(("error", on_error,
                                  ApiError(0, "CLIENT_ERROR", str(err))))
            else:
                self.results.put(("ok", on_done, result))

        threading.Thread(target=runner, daemon=True).start()
        self.after(60, self._drain)

    def _drain(self) -> None:
        """Apply everything the workers have produced since the last check."""
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
        """Default error handling when a call did not pass its own handler."""
        if callback:
            callback(err)
            return
        if err.code == "NO_SERVER":
            self._set_conn(False, "offline")
            self.set_status("Server unreachable - start server_gui.py, then Connect.")
        else:
            messagebox.showerror("Request failed", err.message)

    # ------------------------------------------------------------------
    # Connection and terms
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Point the client at the address in the header and reload."""
        self.api = ApiClient(self.var_server.get().strip() or DEFAULT_SERVER)
        self.var_server.set(self.api.base_url)
        self.settings["server"] = self.api.base_url
        self.set_status(f"Connecting to {self.api.base_url} ...")
        self.load_terms()

    def load_terms(self) -> None:
        """Ask the server which (year, term) pairs exist, then load one.

        This is what makes the term selector real: the list comes from the
        data, not from a hard-coded constant, so adding a new term to
        courses.csv makes it appear in the dropdown automatically.
        """
        self._run(self.api.terms, self._on_terms)

    def _on_terms(self, terms: list[dict]) -> None:
        self.terms = terms
        self._set_conn(True, "connected")
        if not terms:
            self.cmb_term.configure(values=["no courses on the sheet"])
            self.var_term.set("no courses on the sheet")
            self.set_status("The server has no courses - check courses.csv.")
            return

        # Fill the dropdown.  The label is what the user sees; the index of the
        # chosen label is looked up again in self.terms when it changes.
        labels = [f"{t['label']}  ({t['sections']} sections)" for t in terms]
        self.cmb_term.configure(values=labels)

        # Restore the saved term when it still exists, else use the first one.
        saved = next((i for i, t in enumerate(terms)
                      if t["year"] == self.settings.get("year")
                      and t["term"] == self.settings.get("term")), 0)
        self.cmb_term.current(saved)
        self._apply_term(terms[saved])
        self._save_settings()

    def _on_term_change(self, _event=None) -> None:
        """The user picked another term in the dropdown."""
        index = self.cmb_term.current()
        if 0 <= index < len(self.terms):
            self._apply_term(self.terms[index])

    def _apply_term(self, term: dict) -> None:
        """Switch the whole client to one term: sheet, timetable, labels."""
        self.year, self.term = term["year"], term["term"]
        self.var_term.set(f"{term['label']}  ({term['sections']} sections)")
        self.selected = None
        self.lbl_term_badge.configure(text=f"for {term['label']}")
        self.set_status(f"Term set to {term['label']} - loading the course sheet ...")
        self.refresh_courses()

    def _set_conn(self, ok: bool, text: str) -> None:
        """Colour the connection light in the header."""
        self.dot.set_colour(theme.OK if ok else theme.DANGER)
        self.lbl_conn.configure(text=text,
                                foreground=theme.HEADER_SUB if ok else "#ffb4b4")

    # ------------------------------------------------------------------
    # Course sheet, search index and the results table
    # ------------------------------------------------------------------
    def refresh_courses(self) -> None:
        """Load the sections of the current term and rebuild the search index."""
        if not (self.year and self.term):
            return
        self.set_status("Loading course sheet ...")
        year, term = self.year, self.term

        def done(courses: list[dict]) -> None:
            self.courses = courses
            # The hash table is rebuilt once per term load - not per keystroke.
            self.index.build(courses)
            self._render_courses()
            self._set_conn(True, "connected")
            seats = sum(c["capacity"] for c in courses)
            filled = sum(c["enrolled"] for c in courses)
            self.lbl_sheet_info.configure(
                text=f"{len(courses)} sections / {len({c['code'] for c in courses})} "
                     f"courses / {filled} of {seats} places taken")
            self.lbl_stats.configure(
                text=f"hash index: {self.index.key_count} keys over "
                     f"{self.index.size} sections")
            self.set_status(f"{len(courses)} sections loaded for "
                            f"{self.year} Term {self.term}")
            # The timetable follows the same term, and a returning student is
            # recognised straight away: the dictionary lookup fills the form
            # and then loads their registrations for the new term.
            if self.var_sid.get().strip():
                self.lookup_student()

        self._run(lambda: self.api.courses(year, term), done)

    def _render_courses(self) -> None:
        """Repaint the results table from the hash index (search aware).

        The table is rebuilt from the index on every keystroke.  That sounds
        wasteful but is not: the index has already narrowed the candidates, so
        the loop only runs over the matches, not over the whole sheet.
        """
        query = self.var_search.get().strip()
        matches, seconds = self.index.search(query)

        # "Hide full" is applied after the search so the index stays generic.
        if self.var_hide_full.get():
            matches = [c for c in matches if not c["is_full"]]

        # Keep the selected row selected across refreshes.
        keep = self.selected["section_id"] if self.selected else None
        self.tree.delete(*self.tree.get_children())
        for course in matches:
            tag = ("full",) if course["is_full"] else (
                ("low",) if course["seats_left"] <= 3 else ())
            self.tree.insert("", "end", iid=course["section_id"], tags=tag, values=(
                f"{course['code']}-{course['section']}", course["title"],
                f"{course['day']}  {course['start']}-{course['end']}",
                course["venue"], f"{course['enrolled']}/{course['capacity']}"))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)

        # Report what the hash lookup did, in milliseconds.
        self.lbl_index.configure(
            text=f"{len(matches)} match{'' if len(matches) == 1 else 'es'} "
                 f"in {seconds * 1000:.2f} ms")

    def _on_course_select(self, _event=None) -> None:
        """The user picked a section row in the table."""
        selection = self.tree.selection()
        if not selection:
            return
        section_id = selection[0]
        self.selected = next((c for c in self.courses
                              if c["section_id"] == section_id), None)
        if not self.selected:
            return

        course = self.selected
        # How many sessions does this code have in this term?  Telling the user
        # here saves them a ONE_SESSION error later.
        siblings = self.index.index.get(course["code"].lower(), set())
        siblings = {sid for sid in siblings
                    if self.index.tokens[sid]["code"] == course["code"]}
        session_note = (f"   |   session {course['section']} of {len(siblings)}"
                        if len(siblings) > 1 else "")

        self.lbl_sel_title.configure(text=f"{course['label']}  -  {course['title']}")
        places = ("FULL - no places left" if course["is_full"]
                  else f"{course['seats_left']} of {course['capacity']} places left")
        self.lbl_sel_meta.configure(
            text=(f"{course['day']}  {course['start']}-{course['end']}   |   "
                  f"{course['venue']}\n{course['instructor']}   |   "
                  f"{course['credits']} credits   |   {places}{session_note}"))

        # A different row means the old confirmation no longer applies.
        if section_id != self._msg_section:
            self.lbl_form_msg.configure(text="")
            self._msg_section = None
        self.btn_register.configure(state="disabled" if course["is_full"] else "normal")
        self.set_status(f"Selected {course['label']} - {course['title']}")

    # ------------------------------------------------------------------
    # Student dictionary lookup (prevents repeating the same details)
    #
    # Flow: the student types an id -> focus leaves the field -> GET
    # /api/students/<id> -> the server looks the id up in its dictionary.
    # Known student: name and e-mail are filled in (only if still empty) and
    # the timetable is loaded.  Unknown id: nothing is fetched and the form is
    # left for the user to complete.
    # ------------------------------------------------------------------
    def lookup_student(self) -> None:
        """Fill in the form for a returning student, straight from the server.

        The server keeps `students` as a dictionary keyed by student id.  When
        the id is recognised the name and e-mail are written into the form (if
        those fields are empty) and the timetable is loaded, so a returning
        student only ever types their id.
        """
        student_id = self.var_sid.get().strip()
        if len(student_id) < 3:
            return                            # too short to be meaningful
        self.set_status(f"Looking up {student_id.upper()} ...")

        def done(result) -> None:
            if not result:
                self.lbl_tt_hint.configure(
                    text=f"New student {student_id.upper()} - "
                         f"name and e-mail are needed.")
                return
            student = result["student"]
            # Only fill empty fields: never silently overwrite typing.
            if not self.var_name.get().strip():
                self.var_name.set(student["name"])
            if not self.var_email.get().strip():
                self.var_email.set(student["email"])
            self.lbl_tt_hint.configure(
                text=f"Welcome back, {student['name']} - details loaded from the "
                     f"student dictionary.")
            self.set_status(f"{student_id.upper()} found: {student['name']}")
            self.load_my_registrations()

        self._run(lambda: self.api.student(student_id), done)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def do_register(self) -> None:
        """Send the registration to the server.

        The client deliberately validates almost nothing: the server owns the
        rules, so the same checks apply no matter which client (or curl) is
        used.  The form only checks that *something* was typed, to give a
        quicker answer for the obvious mistake.
        """
        if not self.selected:
            messagebox.showinfo("No course selected",
                                "Choose a section row in the course sheet first.")
            return

        payload = {
            "section_id": self.selected["section_id"],
            "student_id": self.var_sid.get().strip(),
            "student_name": self.var_name.get().strip(),
            "email": self.var_email.get().strip(),
            "notes": self.var_notes.get().strip(),
        }
        self.btn_register.configure(state="disabled", text="Registering ...")
        self.lbl_form_msg.configure(text="Sending registration ...",
                                    foreground=theme.MUTED)
        self._run(lambda: self.api.register(**payload),
                  self._on_registered, self._on_register_failed)

    def _on_registered(self, reg: dict) -> None:
        """Show the confirmation, then refresh the sheet and the timetable."""
        self.btn_register.configure(text="Register", state="normal")
        self._msg_section = reg["section_id"]
        welcome = " (new student added to the register)" if reg.get("new_student") else ""
        self.lbl_form_msg.configure(
            text=f"Registered in {reg['label']} - {reg['title']}\n"
                 f"{reg['day']} {reg['start']}-{reg['end']}, {reg['venue']}   "
                 f"(ref #{reg['id']}){welcome}",
            foreground=theme.OK)
        self.var_notes.set("")
        self._save_settings()
        self.set_status(f"Registration #{reg['id']} confirmed "
                        f"for {reg['student_id']}")
        self.refresh_courses()          # seat counts changed
        self.load_my_registrations()    # the timetable changed

    def _on_register_failed(self, err: ApiError) -> None:
        """Explain a refused registration in the form, not in a pop-up."""
        self.btn_register.configure(text="Register", state="normal")
        self._msg_section = self.selected["section_id"] if self.selected else None
        colours = {"FULL": theme.DANGER, "DUPLICATE": theme.WARN,
                   "TIME_CONFLICT": theme.WARN, "ONE_SESSION": theme.WARN}
        self.lbl_form_msg.configure(text=err.message,
                                    foreground=colours.get(err.code, theme.DANGER))
        self.set_status(err.message.splitlines()[0])
        if err.code in ("FULL", "DUPLICATE", "TIME_CONFLICT", "ONE_SESSION"):
            self.refresh_courses()      # numbers on screen may be stale
        if err.code == "NO_SERVER":
            self._set_conn(False, "offline")

    # ------------------------------------------------------------------
    # Timetable data and interaction
    # ------------------------------------------------------------------
    def load_my_registrations(self) -> None:
        """Fetch this student's registrations for the selected term.

        The year/term filter is sent to the server, so the timetable only ever
        contains classes of the term shown in the selector - switching term
        therefore switches the timetable too, without extra client-side logic.
        """
        student_id = self.var_sid.get().strip()
        if not student_id:
            self.timetable.set_rows([])
            self.lbl_tt_hint.configure(text="Enter a student ID to see their week.")
            self.lbl_tt_count.configure(text="")
            return
        year, term = self.year, self.term
        self.lbl_tt_hint.configure(
            text=f"Classes for {student_id.upper()} in {year} Term {term}  -  "
                 f"click a block to select it.")

        def done(rows: list[dict]) -> None:
            self.my_rows = rows
            self.timetable.set_rows(rows)
            # The footer summarises what the grid shows: classes and credits.
            count = len(rows)
            credits = sum(int(r.get("credits") or 0) for r in rows)
            self.lbl_tt_count.configure(
                text=f"{count} class{'es' if count != 1 else ''} this term"
                     + (f"   |   {credits} credits" if credits else ""))

        self._run(lambda: self.api.registrations(student_id, year, term), done)

    def _on_block_select(self, reg: dict | None) -> None:
        """Called by the timetable when a block is clicked (or cleared)."""
        if reg is None:
            self.btn_drop.configure(state="disabled")
            return
        self.lbl_tt_hint.configure(
            text=f"Selected {reg['code']}-{reg['section']}  -  {reg['day']} "
                 f"{reg['start']}-{reg['end']} in {reg['venue']}  |  registration "
                 f"#{reg['id']}")
        self.btn_drop.configure(state="normal")

    def do_drop(self) -> None:
        """Cancel the registration of the selected timetable block.

        Dropping is a destructive action, so it always asks first and then
        refreshes both the sheet (the seat is free again) and the timetable
        (the block disappears).
        """
        reg = self.timetable.selected
        if not reg:
            return
        if not messagebox.askyesno(
                "Drop course",
                f"Drop {reg['code']}-{reg['section']} ({reg['title']})\n"
                f"{reg['day']} {reg['start']}-{reg['end']}, {reg['venue']}?"):
            return
        self._run(lambda: self.api.cancel(reg["id"]),
                  self._on_dropped, self._on_drop_failed)

    def _on_dropped(self, result: dict) -> None:
        self.set_status(f"Registration #{result['deleted']} cancelled.")
        self.refresh_courses()
        self.load_my_registrations()

    def _on_drop_failed(self, err: ApiError) -> None:
        messagebox.showerror("Could not drop the course", err.message)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        """Remember the user's choices and close the window."""
        self._save_settings()
        self.destroy()


def main() -> None:
    """Command line entry point: `python3 client_gui.py [--server URL]`."""
    parser = argparse.ArgumentParser(description="Class Registration - student client")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help="server base URL (default http://127.0.0.1:8000)")
    args = parser.parse_args()
    RegistrationClient(args.server).mainloop()


if __name__ == "__main__":
    main()
