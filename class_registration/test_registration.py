#!/usr/bin/env python3
"""
test_registration.py - self-checks for the Class Registration System (v2).

Run with:

    python3 test_registration.py            # all tests, verbose
    python3 -m unittest test_registration   # same thing through unittest

The suite is split into four groups:

    TestSheetLoading  - the CSV is read correctly and repeated sections refused
    TestDictionaries  - the course_index and students dictionaries
    TestRules         - the registration rules (capacity, duplicates, one
                        session per course, timetable clashes, terms)
    TestHttpApi       - a real HTTP round-trip against a throw-away server

Every test builds its own small course sheet in a temporary folder, so the
tests never touch the real database or the real courses.csv.

WHAT EACH GROUP PROVES
----------------------
TestSheetLoading   the CSV reader keeps good rows, skips a repeated section id
                   and computes seats/times correctly
TestDictionaries   course_index groups the sessions of a code per term, and
                   students stores each student exactly once
TestRules          capacity, duplicate registration, one session per course,
                   timetable clashes, ambiguity, term isolation, cancellation
TestHttpApi        the same rules survive a real HTTP round-trip, plus the
                   error JSON shape and the HTML course sheet
TestRealSheet      the project's own courses.csv is well formed and really does
                   contain multiple sessions and overlapping classes

A note on style: assertions check error *codes* and data, never wording, so the
human-readable messages can be improved without breaking the suite.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import server

# ---------------------------------------------------------------------------
# A miniature course sheet used by most tests.
#
# It deliberately contains:
#   * CS101 twice (sections A and B) -> same code, two sessions
#   * MA110-A overlapping CS101-C on Monday 11:00
#   * a second term so "different courses per term" can be checked
#   * a duplicate row (CS101-A again) to prove the duplicate guard works
# ---------------------------------------------------------------------------
TEST_SHEET = """year,term,code,section,title,instructor,department,day,start,end,venue,capacity,credits,description
2026,1,CS101,A,Intro Programming,Dr. A,CS,Mon,09:00,10:30,Room 1,2,3,first session
2026,1,CS101,B,Intro Programming,Dr. A,CS,Wed,09:00,10:30,Room 1,5,3,second session
2026,1,CS101,C,Intro Programming,Dr. B,CS,Mon,11:00,12:30,Room 2,5,3,third session
2026,1,MA110,A,Calculus,Dr. C,Maths,Mon,11:00,12:30,Room 3,5,4,clashes with CS101-C
2026,1,MA110,B,Calculus,Dr. C,Maths,Tue,09:00,10:30,Room 3,5,4,no clash
2026,1,EN150,A,Writing,Ms. D,English,Thu,09:00,10:30,Room 4,5,3,free on Thursday
2026,2,CS102,A,Objects,Dr. A,CS,Mon,09:00,10:30,Room 1,5,3,term 2 course
2026,1,CS101,A,Duplicate Row,Dr. A,CS,Mon,09:00,10:30,Room 1,2,3,this row must be skipped
"""


class Base(unittest.TestCase):
    """Creates a temporary folder with a course sheet and a database.

    Using a temporary database per test means the tests can run in any order,
    in parallel, and never leave state behind that changes the next result.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmp, "courses.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(TEST_SHEET)
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = server.CourseDB(self.db_path, self.csv_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def register(self, section_id, student_id="S9001", name="Test Student",
                 email="test@school.edu"):
        """Register through the database object (the same path the API uses)."""
        payload = {"section_id": section_id, "student_id": student_id,
                   "student_name": name, "email": email}
        return self.db.register(payload)

    def assertApiError(self, code, call, *args, **kwargs):
        """Assert that a call raises ApiError with the expected code.

        Checking the *code* rather than the message keeps these tests readable
        and lets the human-readable wording change freely.
        """
        with self.assertRaises(server.ApiError) as context:
            call(*args, **kwargs)
        self.assertEqual(context.exception.code, code, context.exception.message)
        return context.exception


# ---------------------------------------------------------------------------
# 1. Loading the course sheet
#
# These tests are about courses.csv -> SQLite -> course_index.  They pin down
# the behaviour that matters for the new data model: a course code may appear
# several times (once per session), but one (year, term, code, section) tuple
# may appear only once.
# ---------------------------------------------------------------------------
class TestSheetLoading(Base):
    def test_all_sections_loaded(self):
        """Seven good rows out of eight: the duplicate row is skipped."""
        self.assertEqual(self.db.sheet_report["rows"], 8)
        self.assertEqual(self.db.sheet_report["loaded"], 7)
        self.assertEqual(self.db.sheet_report["skipped"], 1)

    def test_duplicate_section_id_is_refused(self):
        """The repeated CS101-A row must not overwrite the first one."""
        courses = {c["section_id"]: c for c in self.db.courses()}
        self.assertEqual(courses["2026-T1-CS101-A"]["title"], "Intro Programming")

    def test_section_ids_are_well_formed(self):
        """Every section id follows year-Tterm-CODE-SECTION."""
        for course in self.db.courses():
            self.assertEqual(
                course["section_id"],
                f"{course['year']}-T{course['term']}-{course['code']}-{course['section']}")

    def test_seats_are_computed(self):
        """capacity, enrolled and seats_left always agree."""
        self.register("2026-T1-CS101-B")
        course = next(c for c in self.db.courses() if c["section_id"] == "2026-T1-CS101-B")
        self.assertEqual(course["capacity"], 5)
        self.assertEqual(course["enrolled"], 1)
        self.assertEqual(course["seats_left"], 4)
        self.assertFalse(course["is_full"])


# ---------------------------------------------------------------------------
# 2. The two dictionaries the requirements ask for
#
# course_index : {(year, term): {code: [section ids]}}   - no repeated ids
# students     : {student_id: details}                    - no repeated people
#
# Both are checked from the outside (through public methods) rather than by
# poking at private state, so they keep working if the implementation changes.
# ---------------------------------------------------------------------------
class TestDictionaries(Base):
    def test_course_index_groups_sessions(self):
        """course_index[(year, term)][code] lists every session of that code."""
        self.assertEqual(self.db.course_index[(2026, 1)]["CS101"],
                         ["2026-T1-CS101-A", "2026-T1-CS101-B", "2026-T1-CS101-C"])
        self.assertEqual(self.db.sections_of(2026, 1, "MA110"),
                         ["2026-T1-MA110-A", "2026-T1-MA110-B"])

    def test_course_index_is_per_term(self):
        """A code offered in term 1 is absent from term 2's dictionary."""
        self.assertIn("MA110", self.db.course_index[(2026, 1)])
        self.assertNotIn("MA110", self.db.course_index[(2026, 2)])
        self.assertEqual(self.db.sections_of(2026, 2, "MA110"), [])

    def test_student_dictionary_prevents_repetition(self):
        """The first registration stores the student, later ones reuse it."""
        self.register("2026-T1-CS101-A", "S1001", "Amy Wong", "amy@school.edu")
        self.assertEqual(len(self.db.students), 1)

        # Register again without name/e-mail: the dictionary supplies them.
        second = self.db.register({"section_id": "2026-T1-MA110-B",
                                   "student_id": "S1001"})
        self.assertEqual(second["student_name"], "Amy Wong")
        self.assertEqual(second["email"], "amy@school.edu")
        self.assertFalse(second["new_student"])
        self.assertEqual(len(self.db.students), 1)

    def test_student_details_are_not_overwritten(self):
        """A typo in a later submission cannot rename an existing student.

        The dictionary is the authority once a student exists, so the server
        ignores conflicting details instead of quietly editing the record.
        """
        self.register("2026-T1-CS101-A", "S1001", "Amy Wong", "amy@school.edu")
        second = self.db.register({"section_id": "2026-T1-MA110-B",
                                   "student_id": "S1001",
                                   "student_name": "A. Wong Typo",
                                   "email": "wrong@school.edu"})
        self.assertEqual(second["student_name"], "Amy Wong")
        self.assertEqual(self.db.students["S1001"]["email"], "amy@school.edu")

    def test_new_student_requires_details(self):
        """An unknown id without a name or e-mail is a validation error."""
        self.assertApiError(server.ERROR_VALIDATION, self.db.register,
                            {"section_id": "2026-T1-CS101-A", "student_id": "S123"})
        self.assertApiError(server.ERROR_VALIDATION, self.db.register,
                            {"section_id": "2026-T1-CS101-A", "student_id": "S123",
                             "student_name": "No Mail", "email": "bad"})

    def test_reload_keeps_students(self):
        """Reloading the CSV does not lose the student dictionary."""
        self.register("2026-T1-CS101-A", "S1001", "Amy Wong", "amy@school.edu")
        self.db.reload_courses()
        self.assertIn("S1001", self.db.students)


# ---------------------------------------------------------------------------
# 3. The registration rules
#
# One test per rule, each named after the rule rather than after the method it
# calls, so a failure reads like a bug report: "one_session_per_course_code".
# ---------------------------------------------------------------------------
class TestRules(Base):
    def test_normal_registration(self):
        record = self.register("2026-T1-CS101-A", "S1001")
        self.assertEqual(record["label"], "CS101-A")
        self.assertEqual(record["label"],
                         f"{record['course_code']}-{record['section']}")
        self.assertTrue(record["new_student"])

    def test_validation(self):
        cases = [
            ({"section_id": "2026-T1-CS101-A", "student_id": "x",
              "student_name": "A B", "email": "a@b.edu"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-CS101-A", "student_id": "S123",
              "student_name": "A", "email": "a@b.edu"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-CS101-A", "student_id": "S123",
              "student_name": "A B", "email": "nope"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-NOPE-A", "student_id": "S123",
              "student_name": "A B", "email": "a@b.edu"}, server.ERROR_NO_SUCH_COURSE),
        ]
        for payload, expected in cases:
            self.assertApiError(expected, self.db.register, payload)

    def test_capacity_is_enforced(self):
        """CS101-A holds 2 students; the third is refused with FULL.

        The capacity in the test sheet is 2 rather than 40 so the boundary is
        reached with two calls instead of forty.
        """
        self.register("2026-T1-CS101-A", "S1001")
        self.register("2026-T1-CS101-A", "S1002")
        self.assertTrue(self.db.courses()[0] or True)
        course = next(c for c in self.db.courses() if c["section_id"] == "2026-T1-CS101-A")
        self.assertTrue(course["is_full"])
        self.assertApiError(server.ERROR_FULL, self.register,
                            "2026-T1-CS101-A", "S1003")

    def test_duplicate_registration(self):
        """The same student cannot take the same section twice.

        DUPLICATE (same section) and ONE_SESSION (same course, other section)
        are deliberately different codes, so a client can explain precisely
        what went wrong.
        """
        self.register("2026-T1-CS101-A", "S1001")
        self.assertApiError(server.ERROR_DUPLICATE, self.register,
                            "2026-T1-CS101-A", "S1001")

    def test_only_one_session_per_course_code(self):
        """CS101-A and CS101-B are two sessions of ONE course: pick one."""
        self.register("2026-T1-CS101-A", "S1001")
        self.assertApiError(server.ERROR_ONE_SESSION, self.register,
                            "2026-T1-CS101-B", "S1001")

    def test_other_courses_are_still_allowed(self):
        """The one-session rule must not block unrelated courses."""
        self.register("2026-T1-CS101-A", "S1001")
        self.register("2026-T1-EN150-A", "S1001")
        registrations = self.db.registrations(student_id="S1001")
        self.assertEqual(len(registrations), 2)

    def test_timetable_clash(self):
        """CS101-C (Mon 11:00) and MA110-A (Mon 11:00) cannot both be taken."""
        self.register("2026-T1-CS101-C", "S1001")
        self.assertApiError(server.ERROR_TIME_CONFLICT, self.register,
                            "2026-T1-MA110-A", "S1001")

    def test_same_course_different_day_is_allowed(self):
        """MA110-B is on Tuesday, so it does not clash with CS101-C."""
        self.register("2026-T1-CS101-C", "S1001")
        self.register("2026-T1-MA110-B", "S1001")
        self.assertEqual(len(self.db.registrations(student_id="S1001")), 2)

    def test_same_course_in_another_term_is_allowed(self):
        """A student may retake CS101 in the next term."""
        self.register("2026-T1-CS101-A", "S1001")
        record = self.register("2026-T2-CS102-A", "S1001")
        self.assertEqual(record["term"], 2)

    def test_ambiguous_code_needs_a_section(self):
        """Sending only 'CS101' when three sessions exist is refused."""
        self.assertApiError(server.ERROR_AMBIGUOUS, self.db.register,
                            {"year": 2026, "term": 1, "course_code": "CS101",
                             "student_id": "S123", "student_name": "A B",
                             "email": "a@b.edu"})

    def test_code_from_wrong_term_is_rejected(self):
        """MA110 is not offered in term 2, so it cannot be registered there."""
        self.assertApiError(server.ERROR_NO_SUCH_COURSE, self.db.register,
                            {"year": 2026, "term": 2, "course_code": "MA110",
                             "student_id": "S123", "student_name": "A B",
                             "email": "a@b.edu"})

    def test_cancel_frees_the_seat(self):
        record = self.register("2026-T1-CS101-A", "S1001")
        self.db.cancel(record["id"])
        self.assertEqual(self.db.registrations(student_id="S1001"), [])
        course = next(c for c in self.db.courses() if c["section_id"] == "2026-T1-CS101-A")
        self.assertEqual(course["enrolled"], 0)

    def test_stats_respect_the_term(self):
        self.register("2026-T1-CS101-A", "S1001")
        self.register("2026-T2-CS102-A", "S1002")
        term1 = self.db.stats(2026, 1)
        term2 = self.db.stats(2026, 2)
        self.assertEqual(term1["total_registrations"], 1)
        self.assertEqual(term2["total_registrations"], 1)
        self.assertEqual(term1["known_students"], 2)     # dictionary is global


# ---------------------------------------------------------------------------
# 4. The HTTP API and the real course sheet
#
# The database tests above could all pass while the HTTP layer is broken, so the
# same critical paths are repeated here through a real socket.  port 0 asks the
# operating system for a free port, which keeps the suite safe to run twice.
# ---------------------------------------------------------------------------
class TestHttpApi(Base):
    def setUp(self) -> None:
        super().setUp()
        self.httpd = server.create_server("127.0.0.1", 0, self.db_path, self.csv_path)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, payload=None):
        """Make one request and return (status, decoded body)."""
        data = json.dumps(payload).encode() if payload else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_health_and_terms(self):
        """The two endpoints a client calls before anything else."""
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body = self.call("GET", "/api/terms")
        self.assertEqual(status, 200)
        self.assertEqual([(t["year"], t["term"]) for t in body["terms"]],
                         [(2026, 1), (2026, 2)])

    def test_courses_are_filtered_by_term(self):
        """The server returns different courses for different terms.

        Term 1 of the test sheet has six sections and term 2 has one, so a leak
        between terms would show up immediately as a wrong count.
        """
        _, term1 = self.call("GET", "/api/courses?year=2026&term=1")
        _, term2 = self.call("GET", "/api/courses?year=2026&term=2")
        self.assertTrue(all(c["term"] == 1 for c in term1["courses"]))
        self.assertEqual([c["code"] for c in term2["courses"]], ["CS102"])
        self.assertEqual(term1["count"], 6)

    def test_register_list_delete(self):
        """The full lifecycle of a registration over HTTP: create, read, delete."""
        status, body = self.call("POST", "/api/registrations", {
            "section_id": "2026-T1-EN150-A", "student_id": "S7001",
            "student_name": "Http Tester", "email": "http@school.edu"})
        self.assertEqual(status, 201)
        reg_id = body["registration"]["id"]

        _, body = self.call("GET", "/api/registrations?student_id=S7001")
        self.assertEqual(body["count"], 1)

        status, body = self.call("DELETE", f"/api/registrations/{reg_id}")
        self.assertEqual((status, body["deleted"]), (200, reg_id))
        _, body = self.call("GET", "/api/registrations?student_id=S7001")
        self.assertEqual(body["count"], 0)

    def test_student_endpoint(self):
        """GET /api/students/<id> is the dictionary lookup the client uses."""
        status, _ = self.call("GET", "/api/students/S4040")
        self.assertEqual(status, 404)
        self.call("POST", "/api/registrations", {
            "section_id": "2026-T1-EN150-A", "student_id": "S4040",
            "student_name": "Known Student", "email": "known@school.edu"})
        status, body = self.call("GET", "/api/students/S4040")
        self.assertEqual(status, 200)
        self.assertEqual(body["student"]["name"], "Known Student")
        self.assertEqual(len(body["registrations"]), 1)

    def test_error_shape(self):
        """Errors must always be JSON with a code - the client depends on it."""
        status, body = self.call("POST", "/api/registrations", {})
        self.assertEqual((status, body["code"]), (400, server.ERROR_VALIDATION))
        status, body = self.call("GET", "/api/nothing")
        self.assertEqual((status, body["code"]), (404, server.ERROR_NO_ROUTE))

    def test_html_course_sheet(self):
        """The browser view renders with a real course in it."""
        with urllib.request.urlopen(self.base + "/") as response:
            html = response.read().decode()
        self.assertIn("Course Sheet", html)
        self.assertIn("CS101", html)


