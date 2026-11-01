#!/usr/bin/env python3
"""
server.py - Class Registration System: server side.

A small, dependency-free JSON API server built on the Python standard library
(http.server + sqlite3).  It owns the course sheet and every registration.

Run it on its own:
    python3 server.py                 # http://0.0.0.0:8000
    python3 server.py --port 9000 --demo

It is also imported by server_gui.py, which starts the same server in a
background thread and gives it a desktop console.

Endpoints
---------
GET    /api/health                       -> {"status": "ok", ...}
GET    /api/courses                      -> {"courses": [ ... ]}
GET    /api/registrations?course_code=&student_id=&q=
DELETE /api/registrations/<id>           -> {"deleted": <id>}
POST   /api/registrations                -> {"registration": { ... }}
GET    /api/stats                        -> summary numbers
GET    /                                 -> small HTML view of the course sheet

Every error answer looks like {"error": "...", "code": "FULL"} with a matching
HTTP status, so the client can react precisely.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COURSES = os.path.join(BASE_DIR, "courses.csv")
DEFAULT_DB = os.path.join(BASE_DIR, "data", "registration.db")

STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

COURSE_FIELDS = ("code", "title", "instructor", "department", "day",
                 "start", "end", "venue", "capacity", "credits", "description")


class ApiError(Exception):
    """An error that carries the HTTP status the client should see."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
