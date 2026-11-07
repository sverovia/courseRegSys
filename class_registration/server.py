#!/usr/bin/env python3
"""
server.py - Class Registration System: server side (version 2).

============================================================================
WHAT THIS FILE IS
============================================================================

This is the whole server of the project.  It is written with nothing but the
Python standard library, so there is nothing to install:

    * http.server  - a small threaded HTTP/JSON API
    * sqlite3      - permanent storage for the course sheet, students and
                     registrations
    * csv          - the course sheet itself lives in courses.csv

Two programs use this file:

    1. `python3 server.py`      -> runs the bare API server in the terminal
    2. `python3 server_gui.py`  -> imports this file, starts the same API in a
                                   background thread and adds a desktop console

============================================================================
THE DATA MODEL (this is the part that changed in version 2)
============================================================================

A course sheet row is now a *section* (one concrete session of a course):

    (year, term, code, section)  ->  day, start, end, venue, capacity, ...

This makes it possible for one course code to appear several times in the CSV
(a Monday group and a Wednesday group, for example) while staying a single
course from the student's point of view.  A student may therefore register in
CS101 section A *or* section B - but never both.  That rule is called
"one session per course code per term" and is enforced here, not in the GUI.

============================================================================
TWO DICTIONARIES THAT THE REQUIREMENTS ASK FOR
============================================================================

`CourseDB.course_index`  - a nested dictionary built while the CSV is loaded:

        {(year, term): {course_code: [section_id, ...]}}

    It is a hash table, so looking a course code up is O(1) instead of
    scanning the sheet.  It is used for three jobs:
        * refusing a *repeated section id* while loading (code repetition guard)
        * resolving "CS101" to its sections when a client omits the section
        * finding the other sections of a course to enforce one-session-only

`CourseDB.students`      - a dictionary of every known student:

        {student_id: {"student_id": ..., "name": ..., "email": ...}}

    It prevents a student from being stored over and over again.  The first
    registration creates the record; every later registration only needs the
    student id, and the name/e-mail are reused automatically.  A client can
    also pre-fill a form from `GET /api/students/<id>`.

============================================================================
HTTP API
============================================================================

GET    /api/health                         -> {"status": "ok", ...}
GET    /api/terms                          -> every (year, term) in the sheet
GET    /api/courses?year=&term=            -> sections for one term
GET    /api/courses/search?year=&term=&q=  -> server-side search (client does
                                              its own hash search as well)
GET    /api/students                       -> the student dictionary
GET    /api/students/<student_id>          -> one student, for form pre-fill
GET    /api/registrations?student_id=&year=&term=&course_code=&q=
POST   /api/registrations                  -> create a registration
DELETE /api/registrations/<id>             -> cancel a registration
GET    /api/stats?year=&term=              -> counters for the dashboard
GET    /                                   -> small HTML view of the sheet

Every failure answers {"error": "...", "code": "FULL"} with a matching HTTP
status so the client can react precisely (see ERROR_* constants below).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports only - no third party packages, no pip install.
# ---------------------------------------------------------------------------
import argparse                     # command line flags for `python3 server.py`
import csv                          # reading courses.csv
import json                         # request / response bodies
import os                           # path handling and folder creation
import re                           # validation patterns
import sqlite3                      # permanent storage
import threading                    # one lock per database + logging hook
from contextlib import contextmanager   # tidy "open, commit, close" blocks
from datetime import datetime       # registration timestamps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COURSES = os.path.join(BASE_DIR, "courses.csv")      # the course sheet
DEFAULT_DB = os.path.join(BASE_DIR, "data", "registration.db")

# The schema version is stored in the database with PRAGMA user_version.
# Version 2 introduced terms, sections and the students table.  If an older
# file is found it is moved aside (never deleted) and rebuilt.
SCHEMA_VERSION = 2

# Validation patterns: a student id is 3-20 "safe" characters, an e-mail has
# to look like a real address.  Keeping them here makes them easy to change.
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Error codes shared between server and client.  Using constants avoids typos
# and documents every failure the API can produce in one place.
ERROR_VALIDATION = "VALIDATION"          # 400 - something the user typed is wrong
ERROR_BAD_JSON = "BAD_JSON"              # 400 - body was not a JSON object
ERROR_BAD_ID = "BAD_ID"                  # 400 - registration id not a number
ERROR_NO_SUCH_COURSE = "NO_SUCH_COURSE"  # 404 - code/section not on the sheet
ERROR_NO_SUCH_STUDENT = "NO_SUCH_STUDENT"  # 404 - unknown student id
ERROR_NO_SUCH_REGISTRATION = "NO_SUCH_REGISTRATION"   # 404 - cancel target gone
ERROR_NO_ROUTE = "NO_ROUTE"              # 404 - unknown URL
ERROR_FULL = "FULL"                      # 409 - capacity reached
ERROR_DUPLICATE = "DUPLICATE"            # 409 - already in this exact section
ERROR_ONE_SESSION = "ONE_SESSION"        # 409 - already in another session of it
ERROR_TIME_CONFLICT = "TIME_CONFLICT"    # 409 - two classes at the same time
ERROR_AMBIGUOUS = "AMBIGUOUS_SECTION"    # 409 - code given, several sections exist
ERROR_SERVER = "SERVER_ERROR"            # 500 - unexpected bug

# Weekday order used when the sheet is sorted for display, and the length of a
# "slot" in minutes (the timetable in the client draws 30-minute rows).
DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SLOT_MINUTES = 30


def term_label(year: int, term: int) -> str:
    """Human readable label for a year/term pair, e.g. '2026 . Term 1'.

    Keeping the formatting in one function means the server, the client
    dropdown and the HTML page always agree on how a term is spelled.
    """
    return f"{year} · Term {term}"


def minutes(hhmm: str) -> int:
    """Convert 'HH:MM' into minutes after midnight (09:30 -> 570).

    Timetables are compared as integers, which is simpler and safer than
    comparing strings or datetime objects for every pair of courses.
    """
    hours, _, mins = hhmm.partition(":")
    return int(hours) * 60 + int(mins)


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """True when two [start, end) intervals share any minute.

    Two classes that merely touch (10:30 end, 10:30 start) do NOT clash, which
    is why the comparison is strict (<) on both sides.
    """
    return minutes(a_start) < minutes(b_end) and minutes(b_start) < minutes(a_end)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """An error that already knows which HTTP status and code to send back.

    Code everywhere: validation problems raise ApiError(400, ...), missing
    things raise 404, business-rule clashes raise 409.  The request handler
    turns it into JSON without any extra branching.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)    # keeps the message visible in tracebacks
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
class CourseDB:
    """All storage and all business rules of the registration system.

    The GUI never talks to SQLite directly; it goes through this class (or
    through the HTTP API, which also goes through this class).  That way the
    rules hold no matter which door the request came in through.
    """

    def __init__(self, db_path: str = DEFAULT_DB, courses_csv: str = DEFAULT_COURSES):
        # Remember where the data lives so reloads can be repeated later.
        self.db_path = db_path
        self.courses_csv = courses_csv

        # One re-entrant lock serialises every write.  SQLite is fine with
        # concurrent readers, but seat counting must not interleave, otherwise
        # two clients could both see "1 seat left" and both take it.
        self.lock = threading.RLock()

        # ------------------------------------------------------------------
        # The two dictionaries the requirements ask for.
        # ------------------------------------------------------------------
        # course_index[(year, term)][course_code] = [section_id, ...]
        self.course_index: dict[tuple[int, int], dict[str, list[str]]] = {}
        # students[student_id] = {"student_id", "name", "email", "created_at"}
        self.students: dict[str, dict] = {}
        # Statistics about the last CSV load, shown in the server console.
        self.sheet_report = {"rows": 0, "loaded": 0, "skipped": 0, "sections": 0}

        # Make sure the folder for the database file exists before connecting.
        folder = os.path.dirname(os.path.abspath(db_path))
        if folder:
            os.makedirs(folder, exist_ok=True)

        # Build (or upgrade) the schema, then fill the in-memory dictionaries
        # from disk so the API is ready the moment the constructor returns.
        self._prepare_database()
        self.reload_courses()
        self.reload_students()

    # -- connection handling ------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection.

        `row_factory = sqlite3.Row` lets rows be read like dictionaries, which
        makes the JSON responses almost free.  Foreign keys are switched on so
        a registration can never point at a course that does not exist.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")     # better concurrency
        return conn

    @contextmanager
    def session(self):
        """Open a connection, commit on success, always close it.

        `with sqlite3.connect(...)` only commits - it does NOT close the file
        handle.  Wrapping it here means every call site gets the same tidy
        behaviour: commit, rollback on error, close in a `finally` block.
        """
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _prepare_database(self) -> None:
        """Create the tables, or move an incompatible old file out of the way.

        Version 1 of this project stored a flat course table.  When a version 1
        file is found it is renamed to `registration.v1-backup.db` instead of
        being deleted, and a fresh database is created.
        """
        needs_reset = False
        if os.path.exists(self.db_path):
            try:
                with self.session() as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                needs_reset = version != SCHEMA_VERSION
            except sqlite3.DatabaseError:
                needs_reset = True      # unreadable file - rebuild as well

        if needs_reset:
            backup = self.db_path + ".v1-backup"
            os.replace(self.db_path, backup)
            print(f"[server] old database moved to {os.path.basename(backup)}")

        # Create every table if it is missing.  `IF NOT EXISTS` keeps this
        # safe to run on an existing, already correct database.
        with self.session() as conn:
            conn.executescript(
                """
                -- One row per SECTION of a course (a course code may repeat).
                CREATE TABLE IF NOT EXISTS courses (
                    section_id  TEXT PRIMARY KEY,   -- 2026-T1-CS101-A
                    year        INTEGER NOT NULL,
                    term        INTEGER NOT NULL,
                    code        TEXT    NOT NULL,
                    section     TEXT    NOT NULL,
                    title       TEXT    NOT NULL,
                    instructor  TEXT    NOT NULL,
                    department  TEXT    NOT NULL,
                    day         TEXT    NOT NULL,
                    start       TEXT    NOT NULL,
                    end         TEXT    NOT NULL,
                    venue       TEXT    NOT NULL,
                    capacity    INTEGER NOT NULL,
                    credits     INTEGER NOT NULL,
                    description TEXT DEFAULT ''
                );

                -- One row per student: this is the persistence side of the
                -- `students` dictionary, and stops details being repeated.
                CREATE TABLE IF NOT EXISTS students (
                    student_id  TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    email       TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );

                -- A registration links a student to one section.
                CREATE TABLE IF NOT EXISTS registrations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    section_id  TEXT NOT NULL REFERENCES courses(section_id),
                    student_id  TEXT NOT NULL REFERENCES students(student_id),
                    notes       TEXT DEFAULT '',
                    created_at  TEXT NOT NULL,
                    UNIQUE (section_id, student_id)
                );

                CREATE INDEX IF NOT EXISTS idx_reg_section ON registrations(section_id);
                CREATE INDEX IF NOT EXISTS idx_reg_student ON registrations(student_id);
                """
            )
            # Record the schema version so the check above is a single read.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- course sheet -------------------------------------------------------
    def reload_courses(self) -> dict:
        """Load courses.csv into SQLite and rebuild `course_index`.

        The CSV is the single source of truth for the course sheet.  Loading is
        an *upsert*: new rows are inserted, edited rows are updated, and rows
        that disappeared from the CSV are deleted.  Registrations are kept
        wherever possible because they reference `section_id`.

        Duplicate protection: while reading, `seen_sections` (a set) and the
        `course_index` dictionary make sure the same (year, term, code,
        section) is not loaded twice, even if the CSV contains it twice by
        accident.  The first copy wins and the row is counted as skipped.
        """
        if not os.path.exists(self.courses_csv):
            # No sheet at all is not fatal: the server simply has no courses.
            self.sheet_report = {"rows": 0, "loaded": 0, "skipped": 0, "sections": 0}
            return self.sheet_report

        rows: list[dict] = []          # rows that survived validation
        seen: set[str] = set()         # section ids already accepted
        index: dict[tuple[int, int], dict[str, list[str]]] = {}
        skipped = 0
        total = 0

        with open(self.courses_csv, newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                total += 1
                # Ignore completely blank lines silently.
                if not raw or not (raw.get("code") or "").strip():
                    continue

                # Normalise every field once, so the rest of the code can trust
                # the values (upper-case codes, trimmed text, ints for numbers).
                year = int(raw.get("year") or 0)
                term = int(raw.get("term") or 0)
                code = raw["code"].strip().upper()
                section = (raw.get("section") or "A").strip().upper()
                section_id = f"{year}-T{term}-{code}-{section}"

                # ---- code repetition guard ------------------------------
                # The dictionary for this term is created on first use, then
                # the section id list is checked before anything is stored.
                term_index = index.setdefault((year, term), {})
                if section_id in seen:
                    skipped += 1         # exact repeat of the same session
                    continue
                seen.add(section_id)
                # Append the section to its course code's list.  A course code
                # may legitimately appear many times (different sessions).
                term_index.setdefault(code, []).append(section_id)

                try:
                    capacity = int(raw["capacity"])
                except (TypeError, ValueError, KeyError):
                    capacity = 0

                rows.append({
                    "section_id": section_id,
                    "year": year,
                    "term": term,
                    "code": code,
                    "section": section,
                    "title": raw["title"].strip(),
                    "instructor": raw.get("instructor", "").strip(),
                    "department": raw.get("department", "").strip(),
                    "day": raw["day"].strip(),
                    "start": raw["start"].strip(),
                    "end": raw["end"].strip(),
                    "venue": raw["venue"].strip(),
                    "capacity": capacity,
                    "credits": int(raw.get("credits") or 3),
                    "description": (raw.get("description") or "").strip(),
                })

        with self.session() as conn:
            # Upsert each section.  Doing it row by row (rather than a giant
            # executemany with ON CONFLICT) keeps the SQL readable and lets us
            # delete vanished sections afterwards.
            for row in rows:
                conn.execute(
                    """INSERT INTO courses (section_id, year, term, code, section,
                                            title, instructor, department, day,
                                            start, end, venue, capacity, credits,
                                            description)
                       VALUES (:section_id, :year, :term, :code, :section, :title,
                               :instructor, :department, :day, :start, :end,
                               :venue, :capacity, :credits, :description)
                       ON CONFLICT(section_id) DO UPDATE SET
                           year=excluded.year, term=excluded.term,
                           code=excluded.code, section=excluded.section,
                           title=excluded.title, instructor=excluded.instructor,
                           department=excluded.department, day=excluded.day,
                           start=excluded.start, end=excluded.end,
                           venue=excluded.venue, capacity=excluded.capacity,
                           credits=excluded.credits, description=excluded.description""",
                    row,
                )
            # Remove sections that are no longer in the CSV.  Sections that
            # still have registrations are kept so no history is lost; the
            # console reports how many rows were affected.
            known = [r["section_id"] for r in rows]
            if known:
                placeholders = ",".join("?" * len(known))
                conn.execute(
                    f"""DELETE FROM courses
                         WHERE section_id NOT IN ({placeholders})
                           AND section_id NOT IN (SELECT section_id FROM registrations)""",
                    known,
                )

        # Publish the freshly built dictionary and the load report.
        self.course_index = index
        self.sheet_report = {
            "rows": total,
            "loaded": len(rows),
            "skipped": skipped,
            "sections": len(seen),
        }
        return self.sheet_report

    def reload_students(self) -> dict:
        """Fill the `students` dictionary from the students table.

        Called at start-up and after any change, so the dictionary and the
        database never disagree.  Keyed by student id, which makes "do we know
        this student?" an O(1) question.
        """
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM students").fetchall()
        self.students = {
            r["student_id"]: {
                "student_id": r["student_id"],
                "name": r["name"],
                "email": r["email"],
                "created_at": r["created_at"],
            }
            for r in rows
        }
        return self.students

    def _row_to_course(self, row: sqlite3.Row, enrolled: int) -> dict:
        """Turn a database row into the course dictionary the API returns.

        `enrolled`, `seats_left` and `is_full` are computed here so every
        caller (and therefore every screen) agrees on what "full" means.
        """
        course = dict(row)
        course["enrolled"] = enrolled
        course["seats_left"] = max(course["capacity"] - enrolled, 0)
        course["is_full"] = course["seats_left"] <= 0
        course["label"] = f"{course['code']}-{course['section']}"   # e.g. CS101-B
        return course

    def courses(self, year: int | None = None, term: int | None = None) -> list[dict]:
        """Return sections, optionally limited to one year/term.

        The LEFT JOIN counts registrations in SQL, so one query produces the
        whole sheet with live enrolment numbers.  Sorting is by term, then
        course code, then weekday/time, which is the order the timetable and
        the tables expect.
        """
        sql = """SELECT c.*, COUNT(r.id) AS enrolled
                   FROM courses c
                   LEFT JOIN registrations r ON r.section_id = c.section_id
                  WHERE 1 = 1"""
        args: list[int] = []
        if year is not None:
            sql += " AND c.year = ?"
            args.append(year)
        if term is not None:
            sql += " AND c.term = ?"
            args.append(term)
        sql += " GROUP BY c.section_id"
        with self.session() as conn:
            rows = conn.execute(sql, args).fetchall()

        # Sort in Python so the weekday order is Mon..Sun rather than A..Z,
        # and so the sessions of one course stay together (A, B, C...).
        courses = [self._row_to_course(r, r["enrolled"]) for r in rows]
        courses.sort(key=lambda c: (c["year"], c["term"], c["code"], c["section"],
                                    DAY_ORDER.index(c["day"]) if c["day"] in DAY_ORDER else 9,
                                    minutes(c["start"])))
        return courses

    def terms(self) -> list[dict]:
        """List every (year, term) that has courses, with a little summary.

        The client uses this to fill its term selector; the numbers are handy
        for showing "19 sections" next to each choice.
        """
        with self.session() as conn:
            rows = conn.execute(
                """SELECT year, term, COUNT(*) AS sections,
                          COUNT(DISTINCT code) AS courses,
                          SUM(capacity) AS seats
                     FROM courses GROUP BY year, term ORDER BY year, term"""
            ).fetchall()
        return [{
            "year": r["year"],
            "term": r["term"],
            "label": term_label(r["year"], r["term"]),
            "sections": r["sections"],
            "courses": r["courses"],
            "seats": r["seats"] or 0,
        } for r in rows]

    def sections_of(self, year: int, term: int, code: str) -> list[str]:
        """Return every section id of one course code via the dictionary.

        This is the O(1) hash lookup the requirement asks for: no SQL, no loop
        over the sheet, just two dictionary accesses.
        """
        return self.course_index.get((year, term), {}).get(code.strip().upper(), [])

    def resolve_section(self, year: int | None, term: int | None,
                        section_id: str = "", code: str = "") -> dict:
        """Find the one course section a request is talking about.

        Callers may send either the exact `section_id` (preferred - the client
        always does) or just a course `code` (handy for curl).  When only a code
        is given and the term has several sessions of it, an AMBIGUOUS_SECTION
        error asks for a section instead of silently picking one.
        """
        section_id = (section_id or "").strip()
        code = (code or "").strip().upper()

        if section_id:
            with self.session() as conn:
                row = conn.execute("SELECT * FROM courses WHERE section_id = ?",
                                   (section_id,)).fetchone()
            if row is None:
                raise ApiError(404, ERROR_NO_SUCH_COURSE,
                               f"Section {section_id} is not on the course sheet.")
            return self._row_to_course(row, self._enrolled(row["section_id"]))

        if not code:
            raise ApiError(400, ERROR_VALIDATION, "Please choose a course section.")
        if year is None or term is None:
            raise ApiError(400, ERROR_VALIDATION,
                           "Please choose a year and term before registering.")

        # Use the course dictionary to answer "which sessions does CS101 have?"
        matches = self.sections_of(year, term, code)
        if not matches:
            raise ApiError(404, ERROR_NO_SUCH_COURSE,
                           f"{code} is not offered in {term_label(year, term)}.")
        if len(matches) > 1:
            raise ApiError(409, ERROR_AMBIGUOUS,
                           f"{code} has {len(matches)} sessions in "
                           f"{term_label(year, term)}; please send the section id.")
        return self.resolve_section(year, term, section_id=matches[0])

    def _enrolled(self, section_id: str) -> int:
        """How many students already hold a place in this section."""
        with self.session() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM registrations WHERE section_id = ?",
                (section_id,)).fetchone()[0]

    # -- students -----------------------------------------------------------
    def get_student(self, student_id: str) -> dict | None:
        """Look a student up in the dictionary (None when unknown).

        This is what the client calls when the student id field loses focus, so
        a returning student never has to retype their name and e-mail.
        """
        return self.students.get((student_id or "").strip().upper())

    def list_students(self) -> list[dict]:
        """Return the student dictionary as a sorted list for the console."""
        return sorted(self.students.values(), key=lambda s: s["student_id"])

    def _remember_student(self, conn: sqlite3.Connection, student_id: str,
                          name: str, email: str) -> tuple[dict, bool]:
        """Return (student, created) for a registration attempt.

        If the id is already in the dictionary the stored details win: the
        name and e-mail the client typed are ignored, so a typo can never
        overwrite the record and no duplicate row is written.  A brand new id
        must come with a valid name and e-mail.
        """
        existing = self.students.get(student_id)

        if existing:
            # Known student -> reuse, and backfill if anything is missing.
            return existing, False

        # Unknown student -> the details are mandatory (validated by caller).
        # The INSERT uses the transaction of the caller (the same connection),
        # so the student and the registration are saved atomically: either both
        # rows land or neither does.
        record = {
            "student_id": student_id,
            "name": " ".join(name.split()),
            "email": email.strip(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        conn.execute(
            """INSERT INTO students (student_id, name, email, created_at)
               VALUES (?, ?, ?, ?)""",
            (record["student_id"], record["name"], record["email"],
             record["created_at"]))
        self.students[student_id] = record      # keep the dictionary in sync
        return record, True

    # -- registration -------------------------------------------------------
    def register(self, payload: dict) -> dict:
        """Register one student in one course section.

        Everything happens inside a single write transaction guarded by
        `self.lock`, in this order:

            1. validate the input
            2. find the section
            3. capacity check            -> FULL
            4. same section twice?       -> DUPLICATE
            5. another session of the
               same course code?         -> ONE_SESSION
            6. another class at the
               same time?                -> TIME_CONFLICT
            7. remember/lookup student   (the students dictionary)
            8. insert the registration

        Steps 3-6 are the business rules, so they live here and not in a GUI.
        """
        # ---- 1. read and normalise the payload ---------------------------
        year = payload.get("year")
        term = payload.get("term")
        year = int(year) if str(year or "").isdigit() else None
        term = int(term) if str(term or "").isdigit() else None

        section_id = (payload.get("section_id") or "").strip()
        code = (payload.get("course_code") or "").strip().upper()
        student_id = (payload.get("student_id") or "").strip().upper()
        name = " ".join((payload.get("student_name") or "").split())
        email = (payload.get("email") or "").strip()
        notes = (payload.get("notes") or "").strip()[:200]

        if not section_id and not code:
            raise ApiError(400, ERROR_VALIDATION, "Please choose a course section.")
        if not STUDENT_ID_RE.match(student_id):
            raise ApiError(400, ERROR_VALIDATION,
                           "Student ID must be 3-20 letters, digits, '-' or '_'.")

        # Name and e-mail are only required for a student we have never seen;
        # a returning student is recognised by the students dictionary.
        known = student_id in self.students
        if not known:
            if len(name) < 2:
                raise ApiError(400, ERROR_VALIDATION,
                               "Please enter the student's full name.")
            if not EMAIL_RE.match(email):
                raise ApiError(400, ERROR_VALIDATION,
                               "Please enter a valid e-mail address.")

        # ---- 2. locate the section --------------------------------------
        course = self.resolve_section(year, term, section_id, code)
        year, term = course["year"], course["term"]

        with self.lock:                         # one writer at a time
            with self.session() as conn:
                conn.execute("BEGIN IMMEDIATE")  # reserve the write lock

                # ---- 3. capacity check --------------------------------
                enrolled = conn.execute(
                    "SELECT COUNT(*) FROM registrations WHERE section_id = ?",
                    (course["section_id"],)).fetchone()[0]
                if enrolled >= course["capacity"]:
                    raise ApiError(409, ERROR_FULL,
                                   f"{course['label']} is full "
                                   f"({enrolled}/{course['capacity']} places taken).")

                # ---- 4. duplicate registration in this very section ----
                dupe = conn.execute(
                    """SELECT id FROM registrations
                        WHERE section_id = ? AND student_id = ?""",
                    (course["section_id"], student_id)).fetchone()
                if dupe:
                    raise ApiError(409, ERROR_DUPLICATE,
                                   f"{student_id} is already registered in "
                                   f"{course['label']}.")

                # ---- 5. one session per course code -------------------
                # Ask the course dictionary for every section of this code,
                # then look for a registration in any of the *other* ones.
                siblings = [sid for sid in self.sections_of(year, term, course["code"])
                            if sid != course["section_id"]]
                if siblings:
                    placeholders = ",".join("?" * len(siblings))
                    other = conn.execute(
                        f"""SELECT c.code, c.section FROM registrations r
                              JOIN courses c ON c.section_id = r.section_id
                             WHERE r.student_id = ? AND r.section_id IN ({placeholders})
                             LIMIT 1""",
                        [student_id, *siblings]).fetchone()
                    if other:
                        raise ApiError(
                            409, ERROR_ONE_SESSION,
                            f"{student_id} already has {other['code']} "
                            f"section {other['section']} in {term_label(year, term)}. "
                            f"Only one session per course can be taken.")

                # ---- 6. timetable clash -------------------------------
                # Only classes of the same year and term can clash.
                same_term = conn.execute(
                    """SELECT c.title, c.day, c.start, c.end, c.code
                         FROM registrations r
                         JOIN courses c ON c.section_id = r.section_id
                        WHERE r.student_id = ? AND c.year = ? AND c.term = ?""",
                    (student_id, year, term)).fetchall()
                for booked in same_term:
                    if booked["day"] == course["day"] and overlaps(
                            course["start"], course["end"],
                            booked["start"], booked["end"]):
                        raise ApiError(
                            409, ERROR_TIME_CONFLICT,
                            f"Timetable clash: {booked['code']} "
                            f"({booked['day']} {booked['start']}-{booked['end']}) "
                            f"overlaps {course['label']} "
                            f"({course['day']} {course['start']}-{course['end']}).")

                # ---- 7. remember the student --------------------------
                student, created = self._remember_student(conn, student_id, name, email)

                # ---- 8. store the registration ------------------------
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor = conn.execute(
                    """INSERT INTO registrations (section_id, student_id, notes, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (course["section_id"], student_id, notes, stamp))
                new_id = cursor.lastrowid
                conn.commit()

        # Build the response from data we already have (no second query).
        return {
            "id": new_id,
            "section_id": course["section_id"],
            "course_code": course["code"],
            "section": course["section"],
            "label": course["label"],
            "title": course["title"],
            "day": course["day"],
            "start": course["start"],
            "end": course["end"],
            "venue": course["venue"],
            "year": year,
            "term": term,
            "student_id": student_id,
            "student_name": student["name"],
            "email": student["email"],
            "notes": notes,
            "created_at": stamp,
            "new_student": created,        # lets the client say "welcome back"
        }

    def registrations(self, student_id: str = "", year: int | None = None,
                      term: int | None = None, course_code: str = "",
                      q: str = "") -> list[dict]:
        """Retrieve registrations with optional filters.

        This is the "server side retrieves the registration" endpoint.  Filters
        are optional and combine freely: one student's timetable, one term, one
        course, or a free-text search across name/id/e-mail/title.
        """
        sql = """SELECT r.id, r.section_id, r.notes, r.created_at,
                        c.code, c.section, c.title, c.day, c.start, c.end,
                        c.venue, c.year, c.term, c.capacity, c.credits,
                        r.student_id, s.name AS student_name, s.email
                   FROM registrations r
                   JOIN courses  c ON c.section_id = r.section_id
                   JOIN students s ON s.student_id = r.student_id
                  WHERE 1 = 1"""
        args: list = []

        if student_id:
            sql += " AND r.student_id = ?"
            args.append(student_id.strip().upper())
        if year is not None:
            sql += " AND c.year = ?"
            args.append(year)
        if term is not None:
            sql += " AND c.term = ?"
            args.append(term)
        if course_code:
            sql += " AND c.code = ?"
            args.append(course_code.strip().upper())
        if q:
            # Free-text search used by the server console.
            like = f"%{q.strip()}%"
            sql += """ AND (s.name LIKE ? OR r.student_id LIKE ? OR s.email LIKE ?
                           OR c.title LIKE ? OR c.code LIKE ? OR c.venue LIKE ?)"""
            args += [like] * 6

        sql += " ORDER BY c.year, c.term, c.code, c.section, s.name"
        with self.session() as conn:
            rows = conn.execute(sql, args).fetchall()

        # Add a friendly label for the GUI and keep the JSON flat.
        out = []
        for row in rows:
            record = dict(row)
            record["label"] = f"{record['code']}-{record['section']}"
            record["term_label"] = term_label(record["year"], record["term"])
            out.append(record)
        return out

    def cancel(self, registration_id: int) -> dict:
        """Delete one registration and return what was removed."""
        with self.lock, self.session() as conn:
            row = conn.execute("SELECT * FROM registrations WHERE id = ?",
                               (registration_id,)).fetchone()
            if row is None:
                raise ApiError(404, ERROR_NO_SUCH_REGISTRATION,
                               f"Registration #{registration_id} does not exist.")
            conn.execute("DELETE FROM registrations WHERE id = ?", (registration_id,))
            conn.commit()
        return dict(row)

    def stats(self, year: int | None = None, term: int | None = None) -> dict:
        """Numbers for the dashboard: totals, seat usage and full courses."""
        courses = self.courses(year, term)

        # The registration count has to respect the same term filter, so the
        # WHERE clause is assembled once and reused for both queries.
        where, args = " WHERE 1 = 1", []
        if year is not None:
            where += " AND c.year = ?"
            args.append(year)
        if term is not None:
            where += " AND c.term = ?"
            args.append(term)

        with self.session() as conn:
            total = conn.execute(
                f"""SELECT COUNT(*) FROM registrations r
                      JOIN courses c ON c.section_id = r.section_id{where}""",
                args).fetchone()[0]
            unique = conn.execute(
                f"""SELECT COUNT(DISTINCT r.student_id) FROM registrations r
                      JOIN courses c ON c.section_id = r.section_id{where}""",
                args).fetchone()[0]
            known_students = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]

        seats_total = sum(c["capacity"] for c in courses)
        seats_filled = sum(c["enrolled"] for c in courses)
        return {
            "year": year,
            "term": term,
            "total_registrations": total,
            "unique_students": unique,
            "known_students": known_students,
            "sections": len(courses),
            "courses": len({c["code"] for c in courses}),
            "seats_total": seats_total,
            "seats_filled": seats_filled,
            "seats_left": max(seats_total - seats_filled, 0),
            "full_courses": sum(1 for c in courses if c["is_full"]),
        }

    # -- demo data ----------------------------------------------------------
    def load_demo_data(self) -> int:
        """Insert a few sample registrations so the screens are not empty."""
        demo = [
            # (year, term, code, section, student id)
            (2026, 1, "CS101", "A", "S1001"),
            (2026, 1, "MA110", "A", "S1001"),
            (2026, 1, "CS205", "A", "S1002"),
            (2026, 1, "EN150", "A", "S1003"),
            (2026, 1, "PH120", "A", "S1004"),
            (2026, 1, "BU210", "A", "S1005"),
            (2026, 1, "PS140", "A", "S1006"),
            (2026, 1, "AR180", "A", "S1007"),
            (2026, 1, "CS330", "A", "S1008"),
            (2026, 1, "AI410", "A", "S1009"),
            (2026, 2, "CS102", "A", "S1001"),
            (2026, 2, "MA210", "A", "S1002"),
            (2027, 1, "CS101", "A", "S1010"),
        ]
        # Realistic names/e-mails so the console looks like real data.
        people = {
            "S1001": ("Amy Wong", "amy.wong@student.edu"),
            "S1002": ("Brian Lee", "brian.lee@student.edu"),
            "S1003": ("Chloe Fung", "chloe.fung@student.edu"),
            "S1004": ("Daniel Ip", "daniel.ip@student.edu"),
            "S1005": ("Eva Cheung", "eva.cheung@student.edu"),
            "S1006": ("Felix Tam", "felix.tam@student.edu"),
            "S1007": ("Grace Lau", "grace.lau@student.edu"),
            "S1008": ("Henry So", "henry.so@student.edu"),
            "S1009": ("Iris Ng", "iris.ng@student.edu"),
            "S1010": ("Jason Ho", "jason.ho@student.edu"),
        }
        added = 0
        for year, term, code, section, sid in demo:
            name, mail = people[sid]
            try:
                self.register({
                    "year": year, "term": term, "course_code": code,
                    "section_id": f"{year}-T{term}-{code}-{section}",
                    "student_id": sid, "student_name": name, "email": mail,
                    "notes": "demo",
                })
                added += 1
            except ApiError:
                pass          # already registered - ignore and move on
        return added


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class RegistrationHandler(BaseHTTPRequestHandler):
    """One request = one handler instance (standard library behaviour).

    The handler is deliberately thin: parse, delegate to CourseDB, serialise.
    All the interesting logic lives in CourseDB, which keeps this class short
    and makes it easy to call the same rules from tests.
    """

    server_version = "ClassRegistration/2.0"

    # These three are injected by create_server():
    db: CourseDB = None              # the shared database object
    log_callback = None              # optional callable(str) for the GUI log

    # -- small helpers ------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        """Route the built-in request log to the console or the GUI."""
        line = "%s - %s" % (self.address_string(), fmt % args)
        if self.log_callback:
            self.log_callback(line)

    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        """Send a JSON (or HTML) response with the CORS headers a browser needs."""
        body = payload if isinstance(payload, str) else json.dumps(
            payload, indent=2, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Allow a browser-based client to use the API from another origin.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        """Read the request body and decode it as a JSON object."""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, ERROR_BAD_JSON, "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, ERROR_BAD_JSON, "Request body must be a JSON object.")
        return data

    def _term_from(self, qs: dict) -> tuple[int | None, int | None]:
        """Pull year/term query parameters out of a parsed query string.

        Missing parameters stay None, which the database treats as "all terms".
        """
        def as_int(key: str) -> int | None:
            value = (qs.get(key) or [""])[0].strip()
            return int(value) if value.lstrip("-").isdigit() else None

        return as_int("year"), as_int("term")

    # -- HTTP verbs ---------------------------------------------------------
    # OPTIONS answers the browser's CORS pre-flight request.
    def do_OPTIONS(self) -> None:                     # noqa: N802
        self._send(204, "")

    def do_GET(self) -> None:                         # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:                        # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:                      # noqa: N802
        self._dispatch("DELETE")

    # -- routing ------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        """Route a request to its endpoint, always answering with JSON."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            # ---------------- HTML course sheet -------------------------
            if method == "GET" and path in ("/", "/index.html"):
                year, term = self._term_from(qs)
                return self._send(200, self._course_sheet_html(year, term), "text/html")

            # ---------------- health -------------------------------------
            if method == "GET" and path == "/api/health":
                return self._send(200, {
                    "status": "ok",
                    "terms": len(self.db.terms()),
                    "sections": self.db.sheet_report["sections"],
                    "students": len(self.db.students),
                    "time": datetime.now().isoformat(timespec="seconds"),
                })

            # ---------------- terms --------------------------------------
            if method == "GET" and path == "/api/terms":
                return self._send(200, {"terms": self.db.terms()})

            # ---------------- courses ------------------------------------
            if method == "GET" and path == "/api/courses":
                year, term = self._term_from(qs)
                courses = self.db.courses(year, term)
                return self._send(200, {
                    "year": year, "term": term,
                    "courses": courses, "count": len(courses),
                    # Expose the dictionary size so the client can show that a
                    # hash table (not a list scan) is doing the lookups.
                    "index_codes": len(self.db.course_index.get((year, term), {})),
                })

            # ------------- server side course search ---------------------
            if method == "GET" and path == "/api/courses/search":
                year, term = self._term_from(qs)
                query = (qs.get("q") or [""])[0].strip().lower()
                courses = self.db.courses(year, term)
                if query:
                    # Deliberately simple scan: the *client* implements the
                    # hash-table search required by the brief.  This endpoint
                    # exists so curl users get the same result.
                    courses = [c for c in courses if query in " ".join(
                        str(c[k]) for k in ("code", "section", "title", "instructor",
                                            "venue", "department", "day")).lower()]
                return self._send(200, {"courses": courses, "count": len(courses)})

            # ---------------- students (the dictionary) -----------------
            if method == "GET" and path == "/api/students":
                return self._send(200, {"students": self.db.list_students(),
                                        "count": len(self.db.students)})

            if method == "GET" and path.startswith("/api/students/"):
                student_id = path.rsplit("/", 1)[-1].upper()
                student = self.db.get_student(student_id)
                if student is None:
                    raise ApiError(404, ERROR_NO_SUCH_STUDENT,
                                   f"No student with id {student_id}.")
                # Include their registrations so the client can draw a
                # timetable for the right term in one request.
                return self._send(200, {
                    "student": student,
                    "registrations": self.db.registrations(student_id=student_id),
                })

            # ---------------- registrations -----------------------------
            if method == "GET" and path == "/api/registrations":
                year, term = self._term_from(qs)
                rows = self.db.registrations(
                    student_id=(qs.get("student_id") or [""])[0],
                    year=year, term=term,
                    course_code=(qs.get("course_code") or [""])[0],
                    q=(qs.get("q") or [""])[0])
                return self._send(200, {"registrations": rows, "count": len(rows)})

            if method == "POST" and path == "/api/registrations":
                reg = self.db.register(self._body())
                return self._send(201, {"registration": reg})

            if method == "DELETE" and path.startswith("/api/registrations/"):
                raw_id = path.rsplit("/", 1)[-1]
                if not raw_id.isdigit():
                    raise ApiError(400, ERROR_BAD_ID,
                                   "Registration id must be a number.")
                removed = self.db.cancel(int(raw_id))
                return self._send(200, {"deleted": removed["id"],
                                        "registration": removed})

            # ---------------- stats -------------------------------------
            if method == "GET" and path == "/api/stats":
                year, term = self._term_from(qs)
                return self._send(200, self.db.stats(year, term))

            # ---------------- nothing matched ---------------------------
            raise ApiError(404, ERROR_NO_ROUTE, f"No route for {method} {path}")

        except ApiError as err:
            # Expected, meaningful failures -> sent with their own status.
            self._send(err.status, {"error": err.message, "code": err.code})
        except Exception as err:                        # pragma: no cover
            # Anything else is a bug; answer 500 but keep the server alive.
            self._send(500, {"error": f"Server error: {err}", "code": ERROR_SERVER})

    # -- tiny web view of the course sheet ---------------------------------
    def _course_sheet_html(self, year: int | None, term: int | None) -> str:
        """Build a small read-only HTML page for the selected term.

        Handy for checking the server in a browser.  The term links at the top
        rebuild the page with ?year=&term= so no JavaScript is needed.
        """
        courses = self.db.courses(year, term)
        stats = self.db.stats(year, term)

        # One link per term, highlighting the one being shown.
        links = []
        for t in self.db.terms():
            active = "background:#3355d1;color:#fff" if (t["year"], t["term"]) == (
                year, term) else "background:#e6ebfb;color:#2743a8"
            links.append(
                f'<a href="/?year={t["year"]}&term={t["term"]}" style="{active};'
                f'border-radius:999px;padding:5px 12px;font-size:12px;'
                f'font-weight:700;text-decoration:none;margin-right:6px">'
                f'{t["label"]} ({t["sections"]})</a>')

        # One table row per section; full sections are greyed out.
        rows = []
        for c in courses:
            rows.append(
                f"<tr class='{'full' if c['is_full'] else ''}'>"
                f"<td><b>{c['code']}-{c['section']}</b></td><td>{c['title']}</td>"
                f"<td>{c['instructor']}</td>"
                f"<td>{c['day']} {c['start']}-{c['end']}</td><td>{c['venue']}</td>"
                f"<td class='num'>{c['enrolled']}/{c['capacity']}</td></tr>")

        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Course Sheet - Class Registration</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#eef1f8;
       color:#16202e;margin:0;padding:32px}}
 .wrap{{max-width:1040px;margin:0 auto;background:#fff;border:1px solid #d8dfec;
        border-radius:14px;padding:26px}}
 h1{{margin:0 0 4px;font-size:22px}} p.sub{{margin:0 0 16px;color:#66748a;font-size:13px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;background:#e2e7f2;color:#66748a;padding:9px;font-size:11px;
     text-transform:uppercase;letter-spacing:.04em}}
 td{{padding:9px;border-bottom:1px solid #eef1f8}}
 tr.full td{{color:#98a3b5}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
</style></head><body><div class="wrap">
<h1>Course Sheet</h1>
<p class="sub">{stats['courses']} courses / {stats['sections']} sections /
 {stats['seats_filled']} of {stats['seats_total']} places taken</p>
<p>{''.join(links)}</p>
<table><thead><tr><th>Code</th><th>Course</th><th>Instructor</th><th>Time</th>
<th>Venue</th><th>Enrolled</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="sub" style="margin-top:18px">JSON API: <code>/api/terms</code> &middot;
<code>/api/courses?year=2026&amp;term=1</code> &middot;
<code>/api/registrations</code> &middot; <code>/api/students</code></p>
</div></body></html>"""


def create_server(host: str = "0.0.0.0", port: int = 8000, db_path: str = DEFAULT_DB,
                  courses_csv: str = DEFAULT_COURSES,
                  log_callback=None) -> ThreadingHTTPServer:
    """Build (but do not start) an HTTP server bound to one CourseDB.

    A subclass of the handler is created per server so the database object and
    the log callback are attached without global state - important because the
    test suite starts several servers in one process.
    """
    db = CourseDB(db_path, courses_csv)
    handler = type(
        "BoundRegistrationHandler",
        (RegistrationHandler,),
        {"db": db,
         "log_callback": staticmethod(log_callback) if log_callback else None})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True      # never block shutdown on an open request
    httpd.db = db                    # handy for the GUI and for tests
    return httpd


def main() -> None:
    """Command line entry point: `python3 server.py [--port 8000] [--demo]`."""
    parser = argparse.ArgumentParser(description="Class Registration API server")
    parser.add_argument("--host", default="0.0.0.0", help="interface (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    parser.add_argument("--db", default=DEFAULT_DB, help="sqlite database file")
    parser.add_argument("--courses", default=DEFAULT_COURSES, help="course sheet CSV")
    parser.add_argument("--demo", action="store_true",
                        help="insert sample registrations on start")
    args = parser.parse_args()

    # The log callback prints every request with the standard `[server]` prefix.
    httpd = create_server(args.host, args.port, args.db, args.courses,
                          log_callback=lambda line: print("[server]", line, flush=True))

    if args.demo:
        print(f"[server] demo registrations added: {httpd.db.load_demo_data()}")

    # Startup summary: how many terms/sections/students were loaded.
    report = httpd.db.sheet_report
    terms = httpd.db.terms()
    print(f"[server] course sheet: {report['loaded']} sections in "
          f"{len(terms)} terms (duplicates skipped: {report['skipped']})")
    print(f"[server] students on file: {len(httpd.db.students)}")
    print(f"[server] listening on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        httpd.server_close()


# The classic guard: only run the server when this file is executed directly,
# not when server_gui.py imports it.
if __name__ == "__main__":
    main()
