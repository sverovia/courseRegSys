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
import csv                          # reading courses.csv and students.csv
import hashlib                      # PBKDF2 password hashing
import hmac                         # constant-time password comparison
import json                         # request / response bodies
import os                           # path handling and folder creation
import re                           # validation patterns
import secrets                      # unguessable session tokens
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
DEFAULT_STUDENTS = os.path.join(BASE_DIR, "students.csv")    # accounts + passwords
DEFAULT_DB = os.path.join(BASE_DIR, "data", "registration.db")

# The schema version is stored in the database with PRAGMA user_version, so an
# older file can be recognised and upgraded instead of silently misbehaving.
#   version 1 - flat course table (unsupported, moved aside)
#   version 2 - terms, sections, students table
#   version 3 - students gain password_hash, can_register and role
SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# Security settings
# ---------------------------------------------------------------------------
# Passwords are never stored in plain text.  PBKDF2-HMAC-SHA256 with a random
# per-account salt is used because it ships with the standard library and is
# deliberately slow to brute force.  120 000 iterations takes a few hundredths
# of a second here: unnoticeable at login, expensive in bulk.
PASSWORD_ITERATIONS = 120_000
PASSWORD_ALGORITHM = "pbkdf2_sha256"
SESSION_TTL_SECONDS = 8 * 60 * 60        # a login lasts one working day
TOKEN_BYTES = 24                         # ~32 characters of URL-safe entropy

# ---------------------------------------------------------------------------
# HARD-CODED ADMIN TEST ACCOUNT (requirement 1)
# ---------------------------------------------------------------------------
# This account exists only for testing and demonstrating the admin features: it
# is not read from students.csv and not stored in the database, so it can never
# be deleted or locked out by accident.  Because it is a *development* account
# it is deliberately obvious, and `main()` prints a warning when it is enabled.
#
# To switch it off in a real deployment set ADMIN_ACCOUNT = None (or pass
# `admin_account=None` to CourseDB); every admin endpoint then requires a real
# account whose role is "admin" in students.csv.
ADMIN_ACCOUNT = {
    "student_id": "ADMIN",
    "name": "System Administrator",
    "email": "admin@school.edu",
    "password": "admin2026",
    "role": "admin",
    "can_register": True,
}

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
ERROR_AUTH_REQUIRED = "AUTH_REQUIRED"    # 401 - no / expired session token
ERROR_BAD_LOGIN = "BAD_LOGIN"            # 401 - wrong id or password
ERROR_FORBIDDEN = "FORBIDDEN"            # 403 - authenticated but not allowed
ERROR_REGISTRATION_BLOCKED = "REGISTRATION_BLOCKED"   # 403 - access revoked
ERROR_STUDENT_EXISTS = "STUDENT_EXISTS"  # 409 - admin tried to add a known id
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


