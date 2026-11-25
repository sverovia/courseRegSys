#!/usr/bin/env python3
"""
client_gui.py - Class Registration System: client side (version 3).

============================================================================
WHAT THIS FILE IS
============================================================================

The student's desktop program, written with Tkinter (standard library only) so
nothing has to be installed.  It is the "client" half of the project: it never
touches the database, it only calls the JSON API that `server.py` exposes.

Version 3 adds the LOGIN SYSTEM required by the brief.  The program now opens
on a sign-in window; nothing else is reachable until the server accepts the
student id and password.  After a successful login the main window appears.

============================================================================
THE THREE WINDOWS / SCREENS
============================================================================

1. LoginWindow      - the front door.  Student id + password + server address.
                      The answer from POST /api/login contains a session token,
                      which every later request carries in an X-Auth-Token
                      header.  Closing the program (or pressing Sign out)
                      forgets the token.

2. RegistrationClient (main window)
      +-----------------------------------------------------------------+
      | header:  title      term selector     signed in as ... [Admin]  |
      +--------------------------------+--------------------------------+
      |  COURSE SHEET                  |  REGISTER FOR A COURSE         |
      |  - hash-index search box       |  - your identity (from login)  |
      |  - results table               |  - register / confirmation     |
      |                                +--------------------------------+
      |                                |  MY TIMETABLE (clickable grid) |
      +-----------------------------------------------------------------+

3. AdminWindow      - only for accounts whose role is "admin".  Lists every
                      student account, can add a student (requirement 2) and
                      switch a student's ability to register on or off
                      (requirement 3).

============================================================================
HOW LOGIN PROTECTS THE REST OF THE PROGRAM
============================================================================

* The token lives in `ApiClient.token`; `_call` adds it to every request, so no
  widget ever handles credentials.
* The client no longer sends a student id when registering: the server takes it
  from the session.  That makes "register as somebody else" impossible from the
  GUI, and the server rejects it even if attempted by hand (403 FORBIDDEN).
* A revoked account (`can_register` false) still logs in and can browse its
  timetable, but the Register button is disabled and the server would refuse
  anyway (403 REGISTRATION_BLOCKED).
* If a token expires the next request answers 401; the client then drops back to
  the login window instead of showing a confusing error.

============================================================================
TEST ACCOUNTS (development only - see README)
============================================================================

    student : S1001 / amy2026      (and every row of students.csv)
    blocked : S1006 / felix2026    (registration switched off in the CSV)
    admin   : ADMIN / admin2026    (hard-coded test account in server.py)

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
import threading                    # so the windows never freeze on HTTP
import time                         # measuring how fast the hash search is
import tkinter as tk                # the GUI toolkit
import urllib.error                 # HTTP errors (401, 403, 409, ...)
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
# Saved settings (server address and last student id - never the password)
#
# Two separate functions rather than a settings class, because the file is
# tiny: `load_settings` is called by the login window before anything else
# exists, and `save_settings` is called after a successful login and whenever
# the term changes.  Both swallow I/O errors on purpose - a read-only home
# directory must not stop somebody from registering for a course.
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    """Read data/client_settings.json, or return sensible defaults.

    Deliberately not stored: the password and the session token.  Keeping them
    out of the file means a shared machine cannot leak an account.
    """
    settings = {"server": DEFAULT_SERVER, "student_id": "", "year": 0, "term": 0}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            saved = json.load(handle)
        for key in settings:                 # only copy the keys we expect
            if key in saved:
                settings[key] = saved[key]
    except (OSError, ValueError):
        pass                                 # first run - defaults are fine
    return settings


def save_settings(**values) -> None:
    """Merge `values` into the settings file, ignoring I/O problems."""
    settings = load_settings()
    settings.update(values)
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
    except OSError:
        pass                                 # never break the app over settings


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """A failure from the server (or from the network) with a code attached.

    The GUI uses `code` to decide *where* to show a message: validation
    problems belong next to the form, connection problems belong in the header
    and the status bar, and AUTH_REQUIRED means "go back to the login window".
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# HTTP access layer - every request carries the session token
# ----------------------------------------------------------------------
class ApiClient:
    """All calls to server.py, one method per endpoint.

    The session token is held on the instance and attached to every request, so
    no other part of the program has to remember to send it.

    Two different lifetimes share this one class:

        * the login window creates a *throw-away* client with no token, only to
          call login();
        * the main window keeps a client for its whole life, with the token
          from that login, and uses it for everything else.

    Keeping one class for both keeps the token handling in a single place: if
    the header name ever changed, exactly one line would need editing.
    """

    def __init__(self, base_url: str = DEFAULT_SERVER, token: str | None = None):
        self.base_url = base_url.rstrip("/")     # no trailing slash, paths append
        self.token = token

    # -- plumbing ----------------------------------------------------------
    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Perform one request and translate failures into ApiError.

        urllib raises HTTPError for 4xx/5xx, which still carries a JSON body -
        the server always sends {"error", "code"}, so the message shown to the
        user is written by the server, not invented here.
        """
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Auth-Token"] = self.token     # the session, on every call
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
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

    # -- login / session ---------------------------------------------------
    def login(self, student_id: str, password: str) -> dict:
        """POST /api/login.  Returns {token, role, student, expires_in}.

        This is the only call made *before* a token exists, which is why the
        token is attached afterwards instead of being passed in.

        The plain password exists only as an argument here: it is sent once,
        never stored, and never written to the settings file.
        """
        result = self._call("POST", "/api/login",
                            {"student_id": student_id, "password": password})
        self.token = result["token"]
        return result

    def logout(self) -> dict:
        """POST /api/logout, then forget the token locally either way.

        The `finally` matters: if the server is already unreachable, the local
        token is still discarded, so signing out always works from the user's
        point of view.
        """
        try:
            return self._call("POST", "/api/logout")
        finally:
            self.token = None

    def me(self) -> dict:
        """GET /api/me - who am I, and is my account still allowed to register?"""
        return self._call("GET", "/api/me")

    # -- course sheet ------------------------------------------------------
    def terms(self) -> list[dict]:
        """Every (year, term) the server knows about, for the selector."""
        return self._call("GET", "/api/terms")["terms"]

    def courses(self, year: int, term: int) -> list[dict]:
        """The course sheet for one term (all terms when year/term are 0)."""
        query = f"?year={year}&term={term}" if year and term else ""
        return self._call("GET", "/api/courses" + query)["courses"]

    # -- registrations -----------------------------------------------------
    def register(self, section_id: str, notes: str = "") -> dict:
        """Create a registration for the signed-in student.

        No student id, name or e-mail is sent: the server reads the identity
        from the session token, which is what makes the login meaningful.
        """
        return self._call("POST", "/api/registrations", {
            "section_id": section_id, "notes": notes})["registration"]

    def registrations(self, year: int = 0, term: int = 0) -> list[dict]:
        """Registrations of the signed-in student (optionally one term)."""
        params = {}
        if year and term:
            params.update(year=year, term=term)
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._call("GET", f"/api/registrations{query}")["registrations"]

    def cancel(self, registration_id: int) -> dict:
        """Remove one registration by its id."""
        return self._call("DELETE", f"/api/registrations/{registration_id}")

    # -- admin -------------------------------------------------------------
    # These three raise ApiError 403 when the session is not an admin one, so
    # the UI never has to trust that it only shows them to admins.
    def admin_students(self) -> list[dict]:
        """Every student account with its access flag and class count."""
        return self._call("GET", "/api/admin/students")["students"]

    def admin_add_student(self, student_id: str, name: str, email: str,
                          password: str, can_register: bool = True) -> dict:
        """Create a new student account in the dictionary."""
        return self._call("POST", "/api/admin/students", {
            "student_id": student_id, "name": name, "email": email,
            "password": password, "can_register": can_register})["student"]

    def admin_set_access(self, student_id: str, allowed: bool) -> dict:
        """Switch one student's ability to register on or off."""
        return self._call("POST", f"/api/admin/students/{student_id}/access",
                          {"can_register": allowed})["student"]

    def admin_set_password(self, student_id: str, password: str) -> dict:
        """Reset a student's password."""
        return self._call("POST", f"/api/admin/students/{student_id}/password",
                          {"password": password})["student"]