class CourseDB:
    """SQLite storage for the course sheet and the registrations."""

    def __init__(self, db_path: str = DEFAULT_DB, courses_csv: str = DEFAULT_COURSES):
        self.db_path = db_path
        self.courses_csv = courses_csv
        self.lock = threading.Lock()
        folder = os.path.dirname(os.path.abspath(db_path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._create_schema()
        self.sync_courses()

    # -- plumbing ----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _create_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    code        TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    instructor  TEXT NOT NULL,
                    department  TEXT NOT NULL,
                    day         TEXT NOT NULL,
                    start       TEXT NOT NULL,
                    end         TEXT NOT NULL,
                    venue       TEXT NOT NULL,
                    capacity    INTEGER NOT NULL,
                    credits     INTEGER NOT NULL,
                    description TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS registrations (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_code  TEXT NOT NULL REFERENCES courses(code),
                    student_id   TEXT NOT NULL,
                    student_name TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    notes        TEXT DEFAULT '',
                    created_at   TEXT NOT NULL,
                    UNIQUE (course_code, student_id)
                );

                CREATE INDEX IF NOT EXISTS idx_reg_course  ON registrations(course_code);
                CREATE INDEX IF NOT EXISTS idx_reg_student ON registrations(student_id);
                """
            )

    # -- courses -----------------------------------------------------------
    def sync_courses(self) -> int:
        """Load/refresh the course sheet from courses.csv (upsert, keeps seats)."""
        if not os.path.exists(self.courses_csv):
            return 0
        rows = []
        with open(self.courses_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row.get("code"):
                    continue
                rows.append({
                    "code": row["code"].strip().upper(),
                    "title": row["title"].strip(),
                    "instructor": row["instructor"].strip(),
                    "department": row["department"].strip(),
                    "day": row["day"].strip(),
                    "start": row["start"].strip(),
                    "end": row["end"].strip(),
                    "venue": row["venue"].strip(),
                    "capacity": int(row["capacity"]),
                    "credits": int(row.get("credits") or 3),
                    "description": (row.get("description") or "").strip(),
                })
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO courses (code, title, instructor, department, day, start,
                                        end, venue, capacity, credits, description)
                   VALUES (:code, :title, :instructor, :department, :day, :start,
                           :end, :venue, :capacity, :credits, :description)
                   ON CONFLICT(code) DO UPDATE SET
                       title=excluded.title, instructor=excluded.instructor,
                       department=excluded.department, day=excluded.day,
                       start=excluded.start, end=excluded.end, venue=excluded.venue,
                       capacity=excluded.capacity, credits=excluded.credits,
                       description=excluded.description""",
                rows,
            )
        return len(rows)

    def courses(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.*, COUNT(r.id) AS enrolled
                     FROM courses c
                     LEFT JOIN registrations r ON r.course_code = c.code
                    GROUP BY c.code
                    ORDER BY c.code"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["seats_left"] = max(d["capacity"] - d["enrolled"], 0)
            d["is_full"] = d["seats_left"] <= 0
            out.append(d)
        return out

    def course(self, code: str) -> dict:
        code = (code or "").strip().upper()
        for c in self.courses():
            if c["code"] == code:
                return c
        raise ApiError(404, "NO_SUCH_COURSE", f"Course {code or '(blank)'} is not on the course sheet.")

    # -- registrations -----------------------------------------------------
    def register(self, payload: dict) -> dict:
        code = (payload.get("course_code") or "").strip().upper()
        student_id = (payload.get("student_id") or "").strip().upper()
        name = " ".join((payload.get("student_name") or "").split())
        email = (payload.get("email") or "").strip()
        notes = (payload.get("notes") or "").strip()[:200]

        # --- validation (fast, before touching the database) --------------
        if not code:
            raise ApiError(400, "VALIDATION", "Please choose a course.")
        if not STUDENT_ID_RE.match(student_id):
            raise ApiError(400, "VALIDATION",
                           "Student ID must be 3-20 letters, digits, '-' or '_'.")
        if len(name) < 2:
            raise ApiError(400, "VALIDATION", "Please enter the student's full name.")
        if not EMAIL_RE.match(email):
            raise ApiError(400, "VALIDATION", "Please enter a valid e-mail address.")

        with self.lock:                      # serialise the seat allocation
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                course = conn.execute("SELECT * FROM courses WHERE code = ?", (code,)).fetchone()
                if course is None:
                    raise ApiError(404, "NO_SUCH_COURSE", f"Course {code} is not on the course sheet.")

                enrolled = conn.execute(
                    "SELECT COUNT(*) FROM registrations WHERE course_code = ?", (code,)
                ).fetchone()[0]
                if enrolled >= course["capacity"]:
                    raise ApiError(409, "FULL",
                                   f"{code} is full ({enrolled}/{course['capacity']} places taken).")

                dupe = conn.execute(
                    "SELECT id FROM registrations WHERE course_code = ? AND student_id = ?",
                    (code, student_id)).fetchone()
                if dupe:
                    raise ApiError(409, "DUPLICATE",
                                   f"{student_id} is already registered in {code}.")

                # timetable clash check ------------------------------------
                clash = conn.execute(
                    """SELECT r.course_code, c.title, c.day, c.start, c.end
                         FROM registrations r JOIN courses c ON c.code = r.course_code
                        WHERE r.student_id = ? AND c.day = ?""",
                    (student_id, course["day"])).fetchall()
                for other in clash:
                    if course["start"] < other["end"] and other["start"] < course["end"]:
                        raise ApiError(
                            409, "TIME_CONFLICT",
                            f"Timetable clash: {other['course_code']} "
                            f"({other['day']} {other['start']}-{other['end']}) "
                            f"overlaps {code} ({course['day']} {course['start']}-{course['end']}).")

                created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    """INSERT INTO registrations
                           (course_code, student_id, student_name, email, notes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (code, student_id, name, email, notes, created))
                new_id = cur.lastrowid
                conn.commit()
                row = conn.execute(
                    """SELECT r.*, c.title, c.day, c.start, c.end, c.venue
                         FROM registrations r JOIN courses c ON c.code = r.course_code
                        WHERE r.id = ?""", (new_id,)).fetchone()
        return dict(row)

    def registrations(self, course_code: str = "", student_id: str = "", q: str = "") -> list[dict]:
        sql = """SELECT r.id, r.course_code, c.title, c.day, c.start, c.end, c.venue,
                        r.student_id, r.student_name, r.email, r.notes, r.created_at
                   FROM registrations r JOIN courses c ON c.code = r.course_code
                  WHERE 1 = 1"""
        args: list[str] = []
        if course_code:
            sql += " AND r.course_code = ?"
            args.append(course_code.strip().upper())
        if student_id:
            sql += " AND r.student_id = ?"
            args.append(student_id.strip().upper())
        if q:
            sql += (" AND (r.student_name LIKE ? OR r.student_id LIKE ?"
                    "      OR r.email LIKE ? OR c.title LIKE ? OR r.course_code LIKE ?)")
            args += [f"%{q}%"] * 5
        sql += " ORDER BY r.course_code, r.student_name"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def cancel(self, registration_id: int) -> dict:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM registrations WHERE id = ?",
                               (registration_id,)).fetchone()
            if row is None:
                raise ApiError(404, "NO_SUCH_REGISTRATION",
                               f"Registration #{registration_id} does not exist.")
            conn.execute("DELETE FROM registrations WHERE id = ?", (registration_id,))
            conn.commit()
        return dict(row)

    def stats(self) -> dict:
        courses = self.courses()
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
            students = conn.execute(
                "SELECT COUNT(DISTINCT student_id) FROM registrations").fetchone()[0]
        cap = sum(c["capacity"] for c in courses)
        return {
            "total_registrations": total,
            "unique_students": students,
            "courses": len(courses),
            "seats_total": cap,
            "seats_filled": sum(c["enrolled"] for c in courses),
            "seats_left": max(cap - sum(c["enrolled"] for c in courses), 0),
            "full_courses": sum(1 for c in courses if c["is_full"]),
        }

    # -- demo helper -------------------------------------------------------
    def load_demo_data(self) -> int:
        """Insert a handful of sample registrations (handy for trying things out)."""
        demo = [
            ("CS101", "S1001", "Amy Wong", "amy.wong@student.edu"),
            ("CS101", "S1002", "Brian Lee", "brian.lee@student.edu"),
            ("CS205", "S1003", "Chloe Fung", "chloe.fung@student.edu"),
            ("MA110", "S1001", "Amy Wong", "amy.wong@student.edu"),
            ("EN150", "S1004", "Daniel Ip", "daniel.ip@student.edu"),
            ("AR180", "S1002", "Brian Lee", "brian.lee@student.edu"),
            ("CS330", "S1005", "Eva Cheung", "eva.cheung@student.edu"),
            ("AI410", "S1006", "Felix Tam", "felix.tam@student.edu"),
            ("PS140", "S1007", "Grace Lau", "grace.lau@student.edu"),
            ("BU210", "S1008", "Henry So", "henry.so@student.edu"),
            ("PH120", "S1009", "Iris Ng", "iris.ng@student.edu"),
            ("CS205", "S1010", "Jason Ho", "jason.ho@student.edu"),
        ]
        added = 0
        for code, sid, name, mail in demo:
            try:
                self.register({"course_code": code, "student_id": sid,
                               "student_name": name, "email": mail, "notes": "demo"})
                added += 1
            except ApiError:
                pass                      # already there - ignore
        return added


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class RegistrationHandler(BaseHTTPRequestHandler):
    server_version = "ClassRegistration/1.0"
    db: CourseDB = None          # injected in create_server()
    log_callback = None          # optional callable(str) used by server_gui

    # -- helpers -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        line = "%s - %s" % (self.address_string(), fmt % args)
        if self.log_callback:
            self.log_callback(line)

    def _send(self, status: int, payload: dict | str, ctype="application/json") -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload, indent=2,
                                                                  ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "BAD_JSON", "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_JSON", "Request body must be a JSON object.")
        return data

    # -- verbs -------------------------------------------------------------
    def do_OPTIONS(self) -> None:            # noqa: N802 (browser pre-flight)
        self._send(204, "")

    def do_GET(self) -> None:                # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:               # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:             # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, self._course_sheet_html(), "text/html")
            if method == "GET" and path == "/api/health":
                return self._send(200, {"status": "ok",
                                        "courses": len(self.db.courses()),
                                        "time": datetime.now().isoformat(timespec="seconds")})
            if method == "GET" and path == "/api/courses":
                return self._send(200, {"courses": self.db.courses()})
            if method == "GET" and path == "/api/stats":
                return self._send(200, self.db.stats())
            if method == "GET" and path == "/api/registrations":
                qs = parse_qs(parsed.query)
                rows = self.db.registrations(
                    course_code=(qs.get("course_code") or [""])[0],
                    student_id=(qs.get("student_id") or [""])[0],
                    q=(qs.get("q") or [""])[0])
                return self._send(200, {"registrations": rows, "count": len(rows)})
            if method == "POST" and path == "/api/registrations":
                reg = self.db.register(self._body())
                return self._send(201, {"registration": reg})
            if method == "DELETE" and path.startswith("/api/registrations/"):
                raw_id = path.rsplit("/", 1)[-1]
                if not raw_id.isdigit():
                    raise ApiError(400, "BAD_ID", "Registration id must be a number.")
                removed = self.db.cancel(int(raw_id))
                return self._send(200, {"deleted": removed["id"], "registration": removed})
            raise ApiError(404, "NO_ROUTE", f"No route for {method} {path}")
        except ApiError as err:
            self._send(err.status, {"error": err.message, "code": err.code})
        except Exception as err:                       # pragma: no cover
            self._send(500, {"error": f"Server error: {err}", "code": "SERVER_ERROR"})

    # -- tiny web view of the course sheet ---------------------------------
    def _course_sheet_html(self) -> str:
        rows = "".join(
            "<tr class='{cls}'><td><b>{code}</b></td><td>{title}</td><td>{instructor}</td>"
            "<td>{day} {start}-{end}</td><td>{venue}</td><td class='num'>{enrolled}/{capacity}</td></tr>".format(
                cls="full" if c["is_full"] else "", **c)
            for c in self.db.courses())
        total = self.db.stats()
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Course Sheet - Class Registration</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#eef1f8;color:#16202e;margin:0;padding:32px}}
 .wrap{{max-width:1000px;margin:0 auto;background:#fff;border:1px solid #d8dfec;border-radius:14px;padding:26px}}
 h1{{margin:0 0 4px;font-size:22px}} p.sub{{margin:0 0 20px;color:#66748a;font-size:13px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;background:#e2e7f2;color:#66748a;padding:9px;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
 td{{padding:9px;border-bottom:1px solid #eef1f8}}
 tr.full td{{color:#98a3b5}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .pill{{display:inline-block;background:#e6ebfb;color:#2743a8;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:700}}
</style></head><body><div class="wrap">
<h1>Course Sheet</h1>
<p class="sub">{total['courses']} courses &middot; {total['seats_filled']} of {total['seats_total']} places taken
 &middot; <span class="pill">{total['seats_left']} places left</span></p>
<table><thead><tr><th>Code</th><th>Course</th><th>Instructor</th><th>Time</th><th>Venue</th><th>Enrolled</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="sub" style="margin-top:18px">JSON API: <code>/api/courses</code> &middot;
<code>/api/registrations</code> &middot; <code>/api/stats</code></p>
</div></body></html>"""


def create_server(host: str = "0.0.0.0", port: int = 8000, db_path: str = DEFAULT_DB,
                  courses_csv: str = DEFAULT_COURSES, log_callback=None) -> ThreadingHTTPServer:
    """Build (but do not start) an HTTP server bound to the given database."""
    db = CourseDB(db_path, courses_csv)
    handler = type("BoundRegistrationHandler", (RegistrationHandler,),
                   {"db": db, "log_callback": staticmethod(log_callback) if log_callback else None})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.db = db
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="Class Registration API server")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    ap.add_argument("--db", default=DEFAULT_DB, help="sqlite database file")
    ap.add_argument("--courses", default=DEFAULT_COURSES, help="course sheet CSV")
    ap.add_argument("--demo", action="store_true", help="insert sample registrations on start")
    args = ap.parse_args()

    httpd = create_server(args.host, args.port, args.db, args.courses,
                          log_callback=lambda line: print("[server]", line, flush=True))
    if args.demo:
        print(f"[server] demo registrations added: {httpd.db.load_demo_data()}")
    courses = httpd.db.courses()
    print(f"[server] course sheet loaded: {len(courses)} courses, "
          f"{sum(c['seats_left'] for c in courses)} places available")
    print(f"[server] listening on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