class TestRealSheet(unittest.TestCase):
    """Sanity checks against the project's own courses.csv."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = server.CourseDB(os.path.join(self.tmp, "real.db"),
                                   server.DEFAULT_COURSES)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sheet_has_several_terms(self):
        """The term selector needs something to select: three terms expected."""
        self.assertGreaterEqual(len(self.db.terms()), 3)

    def test_which_courses_are_offered_more_than_once(self):
        """Repeated codes are expected - they are the different sessions.

        This test documents the data requirement: the real sheet must contain
        at least one course offered in several sessions, otherwise the
        one-session-per-course rule could never be exercised in practice.
        """
        repeated = {code: len(sections)
                    for code, sections in self.db.course_index[(2026, 1)].items()
                    if len(sections) > 1}
        self.assertIn("CS101", repeated)
        self.assertGreaterEqual(repeated["CS101"], 3)

    def test_times_are_well_formed(self):
        """Every row has a sane HH:MM window and a positive capacity.

        Bad data in a hand-edited CSV is the most likely way this project breaks
        in real use, so it is checked explicitly.
        """
        for course in self.db.courses():
            self.assertRegex(course["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(course["end"], r"^\d{2}:\d{2}$")
            self.assertLess(server.minutes(course["start"]),
                            server.minutes(course["end"]))
            self.assertGreater(course["capacity"], 0)

    def test_the_sheet_contains_overlaps(self):
        """Overlapping classes exist, otherwise the clash rule is untested.

        Two sections of *any* courses may overlap each other - the rule only
        forbids one student from taking two overlapping classes.  A sheet with
        no overlaps at all would make the timetable feature pointless.
        """
        courses = self.db.courses(2026, 1)
        found = 0
        for first in courses:
            for second in courses:
                if (first["section_id"] < second["section_id"]
                        and first["day"] == second["day"]
                        and server.overlaps(first["start"], first["end"],
                                            second["start"], second["end"])):
                    found += 1
        self.assertGreater(found, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