# ---------------------------------------------------------------------------
# The hash table that powers the search box
# ---------------------------------------------------------------------------
# ----------------------------------------------------------------------
# Search index - the hash table used by the course-code search box
# ----------------------------------------------------------------------
class SearchIndex:
    """An inverted index (a dictionary of sets) over one term's sections.

    WHY THIS EXISTS
    ---------------
    Scanning every course for every keystroke is O(n) per character.  Instead
    the index is built once when a term is loaded:

        index["mon"]   = {"2026-T1-CS101-A", "2026-T1-CS101-B", ...}
        index["cs101"] = {"2026-T1-CS101-A", "2026-T1-CS101-B", "2026-T1-CS101-C"}
        index["science hall"] = {...}

    A query is then split into words, each word is a dictionary lookup (O(1) on
    average), and the resulting sets are intersected.  That makes searching
    proportional to the number of matches rather than to the size of the sheet.

    FIELDS
    ------
    index     : dict[str, set[str]]  word -> set of section ids
    tokens    : dict[str, dict]      section id -> the course itself (for output)
    prefix    : dict[str, set[str]]  first two letters -> section ids, so the
                                     user gets partial matches while typing
    """

    # Fields of a course that should be searchable.  Adding a field here makes
    # it searchable everywhere, because tokens come from this tuple.
    SEARCHABLE = ("code", "section", "title", "instructor", "venue",
                  "department", "day", "label")

    def __init__(self) -> None:
        self.index: dict[str, set[str]] = {}
        self.tokens: dict[str, dict] = {}
        self.prefix: dict[str, set[str]] = {}
        self.built_at = 0.0
        self.size = 0
        self.key_count = 0

    # -- building ----------------------------------------------------------
    @staticmethod
    def _words(course: dict) -> set[str]:
        """Break one course record into lower-case searchable words.

        Multi-word values such as "Science Hall R101" are split on spaces,
        commas, dashes and slashes, so searching for "hall" or "r101" both
        work.  The code with its section letter is indexed as well ("cs101b").
        """
        words: set[str] = set()
        for field in SearchIndex.SEARCHABLE:
            raw = str(course.get(field, "")).lower()
            for chunk in raw.replace(",", " ").replace("-", " ").replace("/", " ").split():
                words.add(chunk)
        words.add(f"{course['code'].lower()}{course['section'].lower()}")
        words.add(f"{course['code'].lower()} {course['section'].lower()}")
        return words

    def build(self, courses: list[dict]) -> None:
        """(Re)build the index from scratch for the courses of one term."""
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
        self.key_count = len(self.index)

    # -- querying ----------------------------------------------------------
    def search(self, query: str) -> tuple[list[dict], float]:
        """Return (matching courses, seconds spent).

        Strategy:
            * empty query  -> everything, in course order
            * every word   -> must match (intersection), exact bucket first
            * no exact bucket for a word -> fall back to its two-letter prefix
              so "prog" still finds "programming" while typing
        """
        started = time.perf_counter()
        text = (query or "").strip().lower()

        if not text:
            everything = sorted(self.tokens.values(),
                                key=lambda c: (c["code"], c["section"], c["day"]))
            return everything, time.perf_counter() - started

        words = [w for w in text.replace("-", " ").split() if w]
        result: set[str] | None = None

        for word in words:
            exact = self.index.get(word)
            if exact is None:
                exact = set()
                for key, bucket in self.prefix.items():
                    if key == word[:2]:
                        exact |= bucket
                exact = {sid for sid in exact
                         if any(word in str(self.tokens[sid].get(f, "")).lower()
                                for f in self.SEARCHABLE)}
            result = set(exact) if result is None else (result & exact)
            if not result:
                break

        matches = [self.tokens[sid] for sid in (result or set())]
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
    the callback, when a block is clicked.
    """

    def __init__(self, parent: tk.Misc, font_family: str,
                 on_select=None, on_open=None):
        super().__init__(parent, background=theme.CARD)
        self.family = font_family
        self.on_select = on_select        # called with a registration or None
        self.on_open = on_open            # reserved for a future double-click

        self.rows: list[dict] = []        # registrations currently drawn
        self.selected: dict | None = None # the block the user clicked
        self.blocks: dict[str, int] = {}  # registration id -> canvas column

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
    # Everything here is expressed in "minutes after midnight": that turns an
    # overlap test into a comparison and the pixel maths into one division.
    def _days(self) -> tuple[str, ...]:
        """Which day columns to draw: Mon-Fri, plus Sat/Sun when used."""
        used = {r["day"] for r in self.rows}
        extras = tuple(d for d in ALL_DAYS[5:] if d in used)
        return WEEKDAYS + extras

    def _window(self) -> tuple[int, int]:
        """The vertical time window in minutes.

        Built from the earliest start and latest end of the registrations, then
        clamped so the grid is never shorter than 09:00-15:00 and never starts
        later than 09:00.  Without the clamp a single morning class would be
        stretched over the whole canvas.
        """
        if not self.rows:
            return 9 * 60, 15 * 60
        first = min(hhmm_to_minutes(r["start"]) for r in self.rows)
        last = max(hhmm_to_minutes(r["end"]) for r in self.rows)
        first = min(first, 9 * 60)
        last = max(last, 15 * 60)
        # Round outwards to whole hours so the time labels read cleanly.
        return (first // 60) * 60, -(-last // 60) * 60

    # -- drawing -----------------------------------------------------------
    def redraw(self) -> None:
        """Repaint the whole grid.  Cheap enough to call on every resize."""
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
        # (start - window_start) / SLOT rows down, height = rows spanned.
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
            # The selected block gets a thick outline plus a marker, so it is
            # obvious which class "Drop selected" will remove.
            chosen = self.selected and self.selected["id"] == reg["id"]
            tag = f"block-{reg['id']}"

            canvas.create_rectangle(
                x0, top, x1, bottom, fill=fill, outline=ink,
                width=3 if chosen else 1, tags=(tag, "block"))
            if chosen:
                canvas.create_rectangle(
                    x1 - 9, top + 3, x1 - 3, top + 9, fill=ink, outline=ink,
                    tags=(tag, "block"))
            canvas.create_text(
                x0 + 5, top + 5, anchor="nw", tags=(tag, "block"),
                text=f"{reg['code']}-{reg['section']}",
                font=(self.family, 8, "bold"), fill=ink,
                width=max(x1 - x0 - 10, 20))
            # The second line is only drawn when the block is tall enough; the
            # venue is truncated so it can never spill outside the rectangle.
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
        self.clear_selection()            # clicking empty space clears it


# ---------------------------------------------------------------------------
# 1. The login window
# ---------------------------------------------------------------------------
class LoginWindow(tk.Tk):
    """The sign-in screen: student id, password, server address.

    On success it closes itself and opens the main window, handing over the
    token and the student record returned by the server.

    Why a separate Tk root instead of a modal dialog on top of the main window:
    a dialog can be dismissed (or bypassed by code) while the widgets behind it
    stay alive.  Destroying this window before the main window is even created
    makes "you cannot use the system without signing in" a structural fact
    rather than a UI convention.
    """

    def __init__(self, server: str = DEFAULT_SERVER):
        super().__init__()
        self.family = theme.apply(self)
        self.title("Class Registration - Sign in")
        # A login form does not need a big window; keeping it small also makes
        # it obvious that nothing behind it is reachable yet.
        self.geometry("760x480")
        self.resizable(False, False)

        self.settings = load_settings()
        self.server = server or self.settings["server"]
        self.busy = False
        self.results: queue.Queue = queue.Queue()

        self._build()
        self.bind("<Return>", lambda _e: self.do_login())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        """Two panels: a dark brand side and a white form side.

        The brand side also lists the test accounts.  That is a teaching aid for
        this project only - a real deployment would remove that label (and the
        hard-coded admin in server.py).
        """
        outer = tk.Frame(self, background=theme.BG)
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        outer.columnconfigure(0, weight=1, minsize=280)
        outer.columnconfigure(1, weight=1, minsize=380)
        outer.rowconfigure(0, weight=1)

        # ---- left: brand panel ----
        brand = tk.Frame(outer, background=theme.HEADER)
        brand.grid(row=0, column=0, sticky="nsew")
        tk.Label(brand, text="Course\nRegistration", background=theme.HEADER,
                 foreground="#ffffff", font=(self.family, 22, "bold"),
                 justify="left").pack(anchor="w", padx=24, pady=(36, 6))
        tk.Label(brand, text="Student sign-in", background=theme.HEADER,
                 foreground=theme.HEADER_SUB,
                 font=(self.family, 10)).pack(anchor="w", padx=24)
        tk.Label(brand,
                 text=("Sign in with the student ID and password\n"
                       "given to you by the course office.\n\n"
                       "Registration, your timetable and your\n"
                       "account status appear after sign-in."),
                 background=theme.HEADER, foreground="#c3cfe6",
                 font=(self.family, 9), justify="left").pack(
            anchor="w", padx=24, pady=(24, 0))

        # The test accounts are printed on the login screen because this is a
        # teaching project; a real deployment would delete this label.
        tk.Label(brand, text="TEST ACCOUNTS", background=theme.HEADER,
                 foreground="#7f92bb", font=(self.family, 8, "bold")).pack(
            anchor="w", padx=24, pady=(26, 2))
        tk.Label(brand,
                 text="student  S1001 / amy2026\n"
                      "blocked  S1006 / felix2026\n"
                      "admin    ADMIN / admin2026",
                 background=theme.HEADER, foreground="#8ff0c8",
                 font=("DejaVu Sans Mono", 8), justify="left").pack(
            anchor="w", padx=24)

        # ---- right: the form ----
        panel = theme.card(outer)
        panel.grid(row=0, column=1, sticky="nsew")
        card = panel.inner
        form = tk.Frame(card, background=theme.CARD)
        form.pack(fill="both", expand=True, padx=26, pady=26)
        form.columnconfigure(1, weight=1)

        tk.Label(form, text="Sign in", background=theme.CARD, foreground=theme.INK,
                 font=(self.family, 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        tk.Label(form, text="Your session stays open for one working day.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9)).grid(row=1, column=0, columnspan=2,
                                             sticky="w", pady=(2, 16))

        self.var_server = tk.StringVar(value=self.server)
        self.var_sid = tk.StringVar(value=self.settings.get("student_id", ""))
        self.var_password = tk.StringVar()

        rows = (("Server", self.var_server, False),
                ("Student ID", self.var_sid, False),
                ("Password", self.var_password, True))
        for index, (label, var, secret) in enumerate(rows, start=2):
            tk.Label(form, text=label, background=theme.CARD,
                     foreground=theme.MUTED,
                     font=(self.family, 9, "bold")).grid(
                row=index, column=0, sticky="w", pady=6, padx=(0, 12))
            entry = ttk.Entry(form, textvariable=var, show="\u2022" if secret else "")
            entry.grid(row=index, column=1, sticky="ew", pady=6)
            if label == "Password":
                self.ent_password = entry
            elif label == "Student ID":
                self.ent_sid = entry

        self.btn_login = ttk.Button(form, text="Sign in", style="Primary.TButton",
                                    command=self.do_login)
        self.btn_login.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(18, 6))

        # One label for every kind of feedback (hint / error / "signing in").
        self.lbl_msg = tk.Label(form, text="", background=theme.CARD,
                                foreground=theme.MUTED, font=(self.family, 9),
                                anchor="w", justify="left", wraplength=340)
        self.lbl_msg.grid(row=6, column=0, columnspan=2, sticky="ew")

        tk.Label(form, text="Forgot your password? Ask the course office for a reset.",
                 background=theme.CARD, foreground="#9aa5b8",
                 font=(self.family, 8)).grid(row=7, column=0, columnspan=2,
                                             sticky="w", pady=(14, 0))
        self.ent_password.focus_set() if self.var_sid.get() else self.ent_sid.focus_set()

    # -- actions -----------------------------------------------------------
    def _run(self, work, on_done) -> None:
        """Run `work()` in a thread so the window cannot freeze on the network.

        This is a cut-down version of the worker used by the main window (one
        job at a time, no error callback) because the login screen only ever has
        one request in flight.
        """
        def runner() -> None:
            try:
                self.results.put(("ok", on_done, work()))
            except ApiError as err:
                self.results.put(("error", on_done, err))
            except Exception as err:                       # noqa: BLE001
                self.results.put(("error", on_done,
                                  ApiError(0, "CLIENT_ERROR", str(err))))
        threading.Thread(target=runner, daemon=True).start()
        self.after(80, self._drain)

    def _drain(self) -> None:
        """Apply the worker's result on the GUI thread."""
        try:
            kind, callback, payload = self.results.get_nowait()
        except queue.Empty:
            self.after(80, self._drain)
            return
        if kind == "ok":
            callback(payload)
        else:
            self._show_error(payload)

    def _show_error(self, err: ApiError) -> None:
        """Turn a failure into one readable line under the form."""
        self.busy = False
        self.btn_login.configure(text="Sign in", state="normal")
        colours = {"BAD_LOGIN": theme.DANGER, "NO_SERVER": theme.WARN,
                   "TIMEOUT": theme.WARN, "AUTH_REQUIRED": theme.DANGER}
        self.lbl_msg.configure(text=err.message,
                               foreground=colours.get(err.code, theme.DANGER))

    def do_login(self) -> None:
        """Validate the fields locally, then ask the server to authenticate."""
        if self.busy:
            return
        server = self.var_server.get().strip() or DEFAULT_SERVER
        student_id = self.var_sid.get().strip()
        password = self.var_password.get()

        # Only the obvious cases are checked here; the password itself is the
        # server's business.
        if not student_id:
            self.lbl_msg.configure(text="Please enter your student ID.",
                                   foreground=theme.DANGER)
            self.ent_sid.focus_set()
            return
        if not password:
            self.lbl_msg.configure(text="Please enter your password.",
                                   foreground=theme.DANGER)
            self.ent_password.focus_set()
            return

        self.busy = True
        self.btn_login.configure(text="Signing in ...", state="disabled")
        self.lbl_msg.configure(text="Contacting the server ...",
                               foreground=theme.MUTED)

        api = ApiClient(server)
        self._run(lambda: api.login(student_id, password),
                  lambda session: self._on_success(server, student_id, session))

    def _on_success(self, server: str, student_id: str, session: dict) -> None:
        """Store the address for next time, then open the main window.

        Only the server address and the student id are remembered.  The token
        deliberately is not: a fresh login is required on every start, which is
        the safe default for shared lab machines.
        """
        save_settings(server=server, student_id=student_id)
        self.destroy()                       # the login window is finished
        RegistrationClient(server, session).mainloop()