# ---------------------------------------------------------------------------
# Password hashing helpers
# ---------------------------------------------------------------------------
# The stored format is a single self-describing string:
#
#     pbkdf2_sha256$120000$<salt hex>$<hash hex>
#
# Keeping the algorithm, iteration count and salt inside the value means the
# cost can be raised later without invalidating existing passwords: old rows
# still verify, and `needs_rehash()` reports which ones should be upgraded.
def hash_password(password: str, *, salt: bytes | None = None,
                  iterations: int = PASSWORD_ITERATIONS) -> str:
    """Return the salted hash string for a plain-text password."""
    if salt is None:
        salt = secrets.token_bytes(16)          # a fresh random salt each time
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Check a plain-text password against a stored hash.

    `hmac.compare_digest` is used instead of `==` so the comparison time does
    not depend on how many leading characters matched - that timing difference
    is enough for an attacker to guess a hash byte by byte.
    """
    if not stored:
        return False                            # account without a password
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != PASSWORD_ALGORITHM:
            return False
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                       bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False                            # malformed stored value
    return hmac.compare_digest(expected.hex(), digest_hex)


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash was made with an older / weaker setting."""
    if not stored:
        return True
    try:
        algorithm, iterations, _, _ = stored.split("$")
    except ValueError:
        return True
    return algorithm != PASSWORD_ALGORITHM or int(iterations) < PASSWORD_ITERATIONS


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

    def __init__(self, db_path: str = DEFAULT_DB, courses_csv: str = DEFAULT_COURSES,
                 students_csv: str = DEFAULT_STUDENTS,
                 admin_account: dict | None = ADMIN_ACCOUNT):
        # Remember where the data lives so reloads can be repeated later.
        self.db_path = db_path
        self.courses_csv = courses_csv
        self.students_csv = students_csv

        # The hard-coded admin test account (requirement 1).  Passing None
        # disables it, which is what a real deployment should do.
        self.admin_account = admin_account

        # One re-entrant lock serialises every write.  SQLite is fine with
        # concurrent readers, but seat counting must not interleave, otherwise
        # two clients could both see "1 seat left" and both take it.
        self.lock = threading.RLock()

        # ------------------------------------------------------------------
        # The two dictionaries the requirements ask for.
        # ------------------------------------------------------------------
        # course_index[(year, term)][course_code] = [section_id, ...]
        self.course_index: dict[tuple[int, int], dict[str, list[str]]] = {}
        # students[student_id] = public details (never the password hash)
        self.students: dict[str, dict] = {}
        # student_secrets[student_id] = the salted password hash string.  Kept
        # in its own dictionary so a public listing can never leak it by
        # accident - the API only ever copies from `self.students`.
        self.student_secrets: dict[str, str] = {}
        # Active logins: token -> {"student_id", "role", "name", "issued", "expires"}
        self.sessions: dict[str, dict] = {}
        # Summary of the last students.csv load, shown in the console.
        self.student_report = {"rows": 0, "loaded": 0, "skipped": 0, "added": 0}
        # Statistics about the last CSV load, shown in the server console.
        self.sheet_report = {"rows": 0, "loaded": 0, "skipped": 0, "sections": 0}

        # Make sure the folder for the database file exists before connecting.
        folder = os.path.dirname(os.path.abspath(db_path))
        if folder:
            os.makedirs(folder, exist_ok=True)

        # Build (or upgrade) the schema, then fill the in-memory dictionaries
        # from disk so the API is ready the moment the constructor returns.
        self._prepare_database()      # create or upgrade the tables
        self.reload_courses()         # courses.csv  -> courses table + course_index
        self.reload_students()        # students table -> students dictionary
        self.sync_students_csv()      # students.csv  -> accounts, passwords, access

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
        """Create the tables, upgrade an older file, or set a broken one aside.

        Three cases, in order of preference:

            1. no file            -> create everything at SCHEMA_VERSION
            2. version 2 file     -> *migrate in place*: add the new student
                                     columns with ALTER TABLE, so registrations
                                     and course data are preserved untouched
            3. anything else      -> move the file aside (never delete it) and
                                     start a fresh database

        Case 2 is the interesting one: it is why upgrading this project does not
        wipe the demo registrations.
        """
        version = None
        if os.path.exists(self.db_path):
            try:
                with self.session() as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError:
                version = -1            # unreadable file - treat as broken

        if version == 2:
            self._migrate_v2_to_v3()
        elif version not in (None, SCHEMA_VERSION):
            backup = self.db_path + f".v{version}-backup"
            os.replace(self.db_path, backup)
            print(f"[server] incompatible database moved to "
                  f"{os.path.basename(backup)}")

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
                -- password_hash is the salted PBKDF2 string (never plain text);
                -- can_register is the per-student switch from requirement 3.
                CREATE TABLE IF NOT EXISTS students (
                    student_id    TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    email         TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    password_hash TEXT,
                    can_register  INTEGER NOT NULL DEFAULT 1,
                    role          TEXT NOT NULL DEFAULT 'student'
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

    def _migrate_v2_to_v3(self) -> None:
        """Upgrade a version 2 database without losing any rows.

        SQLite can add columns to an existing table, so the students table simply
        gains `password_hash`, `can_register` and `role`.  Existing students get
        the column defaults (registration allowed, role "student") and receive
        their password when `sync_students_csv()` runs straight afterwards.
        """
        with self.session() as conn:
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(students)").fetchall()}
            additions = {
                "password_hash": "ALTER TABLE students ADD COLUMN password_hash TEXT",
                "can_register": ("ALTER TABLE students ADD COLUMN can_register "
                                 "INTEGER NOT NULL DEFAULT 1"),
                "role": ("ALTER TABLE students ADD COLUMN role TEXT NOT NULL "
                         "DEFAULT 'student'"),
            }
            for column, statement in additions.items():
                if column not in columns:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        print("[server] database upgraded v2 -> v3 (students gained "
              "password_hash, can_register, role)")

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

        # Two parallel dictionaries: public details, and the secrets.  Splitting
        # them is a deliberate safety measure - every API response is built from
        # `self.students`, so a password hash cannot leak by carelessness.
        self.students = {r["student_id"]: self._row_to_student(r) for r in rows}
        self.student_secrets = {r["student_id"]: r["password_hash"] for r in rows}
        return self.students

    @staticmethod
    def _row_to_student(row: sqlite3.Row) -> dict:
        """Convert a students table row into the public dictionary entry.

        Note what is missing: password_hash.  The hash never leaves the server.
        """
        return {
            "student_id": row["student_id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            # SQLite has no boolean type; 1/0 is converted here so the JSON and
            # the client can use true/false.
            "can_register": bool(row["can_register"]),
            "created_at": row["created_at"],
        }

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

    def sync_students_csv(self) -> dict:
        """Load students.csv into the students table (requirement 4).

        The CSV is the "account list" the tutor hands out: one row per student
        with their initial password.  Loading it is an upsert:

            * a new student id      -> INSERT with a freshly hashed password
            * an existing id        -> UPDATE name/e-mail/role/access, and the
                                       password only when the CSV value is not
                                       blank (so a cleared cell means "keep the
                                       current password" rather than "remove it")

        The plain-text password is hashed here and never reaches the database or
        any API response.  A repeated student id in the CSV is skipped, so the
        dictionary guard from version 2 also protects this file.
        """
        if not os.path.exists(self.students_csv):
            # No account list is not fatal: the server still starts, and the
            # hard-coded admin can create accounts through the admin API.
            self.student_report = {"rows": 0, "loaded": 0, "skipped": 0, "added": 0}
            return self.student_report

        loaded = skipped = added = 0
        seen: set[str] = set()          # the "no repeated student" guard
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(self.students_csv, newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                student_id = (raw.get("student_id") or "").strip().upper()
                if not student_id or student_id in seen:
                    skipped += 1        # blank line or a repeated id
                    continue
                seen.add(student_id)

                name = " ".join((raw.get("name") or "").strip().split())
                email = (raw.get("email") or "").strip()
                password = raw.get("password") or ""
                # Accept the usual spellings for a false value; anything else
                # (including a blank cell) means "registration allowed".
                flag = str(raw.get("can_register", "1")).strip().lower()
                can_register = 0 if flag in ("0", "no", "false", "off", "n") else 1
                role = (raw.get("role") or "student").strip().lower()
                if role not in ("student", "admin"):
                    role = "student"

                existing = self.students.get(student_id)
                with self.session() as conn:
                    if existing is None:
                        conn.execute(
                            """INSERT INTO students (student_id, name, email,
                                                     created_at, password_hash,
                                                     can_register, role)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (student_id, name, email, stamp,
                             hash_password(password) if password else None,
                             can_register, role))
                        added += 1
                    else:
                        conn.execute(
                            """UPDATE students
                                  SET name = ?, email = ?, can_register = ?, role = ?
                                WHERE student_id = ?""",
                            (name or existing["name"], email or existing["email"],
                             can_register, role, student_id))
                        if password:
                            conn.execute(
                                "UPDATE students SET password_hash = ? "
                                " WHERE student_id = ?",
                                (hash_password(password), student_id))
                loaded += 1

        # Refresh both dictionaries so the change is live immediately.
        self.reload_students()
        self.student_report = {"rows": loaded + skipped, "loaded": loaded,
                               "skipped": skipped, "added": added}
        return self.student_report

    # -- accounts: adding students and switching registration on/off ---------
    def add_student(self, payload: dict) -> dict:
        """Create a new student account (requirement 2).

        Used by the admin API and by the console.  Everything is validated the
        same way a registration is, plus a minimum password length, and the
        students dictionary is checked first so an id can never be duplicated.
        """
        student_id = (payload.get("student_id") or "").strip().upper()
        name = " ".join((payload.get("name") or payload.get("student_name") or "").split())
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        role = (payload.get("role") or "student").strip().lower()
        can_register = payload.get("can_register", True)

        # ---- validation ----
        if not STUDENT_ID_RE.match(student_id):
            raise ApiError(400, ERROR_VALIDATION,
                           "Student ID must be 3-20 letters, digits, '-' or '_'.")
        if len(name) < 2:
            raise ApiError(400, ERROR_VALIDATION, "Please enter the student's full name.")
        if not EMAIL_RE.match(email):
            raise ApiError(400, ERROR_VALIDATION, "Please enter a valid e-mail address.")
        if len(password) < 6:
            raise ApiError(400, ERROR_VALIDATION,
                           "The password must be at least 6 characters long.")
        if role not in ("student", "admin"):
            raise ApiError(400, ERROR_VALIDATION, "Role must be 'student' or 'admin'.")

        # ---- the dictionary is the duplicate guard ----
        if student_id in self.students:
            raise ApiError(409, ERROR_STUDENT_EXISTS,
                           f"Student {student_id} already exists.")
        if self.admin_account and student_id == self.admin_account["student_id"]:
            raise ApiError(409, ERROR_STUDENT_EXISTS,
                           f"{student_id} is reserved for the built-in admin account.")

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock, self.session() as conn:
            conn.execute(
                """INSERT INTO students (student_id, name, email, created_at,
                                         password_hash, can_register, role)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (student_id, name, email, stamp, hash_password(password),
                 1 if can_register else 0, role))
        self.reload_students()
        return self.students[student_id]

    def set_registration_access(self, student_id: str, allowed: bool) -> dict:
        """Switch a student's ability to register on or off (requirement 3).

        Revoking access does NOT delete the student or their existing
        registrations - it only blocks new ones.  That is the behaviour a tutor
        wants when an account is suspended temporarily.
        """
        student_id = (student_id or "").strip().upper()
        student = self.students.get(student_id)
        if student is None:
            raise ApiError(404, ERROR_NO_SUCH_STUDENT,
                           f"No student with id {student_id}.")
        with self.lock, self.session() as conn:
            conn.execute("UPDATE students SET can_register = ? WHERE student_id = ?",
                         (1 if allowed else 0, student_id))
        self.students[student_id]["can_register"] = bool(allowed)
        return self.students[student_id]

    def set_password(self, student_id: str, password: str) -> dict:
        """Admin password reset, using the same hashing path as everything else."""
        student_id = (student_id or "").strip().upper()
        if student_id not in self.students:
            raise ApiError(404, ERROR_NO_SUCH_STUDENT,
                           f"No student with id {student_id}.")
        if len(password or "") < 6:
            raise ApiError(400, ERROR_VALIDATION,
                           "The password must be at least 6 characters long.")
        with self.lock, self.session() as conn:
            conn.execute("UPDATE students SET password_hash = ? WHERE student_id = ?",
                         (hash_password(password), student_id))
        self.student_secrets[student_id] = hash_password(password)
        return self.students[student_id]

    # -- login / sessions ----------------------------------------------------
    def authenticate(self, student_id: str, password: str) -> dict:
        """Check credentials and return the student's public record.

        Two places are consulted:

            1. the hard-coded admin test account (requirement 1), which exists
               even if the database is empty, and
            2. the students dictionary plus its stored password hash.

        The same generic message is used for "unknown id" and "wrong password"
        on purpose: telling an attacker which half was wrong is a gift.
        """
        student_id = (student_id or "").strip().upper()
        password = password or ""

        # ---- 1. built-in admin test account ----
        admin = self.admin_account
        if admin and student_id == admin["student_id"]:
            if hmac.compare_digest(password, admin["password"]):
                return {"student_id": admin["student_id"], "name": admin["name"],
                        "email": admin["email"], "role": "admin",
                        "can_register": True, "created_at": "-", "builtin": True}
            raise ApiError(401, ERROR_BAD_LOGIN, "Wrong student ID or password.")

        # ---- 2. ordinary accounts ----
        student = self.students.get(student_id)
        stored = self.student_secrets.get(student_id)
        if student is None or not verify_password(password, stored):
            raise ApiError(401, ERROR_BAD_LOGIN, "Wrong student ID or password.")

        # Upgrade a hash that was made with an older cost, now that we have the
        # plain password in hand for the first and only time.
        if needs_rehash(stored):
            with self.lock, self.session() as conn:
                conn.execute("UPDATE students SET password_hash = ? "
                             " WHERE student_id = ?",
                             (hash_password(password), student_id))
            self.student_secrets[student_id] = hash_password(password)

        return {**student, "builtin": False}

    def create_session(self, student_id: str, role: str, name: str) -> dict:
        """Issue a session token and remember it in `self.sessions`.

        Tokens are random (secrets.token_urlsafe) rather than derived from the
        student id, so they cannot be guessed or forged from public data.
        """
        token = secrets.token_urlsafe(TOKEN_BYTES)
        issued = datetime.now().timestamp()
        record = {
            "token": token,
            "student_id": student_id,
            "role": role,
            "name": name,
            "issued": issued,
            "expires": issued + SESSION_TTL_SECONDS,
        }
        with self.lock:
            # House-keeping: drop anything that has already expired.
            for key in [k for k, v in self.sessions.items()
                        if v["expires"] <= issued]:
                self.sessions.pop(key, None)
            self.sessions[token] = record
        return record

    def session_of(self, token: str | None) -> dict | None:
        """Return the live session for a token, or None when it is not valid."""
        if not token:
            return None
        record = self.sessions.get(token)
        if record is None:
            return None
        if record["expires"] <= datetime.now().timestamp():
            self.sessions.pop(token, None)      # expired - forget it
            return None
        return record

    def end_session(self, token: str | None) -> bool:
        """Log out: forget the token.  Returns True when it existed."""
        return self.sessions.pop(token, None) is not None

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
        # No password: this path exists for a student who registers before
        # their account appears in students.csv.  They cannot log in until an
        # admin (or the CSV) gives them one.
        conn.execute(
            """INSERT INTO students (student_id, name, email, created_at,
                                     password_hash, can_register, role)
               VALUES (?, ?, ?, ?, NULL, 1, 'student')""",
            (record["student_id"], record["name"], record["email"],
             record["created_at"]))
        record.update({"role": "student", "can_register": True})
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

        # ---- 1b. is this account allowed to register at all? -------------
        # Requirement 3: an admin can switch a student's registration ability
        # off.  The check lives here, in the rule engine, so it applies to the
        # client, to the console and to a raw curl call alike.
        account = self.students.get(student_id)
        if account is not None and not account.get("can_register", True):
            raise ApiError(403, ERROR_REGISTRATION_BLOCKED,
                           f"Registration is currently disabled for {student_id}. "
                           f"Please contact the course administrator.")

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

    def cancel(self, registration_id: int, student_id: str | None = None) -> dict:
        """Delete one registration and return what was removed.

        `student_id` is the owner check: when it is given, the registration must
        belong to that student.  The API passes the session's student id for a
        student login and None for an admin, so students can only drop their own
        classes while staff can drop anyone's.
        """
        with self.lock, self.session() as conn:
            row = conn.execute("SELECT * FROM registrations WHERE id = ?",
                               (registration_id,)).fetchone()
            if row is not None and student_id and row["student_id"] != student_id:
                raise ApiError(403, ERROR_FORBIDDEN,
                               "That registration belongs to another student.")
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
        # X-Auth-Token has to be listed here or a browser would block the
        # logged-in requests before they are even sent.
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token")
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

    # -- authentication helpers --------------------------------------------
    def _token(self, qs: dict) -> str | None:
        """Read the session token from the header, or from ?token= as a fallback.

        The header is what the client uses.  The query parameter exists so the
        HTML page in a browser and a quick `curl` can be logged in too.
        """
        header = self.headers.get("X-Auth-Token")
        if header:
            return header.strip()
        return (qs.get("token") or [""])[0].strip() or None

    def _session(self, qs: dict) -> dict:
        """Return the current session, or fail with 401 AUTH_REQUIRED."""
        record = self.db.session_of(self._token(qs))
        if record is None:
            raise ApiError(401, ERROR_AUTH_REQUIRED,
                           "Please sign in first (POST /api/login).")
        return record

    def _admin_session(self, qs: dict) -> dict:
        """Return the current session, requiring the admin role (403 otherwise)."""
        record = self._session(qs)
        if record["role"] != "admin":
            raise ApiError(403, ERROR_FORBIDDEN,
                           "This action requires an administrator account.")
        return record

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
                    "sessions": len(self.db.sessions),
                    "admin_test_account": bool(self.db.admin_account),
                    "time": datetime.now().isoformat(timespec="seconds"),
                })

            # ---------------- login / logout ----------------------------
            # These two are the only endpoints that do not need a token.
            if method == "POST" and path == "/api/login":
                body = self._body()
                student = self.db.authenticate(body.get("student_id", ""),
                                               body.get("password", ""))
                session = self.db.create_session(student["student_id"],
                                                 student["role"], student["name"])
                return self._send(200, {
                    "token": session["token"],
                    "expires_in": SESSION_TTL_SECONDS,
                    "role": student["role"],
                    "student": student,
                })

            if method == "POST" and path == "/api/logout":
                # Logging out is idempotent: an unknown token still answers ok.
                removed = self.db.end_session(self._token(qs))
                return self._send(200, {"logged_out": removed})

            if method == "GET" and path == "/api/me":
                session = self._session(qs)
                student = self.db.get_student(session["student_id"]) or {
                    "student_id": session["student_id"], "name": session["name"],
                    "role": session["role"], "can_register": True}
                return self._send(200, {
                    "student": student,
                    "role": session["role"],
                    "expires_in": max(
                        int(session["expires"] - datetime.now().timestamp()), 0),
                })

            # ---------------- terms --------------------------------------
            if method == "GET" and path == "/api/terms":
                self._session(qs)          # signed-in users only
                return self._send(200, {"terms": self.db.terms()})

            # ---------------- courses ------------------------------------
            if method == "GET" and path == "/api/courses":
                self._session(qs)          # signed-in users only
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
                self._session(qs)          # signed-in users only
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

            # ---------------- admin: student accounts --------------------
            # Every route under /api/admin/ requires an admin session.
            if path.startswith("/api/admin/"):
                self._admin_session(qs)

                # list every account with its access flag and class count
                if method == "GET" and path == "/api/admin/students":
                    with self.db.session() as conn:
                        counts = {r["student_id"]: r["total"] for r in conn.execute(
                            "SELECT student_id, COUNT(*) AS total "
                            "FROM registrations GROUP BY student_id")}
                    students = [dict(s, registrations=counts.get(s["student_id"], 0))
                                for s in self.db.list_students()]
                    return self._send(200, {"students": students,
                                            "count": len(students)})

                # create a new account (requirement 2)
                if method == "POST" and path == "/api/admin/students":
                    student = self.db.add_student(self._body())
                    return self._send(201, {"student": student})

                # /api/admin/students/<id>/access -> switch registration on/off
                if (method == "POST" and path.startswith("/api/admin/students/")
                        and path.endswith("/access")):
                    student_id = path.split("/")[4]
                    body = self._body()
                    if "can_register" not in body:
                        raise ApiError(400, ERROR_VALIDATION,
                                       "Send {\"can_register\": true} or false.")
                    student = self.db.set_registration_access(
                        student_id, bool(body["can_register"]))
                    return self._send(200, {"student": student})

                # /api/admin/students/<id>/password -> reset a password
                if (method == "POST" and path.startswith("/api/admin/students/")
                        and path.endswith("/password")):
                    student_id = path.split("/")[4]
                    student = self.db.set_password(student_id,
                                                   self._body().get("password", ""))
                    return self._send(200, {"student": student})

                # anything else under /api/admin/ -> clean 404
                raise ApiError(404, ERROR_NO_ROUTE,
                               f"No admin route for {method} {path}")

            # ---------------- students (the dictionary) -----------------
            if method == "GET" and path == "/api/students":
                # The full dictionary is staff information: admin only.
                self._admin_session(qs)
                return self._send(200, {"students": self.db.list_students(),
                                        "count": len(self.db.students)})

            if method == "GET" and path.startswith("/api/students/"):
                # A student may read their own record; staff may read any.
                session = self._session(qs)
                student_id = path.rsplit("/", 1)[-1].upper()
                if session["role"] != "admin" and session["student_id"] != student_id:
                    raise ApiError(403, ERROR_FORBIDDEN,
                                   "You can only view your own student record.")
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
                session = self._session(qs)
                year, term = self._term_from(qs)
                # A student can only ever ask for their own registrations: the
                # filter is forced from the session, never taken from the URL.
                # An admin may look at a named student (the console does this);
                # without a name they get their own timetable, not everyone's.
                wanted = (qs.get("student_id") or [""])[0].upper()
                student_id = (wanted or session["student_id"]) if session["role"] == "admin" \
                    else session["student_id"]
                rows = self.db.registrations(
                    student_id=student_id,
                    year=year, term=term,
                    course_code=(qs.get("course_code") or [""])[0],
                    q=(qs.get("q") or [""])[0])
                return self._send(200, {"registrations": rows, "count": len(rows)})

            if method == "POST" and path == "/api/registrations":
                session = self._session(qs)
                body = self._body()
                # The registration always belongs to the signed-in student.  An
                # admin may register on someone else's behalf by naming them.
                if session["role"] == "admin" and body.get("student_id"):
                    pass
                else:
                    if body.get("student_id") and \
                            body["student_id"].strip().upper() != session["student_id"]:
                        raise ApiError(403, ERROR_FORBIDDEN,
                                       "You can only register yourself.")
                    body["student_id"] = session["student_id"]
                    # No need to resend name and e-mail: the server already
                    # knows them, so they are filled in from the dictionary.
                    account = self.db.get_student(session["student_id"]) or {}
                    body.setdefault("student_name", account.get("name", ""))
                    body.setdefault("email", account.get("email", ""))
                reg = self.db.register(body)
                return self._send(201, {"registration": reg})

            if method == "DELETE" and path.startswith("/api/registrations/"):
                raw_id = path.rsplit("/", 1)[-1]
                if not raw_id.isdigit():
                    raise ApiError(400, ERROR_BAD_ID,
                                   "Registration id must be a number.")
                # Students may only cancel their own registrations.
                session = self._session(qs)
                removed = self.db.cancel(
                    int(raw_id),
                    student_id=None if session["role"] == "admin"
                    else session["student_id"])
                return self._send(200, {"deleted": removed["id"],
                                        "registration": removed})

            # ---------------- stats -------------------------------------
            if method == "GET" and path == "/api/stats":
                self._session(qs)          # signed-in users only
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
                  students_csv: str = DEFAULT_STUDENTS,
                  log_callback=None) -> ThreadingHTTPServer:
    """Build (but do not start) an HTTP server bound to one CourseDB.

    A subclass of the handler is created per server so the database object and
    the log callback are attached without global state - important because the
    test suite starts several servers in one process.
    """
    db = CourseDB(db_path, courses_csv, students_csv)
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
    parser.add_argument("--students", default=DEFAULT_STUDENTS,
                        help="student accounts CSV (ids and passwords)")
    parser.add_argument("--demo", action="store_true",
                        help="insert sample registrations on start")
    args = parser.parse_args()

    # The log callback prints every request with the standard `[server]` prefix.
    httpd = create_server(args.host, args.port, args.db, args.courses, args.students,
                          log_callback=lambda line: print("[server]", line, flush=True))

    if args.demo:
        print(f"[server] demo registrations added: {httpd.db.load_demo_data()}")

    # Startup summary: how many terms/sections/students were loaded.
    report = httpd.db.sheet_report
    terms = httpd.db.terms()
    print(f"[server] course sheet: {report['loaded']} sections in "
          f"{len(terms)} terms (duplicates skipped: {report['skipped']})")
    report_s = httpd.db.student_report
    print(f"[server] students on file: {len(httpd.db.students)} "
          f"({report_s['loaded']} from students.csv, "
          f"{report_s['skipped']} duplicate rows skipped)")
    if httpd.db.admin_account:
        # Deliberately loud: a built-in password must never ship unnoticed.
        admin = httpd.db.admin_account
        print(f"[server] WARNING dev-only admin account enabled: "
              f"{admin['student_id']} / {admin['password']}")
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