# ---------------------------------------------------------------------------
# 3. The admin window (opened from the main window by an admin)
# ---------------------------------------------------------------------------
class AdminWindow(tk.Toplevel):
    """Student account management for staff (requirements 2 and 3).

    Two jobs, mirroring the two admin endpoints:

        * add a student to the dictionary            (POST /api/admin/students)
        * switch a student's ability to register     (.../<id>/access)

    Security note: hiding this window from students is a convenience, not the
    protection.  Every request it makes goes through /api/admin/, which the
    server rejects with 403 for a non-admin session, so a student who somehow
    opened this window could not do anything with it.
    """

    def __init__(self, parent: tk.Misc, api: ApiClient, font_family: str):
        super().__init__(parent)
        self.family = font_family
        self.api = api
        self.title("Student accounts - admin")
        self.geometry("860x600")
        self.configure(background=theme.BG)
        self.results: queue.Queue = queue.Queue()

        self._build()
        self.refresh()

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        outer = theme.card(self)
        outer.pack(fill="both", expand=True, padx=14, pady=(14, 8))
        card = outer.inner
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)

        head = tk.Frame(card, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 2))
        tk.Label(head, text="Student accounts", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(side="left")
        self.lbl_count = tk.Label(head, text="", background=theme.CARD,
                                  foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_count.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.refresh).pack(side="right")

        tk.Label(card, text="Select a row, then use the buttons underneath to allow "
                            "or block registration for that student.",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9)).grid(row=1, column=0, columnspan=2,
                                             sticky="w", padx=16, pady=(0, 8))

        # ---- the account table ----
        columns = ("sid", "name", "email", "role", "access", "regs")
        self.tree = ttk.Treeview(card, columns=columns, show="headings",
                                 selectmode="browse", height=12)
        for key, text, width, anchor in (("sid", "Student ID", 100, "w"),
                                         ("name", "Name", 170, "w"),
                                         ("email", "E-mail", 210, "w"),
                                         ("role", "Role", 80, "w"),
                                         ("access", "Registration", 110, "w"),
                                         ("regs", "Classes", 70, "center")):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "email"))
        # Blocked accounts are shown in red so they stand out.
        self.tree.tag_configure("blocked", foreground=theme.DANGER)
        self.tree.grid(row=2, column=0, sticky="nsew", padx=(16, 0))
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        scroll.grid(row=2, column=1, sticky="ns", padx=(0, 16))
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ---- access buttons ----
        tools = tk.Frame(card, background=theme.CARD)
        tools.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 4))
        self.btn_allow = ttk.Button(tools, text="Allow registration",
                                    style="Ghost.TButton", state="disabled",
                                    command=lambda: self.set_access(True))
        self.btn_allow.pack(side="left")
        self.btn_block = ttk.Button(tools, text="Block registration",
                                    style="Danger.TButton", state="disabled",
                                    command=lambda: self.set_access(False))
        self.btn_block.pack(side="left", padx=(8, 0))
        self.lbl_status = tk.Label(tools, text="", background=theme.CARD,
                                   foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_status.pack(side="right")

        # ---- the "add a student" form ----
        add = tk.Frame(card, background=theme.CARD)
        add.grid(row=4, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 16))
        add.columnconfigure(5, weight=1)

        self.var_new_id = tk.StringVar()
        self.var_new_name = tk.StringVar()
        self.var_new_email = tk.StringVar()
        self.var_new_password = tk.StringVar()

        fields = (("New student ID", self.var_new_id, 12),
                  ("Name", self.var_new_name, 18),
                  ("E-mail", self.var_new_email, 22),
                  ("Password", self.var_new_password, 12))
        for column, (label, var, width) in enumerate(fields):
            tk.Label(add, text=label, background=theme.CARD, foreground=theme.MUTED,
                     font=(self.family, 8, "bold")).grid(row=0, column=column,
                                                         sticky="w", padx=(0, 8))
            ttk.Entry(add, textvariable=var, width=width,
                      show="\u2022" if label == "Password" else "").grid(
                row=1, column=column, sticky="ew", padx=(0, 8))
        ttk.Button(add, text="Add student", style="Primary.TButton",
                   command=self.add_student).grid(row=1, column=4, padx=(0, 8))
        self.lbl_add_msg = tk.Label(add, text="", background=theme.CARD,
                                    foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_add_msg.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

    # -- helpers -----------------------------------------------------------
    def _run(self, work, on_done) -> None:
        """Same worker-thread pattern as the other windows."""
        def runner() -> None:
            try:
                self.results.put(("ok", on_done, work()))
            except ApiError as err:
                self.results.put(("error", on_done, err))
        threading.Thread(target=runner, daemon=True).start()
        self.after(80, self._drain)

    def _drain(self) -> None:
        try:
            kind, callback, payload = self.results.get_nowait()
        except queue.Empty:
            self.after(80, self._drain)
            return
        if kind == "ok":
            callback(payload)
        else:
            messagebox.showerror("Admin action failed", payload.message, parent=self)

    def _selected_id(self) -> str | None:
        """The student id of the highlighted row, if any."""
        selection = self.tree.selection()
        return self.tree.item(selection[0], "values")[0] if selection else None

    def _on_select(self, _event=None) -> None:
        state = "normal" if self.tree.selection() else "disabled"
        self.btn_allow.configure(state=state)
        self.btn_block.configure(state=state)

    # -- actions -----------------------------------------------------------
    def refresh(self) -> None:
        """Reload the account list from the server.

        Always re-read rather than patching the table locally: the server is the
        authority on `can_register`, and two admins may be working at once.
        """
        self._run(self.api.admin_students, self._render)

    def _render(self, students: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for student in students:
            allowed = student.get("can_register", True)
            self.tree.insert("", "end", iid=student["student_id"],
                             tags=() if allowed else ("blocked",),
                             values=(student["student_id"], student["name"],
                                     student["email"], student["role"],
                                     "allowed" if allowed else "blocked",
                                     student.get("registrations", 0)))
        blocked = sum(1 for s in students if not s.get("can_register", True))
        self.lbl_count.configure(
            text=f"{len(students)} accounts, {blocked} blocked")
        self._on_select()

    def set_access(self, allowed: bool) -> None:
        """Allow or block registration for the selected account.

        Both buttons use this one method with a different flag, so the two
        actions can never drift apart in behaviour or messaging.
        """
        student_id = self._selected_id()
        if not student_id:
            return
        self._run(lambda: self.api.admin_set_access(student_id, allowed),
                  lambda student: self._after_access(student, allowed))

    def _after_access(self, student: dict, allowed: bool) -> None:
        self.lbl_status.configure(
            text=f"{student['student_id']}: registration "
                 f"{'allowed' if allowed else 'blocked'}",
            foreground=theme.OK if allowed else theme.DANGER)
        self.refresh()

    def add_student(self) -> None:
        """Create an account from the small form at the bottom.

        The client checks that every field is filled in, and nothing else: the
        password length, e-mail shape and duplicate-id rules all belong to the
        server, which reports them in the message line under the form.
        """
        payload = (self.var_new_id.get().strip(), self.var_new_name.get().strip(),
                   self.var_new_email.get().strip(), self.var_new_password.get())
        if not all(payload):
            self.lbl_add_msg.configure(text="All four fields are required.",
                                       foreground=theme.DANGER)
            return
        self.lbl_add_msg.configure(text="Saving ...", foreground=theme.MUTED)
        self._run(lambda: self.api.admin_add_student(*payload),
                  self._after_add)

    def _after_add(self, student: dict) -> None:
        self.lbl_add_msg.configure(
            text=f"Added {student['student_id']} - {student['name']}",
            foreground=theme.OK)
        for var in (self.var_new_id, self.var_new_name, self.var_new_email,
                    self.var_new_password):
            var.set("")
        self.refresh()


# ---------------------------------------------------------------------------
# 2. The main window
# ---------------------------------------------------------------------------
class RegistrationClient(tk.Tk):
    """The registration screen shown after a successful sign-in.

    One class holds the window, the widgets and the small amount of state: the
    chosen term, the loaded courses, the selected section and the registration
    rows shown in the timetable.
    """

    def __init__(self, server: str = DEFAULT_SERVER, session: dict | None = None):
        super().__init__()
        # theme.apply() sets every ttk style and returns the font family to use
        # for the classic tk widgets (which ttk styles cannot reach).
        self.family = theme.apply(self)
        self.title("Class Registration - Student Client")
        self.geometry("1280x820")
        self.minsize(1120, 720)

        # ---- session (from the login window) ----
        session = session or {}
        self.student = session.get("student", {})
        self.role = session.get("role", "student")
        self.student_id = self.student.get("student_id", "")
        self.api = ApiClient(server, token=session.get("token"))
        self.settings = load_settings()

        # ---- state ----
        self.terms: list[dict] = []          # from /api/terms
        self.year = 0                        # currently selected term
        self.term = 0
        self.courses: list[dict] = []        # sections of the selected term
        self.index = SearchIndex()           # the hash table for the search box
        self.selected: dict | None = None    # section picked in the table
        self.my_rows: list[dict] = []        # registrations of the student
        self.results: queue.Queue = queue.Queue()   # worker-thread results
        self._msg_section: str | None = None # section the form message is about
        self.admin_window: AdminWindow | None = None

        self._build_header()
        self._build_body()
        self._build_statusbar()

        # Start by asking the server which terms exist.
        self.after(120, self.load_terms)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Header: title, term selector, identity, sign out
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        bar = tk.Frame(self, background=theme.HEADER, height=100)
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

        # ---- right: term selector, then the signed-in identity ----
        right = tk.Frame(bar, background=theme.HEADER)
        right.pack(side="right", padx=22)

        tk.Label(right, text="TERM / YEAR", background=theme.HEADER,
                 foreground=theme.HEADER_SUB, font=(self.family, 8, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2))

        # The combobox is filled from /api/terms; `readonly` stops the user
        # typing a term that does not exist.
        self.var_term = tk.StringVar(value="loading ...")
        self.cmb_term = ttk.Combobox(right, textvariable=self.var_term,
                                     state="readonly", width=22,
                                     font=(self.family, 10))
        self.cmb_term.grid(row=1, column=0, sticky="w")
        self.cmb_term.bind("<<ComboboxSelected>>", self._on_term_change)

        # ---- identity block: who is signed in, and how to leave ----
        # Two compact rows (name on top, badge + buttons underneath) so the
        # block never pushes past the fixed-height header bar.
        identity = tk.Frame(right, background=theme.HEADER)
        identity.grid(row=0, column=1, rowspan=3, sticky="e", padx=(20, 0))

        top = tk.Frame(identity, background=theme.HEADER)
        top.pack(anchor="e")
        tk.Label(top, text="SIGNED IN AS  ", background=theme.HEADER,
                 foreground=theme.HEADER_SUB, font=(self.family, 8, "bold")).pack(
            side="left")
        tk.Label(top, text=f"{self.student.get('name', '')}  ({self.student_id})",
                 background=theme.HEADER, foreground="#ffffff",
                 font=(self.family, 10, "bold")).pack(side="left")

        bottom = tk.Frame(identity, background=theme.HEADER)
        bottom.pack(anchor="e", pady=(4, 0))
        badge = "ADMIN" if self.role == "admin" else "STUDENT"
        self.lbl_role = tk.Label(bottom, text=badge, background="#2c3a61",
                                 foreground="#8ff0c8", font=(self.family, 8, "bold"),
                                 padx=8, pady=2)
        self.lbl_role.pack(side="left", padx=(0, 8))
        # Only an admin sees the account manager button.
        if self.role == "admin":
            ttk.Button(bottom, text="Manage accounts", style="Ghost.TButton",
                       command=self.open_admin).pack(side="left", padx=(0, 6))
        ttk.Button(bottom, text="Sign out", style="Ghost.TButton",
                   command=self.do_logout).pack(side="left")

    # ------------------------------------------------------------------
    # Body: course sheet on the left, form + timetable on the right
    # ------------------------------------------------------------------
    def _build_body(self) -> None:
        body = tk.Frame(self, background=theme.BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)
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

        head = tk.Frame(card, background=theme.CARD)
        head.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(head, text="Course Sheet", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(side="left")
        self.lbl_sheet_info = tk.Label(head, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_sheet_info.pack(side="right")

        # ---- the search bar (hash index, live on every keystroke) ----
        search_row = tk.Frame(card, background=theme.CARD)
        search_row.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(search_row, text="SEARCH", background=theme.CARD,
                 foreground=theme.MUTED, font=(self.family, 8, "bold")).pack(
            side="left", padx=(0, 8))
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self._render_courses())
        self.ent_search = ttk.Entry(search_row, textvariable=self.var_search)
        self.ent_search.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="Clear", style="Ghost.TButton",
                   command=lambda: self.var_search.set("")).pack(side="left", padx=(6, 0))
        self.var_hide_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_row, text="Hide full", variable=self.var_hide_full,
                        command=self._render_courses,
                        style="Card.TCheckbutton").pack(side="left", padx=(10, 0))

        hint = tk.Frame(card, background=theme.CARD)
        hint.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(hint, text="Try: cs101, programming, mon, science hall, lam",
                 background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 8)).pack(side="left")
        self.lbl_index = tk.Label(hint, text="", background=theme.CARD,
                                  foreground=theme.PRIMARY_DARK,
                                  font=(self.family, 8, "bold"))
        self.lbl_index.pack(side="right")

        # ---- the results table ----
        table = tk.Frame(card, background=theme.CARD)
        table.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        columns = ("label", "title", "time", "venue", "places")
        self.tree = ttk.Treeview(table, columns=columns, show="headings",
                                 selectmode="browse")
        # Only the title column stretches; the fixed ones always stay visible.
        # The time column is the widest of the fixed ones because a full
        # "Wed 09:00-10:30" label is the longest string the sheet contains.
        spec = {"label": ("Code", 72, "w"), "title": ("Course title", 110, "w"),
                "time": ("Time", 152, "w"), "venue": ("Venue", 158, "w"),
                "places": ("Places", 70, "center")}
        for key, (text, width, anchor) in spec.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=40, anchor=anchor,
                             stretch=(key == "title"))
        self.tree.tag_configure("full", foreground="#9aa5b8")
        self.tree.tag_configure("low", foreground=theme.WARN)
        self.tree.bind("<<TreeviewSelect>>", self._on_course_select)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

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
        """The registration form.  Identity comes from the login, not the form."""
        outer = theme.card(parent)
        outer.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        card = outer.inner

        tk.Label(card, text="Register for a course", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(
            anchor="w", padx=18, pady=(14, 2))

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

        # ---- identity line: read-only, straight from the session ----
        identity = tk.Frame(card, background="#f6f8fd")
        identity.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(identity, text="REGISTERING AS", background="#f6f8fd",
                 foreground=theme.MUTED, font=(self.family, 8, "bold")).pack(
            anchor="w", padx=10, pady=(6, 0))
        tk.Label(identity,
                 text=f"{self.student.get('name', '')}  -  {self.student_id}  "
                      f"-  {self.student.get('email', '')}",
                 background="#f6f8fd", foreground=theme.INK,
                 font=(self.family, 9), anchor="w", justify="left",
                 wraplength=360).pack(anchor="w", padx=10)
        # The account status is shown here, so a blocked student understands
        # why the button is disabled before they even try.
        allowed = self.student.get("can_register", True)
        tk.Label(identity,
                 text="Registration allowed" if allowed
                 else "Registration is blocked for this account - contact the office",
                 background="#f6f8fd",
                 foreground=theme.OK if allowed else theme.DANGER,
                 font=(self.family, 8, "bold")).pack(anchor="w", padx=10,
                                                     pady=(2, 6))

        # ---- notes (the only free-text field left) ----
        form = tk.Frame(card, background=theme.CARD)
        form.pack(fill="x", padx=18)
        form.columnconfigure(1, weight=1)
        self.var_notes = tk.StringVar()
        tk.Label(form, text="Notes", background=theme.CARD, foreground=theme.MUTED,
                 font=(self.family, 9, "bold")).grid(row=0, column=0, sticky="w",
                                                     padx=(0, 10), pady=3)
        self.ent_notes = ttk.Entry(form, textvariable=self.var_notes)
        self.ent_notes.grid(row=0, column=1, sticky="ew", pady=3)

        self.btn_register = ttk.Button(card, text="Register", style="Primary.TButton",
                                       command=self.do_register)
        self.btn_register.pack(fill="x", padx=18, pady=(12, 6))
        # Disabled from the start when the account is blocked.
        if not allowed:
            self.btn_register.configure(state="disabled")

        self.lbl_form_msg = tk.Label(card, text="", background=theme.CARD,
                                     foreground=theme.MUTED, font=(self.family, 9),
                                     anchor="w", justify="left", wraplength=380)
        self.lbl_form_msg.pack(fill="x", padx=18, pady=(0, 12))

    def _build_timetable_card(self, parent: tk.Misc) -> None:
        """The weekly timetable."""
        outer = theme.card(parent)
        outer.grid(row=1, column=0, sticky="nsew")
        card = outer.inner
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)

        head = tk.Frame(card, background=theme.CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 2))
        tk.Label(head, text="My timetable", background=theme.CARD,
                 foreground=theme.INK, font=(self.family, 13, "bold")).pack(side="left")
        self.lbl_term_badge = tk.Label(head, text="", background=theme.CARD,
                                       foreground=theme.MUTED, font=(self.family, 9))
        self.lbl_term_badge.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="Refresh", style="Ghost.TButton",
                   command=self.load_my_registrations).pack(side="right")

        self.lbl_tt_hint = tk.Label(
            card, text="Your classes for the selected term.",
            background=theme.CARD, foreground=theme.MUTED, font=(self.family, 9),
            anchor="w", justify="left", wraplength=420)
        self.lbl_tt_hint.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=18, pady=(0, 6))

        self.timetable = Timetable(card, self.family, on_select=self._on_block_select)
        self.timetable.grid(row=2, column=0, columnspan=2, sticky="nsew",
                            padx=18, pady=(0, 6))

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
        self.var_status = tk.StringVar(value="Signed in.")
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
        """Default error handling when a call did not pass its own handler.

        Three outcomes, in this order: a caller-specific handler, a return to
        the login window for an expired session, or a plain error dialog.

        A 401 means the session is gone (expired or signed out elsewhere), and
        the only sensible response is to return to the login window.
        """
        if callback:
            callback(err)
            return
        if err.code in ("AUTH_REQUIRED", "BAD_LOGIN"):
            messagebox.showwarning("Session ended",
                                   "Your session has expired. Please sign in again.")
            self.back_to_login()
            return
        if err.code == "NO_SERVER":
            self.set_status("Server unreachable - is server_gui.py still running?")
        else:
            messagebox.showerror("Request failed", err.message)

    # ------------------------------------------------------------------
    # Session handling
    # ------------------------------------------------------------------
    def do_logout(self) -> None:
        """Tell the server to forget the token, then return to the login window.

        Asking first is deliberate: signing out throws away the session, and a
        mis-click in the header would otherwise be annoying to recover from.
        """
        if not messagebox.askyesno("Sign out", "Sign out of the registration system?"):
            return
        try:
            self.api.logout()          # also clears self.api.token
        except ApiError:
            pass                       # already unreachable - leave anyway
        self.back_to_login()

    def back_to_login(self) -> None:
        """Close the main window and show the login window again."""
        server = self.api.base_url
        self.destroy()
        LoginWindow(server).mainloop()

    def open_admin(self) -> None:
        """Open the account manager (admins only - the server checks again)."""
        if self.admin_window and self.admin_window.winfo_exists():
            self.admin_window.lift()
            return
        self.admin_window = AdminWindow(self, self.api, self.family)

    # ------------------------------------------------------------------
    # Terms
    # ------------------------------------------------------------------
    def load_terms(self) -> None:
        """Ask the server which (year, term) pairs exist, then load one."""
        self._run(self.api.terms, self._on_terms)

    def _on_terms(self, terms: list[dict]) -> None:
        self.terms = terms
        if not terms:
            self.cmb_term.configure(values=["no courses on the sheet"])
            self.var_term.set("no courses on the sheet")
            self.set_status("The server has no courses - check courses.csv.")
            return

        labels = [f"{t['label']}  ({t['sections']} sections)" for t in terms]
        self.cmb_term.configure(values=labels)

        # Restore the saved term when it still exists, else use the first one.
        saved = next((i for i, t in enumerate(terms)
                      if t["year"] == self.settings.get("year")
                      and t["term"] == self.settings.get("term")), 0)
        self.cmb_term.current(saved)
        self._apply_term(terms[saved])

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
        save_settings(year=self.year, term=self.term)
        self.set_status(f"Term set to {term['label']} - loading the course sheet ...")
        self.refresh_courses()

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
            self.load_my_registrations()

        self._run(lambda: self.api.courses(year, term), done)

    def _render_courses(self) -> None:
        """Repaint the results table from the hash index (search aware)."""
        query = self.var_search.get().strip()
        matches, seconds = self.index.search(query)

        # "Hide full" is applied after the search so the index stays generic.
        if self.var_hide_full.get():
            matches = [c for c in matches if not c["is_full"]]

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
        siblings = {sid for sid in self.index.index.get(course["code"].lower(), set())
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

        if section_id != self._msg_section:
            self.lbl_form_msg.configure(text="")
            self._msg_section = None
        # Both the section and the account have to allow it.
        blocked = not self.student.get("can_register", True)
        self.btn_register.configure(
            state="disabled" if (course["is_full"] or blocked) else "normal")
        self.set_status(f"Selected {course['label']} - {course['title']}")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def do_register(self) -> None:
        """Send the registration to the server.

        Only the section id and the notes are sent: the student is identified by
        the session token, and the server owns every rule.
        """
        if not self.selected:
            messagebox.showinfo("No course selected",
                                "Choose a section row in the course sheet first.")
            return

        section_id = self.selected["section_id"]
        notes = self.var_notes.get().strip()
        self.btn_register.configure(state="disabled", text="Registering ...")
        self.lbl_form_msg.configure(text="Sending registration ...",
                                    foreground=theme.MUTED)
        self._run(lambda: self.api.register(section_id, notes),
                  self._on_registered, self._on_register_failed)

    def _on_registered(self, reg: dict) -> None:
        """Show the confirmation, then refresh the sheet and the timetable."""
        self.btn_register.configure(text="Register", state="normal")
        self._msg_section = reg["section_id"]
        self.lbl_form_msg.configure(
            text=f"Registered in {reg['label']} - {reg['title']}\n"
                 f"{reg['day']} {reg['start']}-{reg['end']}, {reg['venue']}   "
                 f"(ref #{reg['id']})",
            foreground=theme.OK)
        self.var_notes.set("")
        self.set_status(f"Registration #{reg['id']} confirmed for {reg['student_id']}")
        self.refresh_courses()          # seat counts changed
        self.load_my_registrations()    # the timetable changed

    def _on_register_failed(self, err: ApiError) -> None:
        """Explain a refused registration in the form, not in a pop-up."""
        self.btn_register.configure(text="Register", state="normal")
        self._msg_section = self.selected["section_id"] if self.selected else None
        colours = {"FULL": theme.DANGER, "DUPLICATE": theme.WARN,
                   "TIME_CONFLICT": theme.WARN, "ONE_SESSION": theme.WARN,
                   "REGISTRATION_BLOCKED": theme.DANGER}
        self.lbl_form_msg.configure(text=err.message,
                                    foreground=colours.get(err.code, theme.DANGER))
        self.set_status(err.message.splitlines()[0])
        if err.code in ("FULL", "DUPLICATE", "TIME_CONFLICT", "ONE_SESSION",
                        "REGISTRATION_BLOCKED"):
            self.refresh_courses()      # the numbers on screen may be stale
        if err.code == "AUTH_REQUIRED":
            self.back_to_login()

    # ------------------------------------------------------------------
    # Timetable data and interaction
    # ------------------------------------------------------------------
    def load_my_registrations(self) -> None:
        """Fetch this student's registrations for the selected term.

        No student id is sent: the server filters by the session, so a student
        physically cannot load somebody else's timetable.
        """
        year, term = self.year, self.term
        self.lbl_tt_hint.configure(
            text=f"Classes for {self.student_id} in {year} Term {term}  -  "
                 f"click a block to select it.")

        def done(rows: list[dict]) -> None:
            self.my_rows = rows
            self.timetable.set_rows(rows)
            count = len(rows)
            credits = sum(int(r.get("credits") or 0) for r in rows)
            self.lbl_tt_count.configure(
                text=f"{count} class{'es' if count != 1 else ''} this term"
                     + (f"   |   {credits} credits" if credits else ""))

        self._run(lambda: self.api.registrations(year, term), done)

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
        """Cancel the registration of the selected timetable block."""
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
        """Close the window; the token simply expires on the server."""
        self.destroy()


def main() -> None:
    """Command line entry point: show the login window first."""
    parser = argparse.ArgumentParser(description="Class Registration - student client")
    parser.add_argument("--server", default=None,
                        help="server base URL (default from settings, else "
                             "http://127.0.0.1:8000)")
    args = parser.parse_args()
    LoginWindow(args.server or load_settings()["server"]).mainloop()


if __name__ == "__main__":
    main()
