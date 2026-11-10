#!/usr/bin/env python3
"""
test_registration.py - self-checks for the Class Registration System (v3).

Run with:

    python3 test_registration.py            # all tests, verbose
    python3 -m unittest test_registration   # same thing through unittest

The suite is split into six groups:

    TestSheetLoading - the CSV is read correctly and repeated sections refused
    TestDictionaries - the course_index and students dictionaries
    TestRules        - the registration rules (capacity, duplicates, one
                       session per course, clashes, terms, blocked accounts)
    TestAccounts     - passwords, account creation, access switching, sessions
                       and the hard-coded admin test account  (v3 features)
    TestHttpApi      - a real HTTP round-trip: login, tokens, roles, admin API
    TestRealSheet    - the project's own CSV files are well formed

Every test builds its own small course sheet and student list in a temporary
folder, so the tests never touch the real database or the real CSV files.

A note on style: assertions check error *codes* and data, never wording, so the
human-readable messages can be improved without breaking the suite.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
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

# A miniature account list: a normal student, a blocked student, an admin whose
# role comes from the CSV, and a repeated id that must be skipped.
TEST_STUDENTS = """student_id,name,email,password,can_register,role
S9001,Test Student,test@school.edu,secret123,1,student
S9002,Blocked Student,blocked@school.edu,blocked123,0,student
S9003,Teacher Admin,teacher@school.edu,teacher123,1,admin
S9001,Test Student Again,other@school.edu,secret123,1,student
"""


class Base(unittest.TestCase):
    """Creates a temporary folder with a course sheet, accounts and a database.

    Using a temporary database per test means the tests can run in any order,
    in parallel, and never leave state behind that changes the next result.
    """

    def setUp(self) -> None:
        # A throwaway directory per test: the CSVs and the SQLite file are
        # written fresh, so one test can never depend on another's leftovers.
        self.tmp = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmp, "courses.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(TEST_SHEET)
        self.students_path = os.path.join(self.tmp, "students.csv")
        with open(self.students_path, "w", encoding="utf-8") as handle:
            handle.write(TEST_STUDENTS)
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = server.CourseDB(self.db_path, self.csv_path, self.students_path)

    def tearDown(self) -> None:
        # sqlite3 connections are closed by CourseDB.close() in each test; this
        # only removes the files.
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
# the behaviour that matters for the data model: a course code may appear
# several times (once per session), but one (year, term, code, section) tuple
# may appear only once.
# ---------------------------------------------------------------------------
class TestSheetLoading(Base):
    """The CSV is the only source of course data - so it is checked first.

    If these tests fail, every test after them would be misleading, because
    they all assume the sheet loaded cleanly.
    """
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
# 2. The dictionaries the requirements ask for
#
# course_index : {(year, term): {code: [section ids]}}   - no repeated ids
# students     : {student_id: details}                    - no repeated people
# ---------------------------------------------------------------------------
class TestDictionaries(Base):
    """The v2 dictionaries: students and course codes, each stored once.

    The point of both tests is duplication: the same section twice or the same
    student twice must be refused rather than silently merged.
    """
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
        before = len(self.db.students)
        self.register("2026-T1-CS101-A", "S9101", "Amy Wong", "amy@school.edu")
        self.assertEqual(len(self.db.students), before + 1)

        # Register again without name/e-mail: the dictionary supplies them.
        second = self.db.register({"section_id": "2026-T1-MA110-B",
                                   "student_id": "S9101"})
        self.assertEqual(second["student_name"], "Amy Wong")
        self.assertEqual(second["email"], "amy@school.edu")
        self.assertFalse(second["new_student"])
        self.assertEqual(len(self.db.students), before + 1)

    def test_student_details_are_not_overwritten(self):
        """A typo in a later submission cannot rename an existing student.

        The dictionary is the authority once a student exists, so the server
        ignores conflicting details instead of quietly editing the record.
        """
        self.register("2026-T1-CS101-A", "S9102", "Amy Wong", "amy@school.edu")
        second = self.db.register({"section_id": "2026-T1-MA110-B",
                                   "student_id": "S9102",
                                   "student_name": "A. Wong Typo",
                                   "email": "wrong@school.edu"})
        self.assertEqual(second["student_name"], "Amy Wong")
        self.assertEqual(self.db.students["S9102"]["email"], "amy@school.edu")

    def test_new_student_requires_details(self):
        """An unknown id without a name or e-mail is a validation error."""
        self.assertApiError(server.ERROR_VALIDATION, self.db.register,
                            {"section_id": "2026-T1-CS101-A", "student_id": "S9123"})
        self.assertApiError(server.ERROR_VALIDATION, self.db.register,
                            {"section_id": "2026-T1-CS101-A", "student_id": "S9123",
                             "student_name": "No Mail", "email": "bad"})

    def test_reload_keeps_students(self):
        """Reloading the course CSV does not lose the student dictionary."""
        self.register("2026-T1-CS101-A", "S9103", "Amy Wong", "amy@school.edu")
        self.db.reload_courses()
        self.assertIn("S9103", self.db.students)

    def test_public_dictionary_has_no_password(self):
        """The dictionary the API can leak never contains the hash."""
        for student in self.db.students.values():
            self.assertNotIn("password_hash", student)
            self.assertNotIn("password", student)


# ---------------------------------------------------------------------------
# 3. The registration rules
#
# One test per rule, each named after the rule rather than after the method it
# calls, so a failure reads like a bug report: "one_session_per_course_code".
# ---------------------------------------------------------------------------
class TestRules(Base):
    """The registration rules, one test per refusal the server can produce.

    Capacity, duplicates, one-session-per-code and time clashes are the four
    rules; each test names the error code it expects, because the client shows
    the server's code-based message straight to the student.
    """
    def test_normal_registration(self):
        record = self.register("2026-T1-CS101-A", "S9201")
        self.assertEqual(record["label"], "CS101-A")
        self.assertTrue(record["new_student"])

    def test_validation(self):
        cases = [
            ({"section_id": "2026-T1-CS101-A", "student_id": "x",
              "student_name": "A B", "email": "a@b.edu"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-CS101-A", "student_id": "S1234",
              "student_name": "A", "email": "a@b.edu"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-CS101-A", "student_id": "S1234",
              "student_name": "A B", "email": "nope"}, server.ERROR_VALIDATION),
            ({"section_id": "2026-T1-NOPE-A", "student_id": "S1234",
              "student_name": "A B", "email": "a@b.edu"}, server.ERROR_NO_SUCH_COURSE),
        ]
        for payload, expected in cases:
            self.assertApiError(expected, self.db.register, payload)

    def test_capacity_is_enforced(self):
        """CS101-A holds 2 students; the third is refused with FULL.

        The capacity in the test sheet is 2 rather than 40 so the boundary is
        reached with two calls instead of forty.
        """
        self.register("2026-T1-CS101-A", "S9210")
        self.register("2026-T1-CS101-A", "S9211")
        course = next(c for c in self.db.courses() if c["section_id"] == "2026-T1-CS101-A")
        self.assertTrue(course["is_full"])
        self.assertApiError(server.ERROR_FULL, self.register,
                            "2026-T1-CS101-A", "S9212")

    def test_duplicate_registration(self):
        """The same student cannot take the same section twice.

        DUPLICATE (same section) and ONE_SESSION (same course, other section)
        are deliberately different codes, so a client can explain precisely
        what went wrong.
        """
        self.register("2026-T1-CS101-A", "S9220")
        self.assertApiError(server.ERROR_DUPLICATE, self.register,
                            "2026-T1-CS101-A", "S9220")

    def test_only_one_session_per_course_code(self):
        """CS101-A and CS101-B are two sessions of ONE course: pick one."""
        self.register("2026-T1-CS101-A", "S9230")
        self.assertApiError(server.ERROR_ONE_SESSION, self.register,
                            "2026-T1-CS101-B", "S9230")

    def test_other_courses_are_still_allowed(self):
        """The one-session rule must not block unrelated courses."""
        self.register("2026-T1-CS101-A", "S9240")
        self.register("2026-T1-EN150-A", "S9240")
        self.assertEqual(len(self.db.registrations(student_id="S9240")), 2)

    def test_timetable_clash(self):
        """CS101-C (Mon 11:00) and MA110-A (Mon 11:00) cannot both be taken."""
        self.register("2026-T1-CS101-C", "S9250")
        self.assertApiError(server.ERROR_TIME_CONFLICT, self.register,
                            "2026-T1-MA110-A", "S9250")

    def test_same_course_different_day_is_allowed(self):
        """MA110-B is on Tuesday, so it does not clash with CS101-C."""
        self.register("2026-T1-CS101-C", "S9260")
        self.register("2026-T1-MA110-B", "S9260")
        self.assertEqual(len(self.db.registrations(student_id="S9260")), 2)

    def test_same_course_in_another_term_is_allowed(self):
        """A student may take a course again in the next term."""
        self.register("2026-T1-CS101-A", "S9270")
        record = self.register("2026-T2-CS102-A", "S9270")
        self.assertEqual(record["term"], 2)

    def test_ambiguous_code_needs_a_section(self):
        """Sending only 'CS101' when three sessions exist is refused."""
        self.assertApiError(server.ERROR_AMBIGUOUS, self.db.register,
                            {"year": 2026, "term": 1, "course_code": "CS101",
                             "student_id": "S9280", "student_name": "A B",
                             "email": "a@b.edu"})

    def test_code_from_wrong_term_is_rejected(self):
        """MA110 is not offered in term 2, so it cannot be registered there."""
        self.assertApiError(server.ERROR_NO_SUCH_COURSE, self.db.register,
                            {"year": 2026, "term": 2, "course_code": "MA110",
                             "student_id": "S9281", "student_name": "A B",
                             "email": "a@b.edu"})

    def test_cancel_frees_the_seat(self):
        record = self.register("2026-T1-CS101-A", "S9290")
        self.db.cancel(record["id"])
        self.assertEqual(self.db.registrations(student_id="S9290"), [])
        course = next(c for c in self.db.courses() if c["section_id"] == "2026-T1-CS101-A")
        self.assertEqual(course["enrolled"], 0)

    def test_cancel_ownership_is_checked(self):
        """A student cannot cancel a registration that is not theirs."""
        record = self.register("2026-T1-CS101-A", "S9295")
        self.assertApiError(server.ERROR_FORBIDDEN, self.db.cancel,
                            record["id"], student_id="S9002")
        # The owner (and an admin, who passes student_id=None) may cancel.
        self.db.cancel(record["id"], student_id="S9295")
        self.assertEqual(self.db.registrations(student_id="S9295"), [])

    def test_stats_respect_the_term(self):
        self.register("2026-T1-CS101-A", "S9300")
        self.register("2026-T2-CS102-A", "S9301")
        term1 = self.db.stats(2026, 1)
        term2 = self.db.stats(2026, 2)
        self.assertEqual(term1["total_registrations"], 1)
        self.assertEqual(term2["total_registrations"], 1)
        self.assertEqual(term1["unique_students"], 1)
        # known_students counts the whole dictionary, including the CSV accounts.
        self.assertGreaterEqual(term1["known_students"], len(self.db.students) - 1)


# ---------------------------------------------------------------------------
# 4. Accounts: passwords, creation, access and sessions  (v3 features)
# ---------------------------------------------------------------------------
class TestAccounts(Base):
    """Every account rule from the v3 brief, tested without a network.

    The group is ordered after the brief itself:

        requirement 4 - passwords live in students.csv and are hashed
        requirement 1 - the hard-coded admin test account
        requirement 2 - adding a student to the dictionary
        requirement 3 - switching a student's ability to register
        requirement 5 - login, session tokens and password resets

    Each test asserts on behaviour (a code, a value) rather than on wording, so
    the messages a user sees can be reworded safely.
    """

    # -- requirement 4: passwords in the CSV -------------------------------
    def test_students_csv_is_loaded(self):
        """Every unique row of students.csv becomes an account.

        The fixture deliberately repeats S9001, so this test also proves the
        "no repeated student" guard works on the account file, not just on the
        course sheet.
        """
        self.assertEqual(self.db.student_report["loaded"], 3)   # 4 rows, 1 repeat
        self.assertEqual(self.db.student_report["skipped"], 1)
        self.assertIn("S9001", self.db.students)
        self.assertIn("S9003", self.db.students)

    def test_passwords_are_hashed_not_stored(self):
        """The stored value is a salted PBKDF2 string, never the password.

        Two assertions matter here: the plain text must not appear anywhere in
        the stored value, and two hashes of the *same* password must differ -
        which is what the random per-account salt guarantees.
        """
        stored = self.db.student_secrets["S9001"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertNotIn("secret123", stored)
        # Two accounts with the same password still get different hashes,
        # because each one gets its own random salt.
        self.assertNotEqual(server.hash_password("same"), server.hash_password("same"))

    def test_password_verification(self):
        self.assertTrue(server.verify_password("secret123",
                                               self.db.student_secrets["S9001"]))
        self.assertFalse(server.verify_password("wrong",
                                                self.db.student_secrets["S9001"]))
        self.assertFalse(server.verify_password("anything", None))

    def test_role_comes_from_the_csv(self):
        """The role column decides who may use the admin endpoints."""
        self.assertEqual(self.db.students["S9003"]["role"], "admin")
        self.assertEqual(self.db.students["S9001"]["role"], "student")

    def test_can_register_flag_from_csv(self):
        """A student can ship already blocked, straight from the CSV."""
        self.assertTrue(self.db.students["S9001"]["can_register"])
        self.assertFalse(self.db.students["S9002"]["can_register"])

    # -- requirement 1: hard-coded admin test account ----------------------
    def test_admin_test_account_exists_without_the_database(self):
        """The built-in admin works even with a completely empty sheet."""
        empty = os.path.join(self.tmp, "empty.db")
        with open(os.path.join(self.tmp, "empty_students.csv"), "w") as handle:
            handle.write("student_id,name,email,password,can_register,role\n")
        db = server.CourseDB(empty, self.csv_path,
                             os.path.join(self.tmp, "empty_students.csv"))
        self.assertEqual(db.students, {})            # nobody in the dictionary
        admin = db.authenticate("ADMIN", server.ADMIN_ACCOUNT["password"])
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["builtin"])

    def test_admin_account_rejects_a_wrong_password(self):
        """The built-in account is still password-protected."""
        self.assertApiError(server.ERROR_BAD_LOGIN, self.db.authenticate,
                            "ADMIN", "not-the-password")

    def test_admin_account_can_be_disabled(self):
        """A real deployment passes admin_account=None and gets no back door."""
        db = server.CourseDB(os.path.join(self.tmp, "noadmin.db"), self.csv_path,
                             self.students_path, admin_account=None)
        self.assertApiError(server.ERROR_BAD_LOGIN, db.authenticate,
                            "ADMIN", server.ADMIN_ACCOUNT["password"])

    # -- requirement 2: adding students ------------------------------------
    def test_add_student(self):
        """A new account is usable immediately - including for logging in."""
        student = self.db.add_student({"student_id": "S9401", "name": "New Person",
                                       "email": "new@school.edu",
                                       "password": "newpass1"})
        self.assertEqual(student["student_id"], "S9401")
        self.assertTrue(student["can_register"])
        # The new account can log in straight away.
        self.assertEqual(self.db.authenticate("S9401", "newpass1")["name"], "New Person")

    def test_add_student_duplicate_is_refused(self):
        """The students dictionary is the duplicate guard for account creation."""
        self.assertApiError(server.ERROR_STUDENT_EXISTS, self.db.add_student,
                            {"student_id": "S9001", "name": "Copy Cat",
                             "email": "copy@school.edu", "password": "copypass"})

    def test_add_student_validation(self):
        """Bad id, short name, bad e-mail and a weak password are all refused.

        One test for four rules keeps the fixture small while still pinning down
        that `add_student` validates rather than trusting its caller.
        """
        bad_payloads = [
            {"student_id": "x", "name": "A B", "email": "a@b.edu", "password": "abcdef"},
            {"student_id": "S9402", "name": "A", "email": "a@b.edu", "password": "abcdef"},
            {"student_id": "S9403", "name": "A B", "email": "bad", "password": "abcdef"},
            {"student_id": "S9404", "name": "A B", "email": "a@b.edu", "password": "123"},
        ]
        for payload in bad_payloads:
            self.assertApiError(server.ERROR_VALIDATION, self.db.add_student, payload)

    def test_add_student_can_start_blocked(self):
        student = self.db.add_student({"student_id": "S9405", "name": "Held Back",
                                       "email": "held@school.edu",
                                       "password": "heldpass", "can_register": False})
        self.assertFalse(student["can_register"])

    # -- requirement 3: switching registration ability ---------------------
    def test_blocked_account_cannot_register(self):
        """S9002 is blocked in the CSV, so registering fails with 403."""
        self.assertApiError(server.ERROR_REGISTRATION_BLOCKED, self.register,
                            "2026-T1-CS101-A", "S9002")

    def test_access_can_be_switched_back_on(self):
        self.db.set_registration_access("S9002", True)
        record = self.register("2026-T1-CS101-A", "S9002")
        self.assertEqual(record["label"], "CS101-A")

    def test_access_can_be_switched_off(self):
        self.db.set_registration_access("S9001", False)
        self.assertApiError(server.ERROR_REGISTRATION_BLOCKED, self.register,
                            "2026-T1-CS101-A", "S9001")

    def test_blocking_keeps_existing_registrations(self):
        """Blocking stops new registrations but never deletes the old ones.

        This is the difference between "suspended" and "deleted": a student who
        is blocked can still see (and drop) the classes they already hold.
        """
        record = self.register("2026-T1-CS101-A", "S9001")
        self.db.set_registration_access("S9001", False)
        self.assertEqual(len(self.db.registrations(student_id="S9001")), 1)
        self.db.cancel(record["id"], student_id="S9001")
        self.assertEqual(self.db.registrations(student_id="S9001"), [])

    def test_access_switch_needs_a_known_student(self):
        self.assertApiError(server.ERROR_NO_SUCH_STUDENT,
                            self.db.set_registration_access, "S0000", True)

    # -- requirement 5: login and sessions ---------------------------------
    def test_login_returns_the_public_record(self):
        student = self.db.authenticate("s9001", "secret123")   # lower case id
        self.assertEqual(student["student_id"], "S9001")
        self.assertNotIn("password_hash", student)

    def test_login_rejects_bad_credentials(self):
        self.assertApiError(server.ERROR_BAD_LOGIN, self.db.authenticate,
                            "S9001", "wrong")
        self.assertApiError(server.ERROR_BAD_LOGIN, self.db.authenticate,
                            "S0000", "secret123")

    def test_sessions_are_issued_and_expire(self):
        """A token is only valid until its expiry time.

        Rather than sleeping for eight hours, the expiry timestamp of the live
        session is moved into the past - the same code path a real timeout
        takes, without the wait.
        """
        session = self.db.create_session("S9001", "student", "Test Student")
        self.assertTrue(session["token"])
        self.assertIsNotNone(self.db.session_of(session["token"]))
        # Forcing the expiry into the past makes the token invalid at once.
        self.db.sessions[session["token"]]["expires"] = 0
        self.assertIsNone(self.db.session_of(session["token"]))

    def test_logout_forgets_the_token(self):
        session = self.db.create_session("S9001", "student", "Test Student")
        self.assertTrue(self.db.end_session(session["token"]))
        self.assertIsNone(self.db.session_of(session["token"]))
        self.assertFalse(self.db.end_session(session["token"]))

    def test_tokens_are_unguessable(self):
        """Two logins produce different, long, random tokens.

        Deriving the token from the student id would let anyone forge one by
        guessing an id; random tokens make that impossible.
        """
        first = self.db.create_session("S9001", "student", "A")["token"]
        second = self.db.create_session("S9001", "student", "A")["token"]
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 24)

    def test_password_reset(self):
        """An admin reset invalidates the old password immediately."""
        self.db.set_password("S9001", "brand-new-1")
        self.assertApiError(server.ERROR_BAD_LOGIN, self.db.authenticate,
                            "S9001", "secret123")
        self.assertEqual(self.db.authenticate("S9001", "brand-new-1")["student_id"],
                         "S9001")


# ---------------------------------------------------------------------------
# 5. The HTTP API
#
# The database tests above could all pass while the HTTP layer is broken, so the
# same critical paths are repeated here through a real socket.  port 0 asks the
# operating system for a free port, which keeps the suite safe to run twice.
# ---------------------------------------------------------------------------
class TestHttpApi(Base):
    """End-to-end checks through a real socket and real HTTP headers.

    This group is what proves the *login system* rather than just the password
    check: it exercises the X-Auth-Token header, the 401/403 split, and the
    rule that a student's identity always comes from the session instead of
    from the request body.
    """

    def setUp(self) -> None:
        super().setUp()
        self.httpd = server.create_server("127.0.0.1", 0, self.db_path, self.csv_path,
                                          self.students_path)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # Tokens for the two roles used by most tests.
        self.student_token = self.login("S9001", "secret123")
        self.admin_token = self.login("ADMIN", server.ADMIN_ACCOUNT["password"])

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    # -- helpers -----------------------------------------------------------
    def call(self, method, path, payload=None, token=None):
        """Make one request and return (status, decoded body)."""
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Auth-Token"] = token
        request = urllib.request.Request(self.base + path, data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def login(self, student_id, password):
        """Log in through the API and return the session token."""
        status, body = self.call("POST", "/api/login",
                                 {"student_id": student_id, "password": password})
        self.assertEqual(status, 200, body)
        return body["token"]

    # -- login -------------------------------------------------------------
    def test_health_is_public(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertTrue(body["admin_test_account"])

    def test_protected_endpoints_need_a_token(self):
        """Without a session the sheet is not readable at all.

        The course sheet, the term list and the registration list are all
        behind the login: the client cannot even draw its first screen before
        authenticating.
        """
        for path in ("/api/terms", "/api/courses?year=2026&term=1",
                     "/api/registrations", "/api/me"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["code"], server.ERROR_AUTH_REQUIRED)

    def test_login_flow(self):
        status, body = self.call("POST", "/api/login",
                                 {"student_id": "S9001", "password": "secret123"})
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "student")
        self.assertEqual(body["student"]["student_id"], "S9001")
        self.assertTrue(body["token"])

        status, body = self.call("POST", "/api/login",
                                 {"student_id": "S9001", "password": "nope"})
        self.assertEqual((status, body["code"]), (401, server.ERROR_BAD_LOGIN))

    def test_me_and_logout(self):
        status, body = self.call("GET", "/api/me", token=self.student_token)
        self.assertEqual((status, body["student"]["student_id"]), (200, "S9001"))

        status, body = self.call("POST", "/api/logout", token=self.student_token)
        self.assertTrue(body["logged_out"])
        # The token is dead immediately.
        status, body = self.call("GET", "/api/me", token=self.student_token)
        self.assertEqual((status, body["code"]), (401, server.ERROR_AUTH_REQUIRED))
        self.student_token = self.login("S9001", "secret123")   # refresh for cleanup

    # -- terms and courses -------------------------------------------------
    def test_terms_and_term_filtering(self):
        status, body = self.call("GET", "/api/terms", token=self.student_token)
        self.assertEqual(status, 200)
        self.assertEqual([(t["year"], t["term"]) for t in body["terms"]],
                         [(2026, 1), (2026, 2)])

        _, term1 = self.call("GET", "/api/courses?year=2026&term=1",
                             token=self.student_token)
        _, term2 = self.call("GET", "/api/courses?year=2026&term=2",
                             token=self.student_token)
        self.assertTrue(all(c["term"] == 1 for c in term1["courses"]))
        self.assertEqual([c["code"] for c in term2["courses"]], ["CS102"])
        self.assertEqual(term1["count"], 6)

    # -- registrations -----------------------------------------------------
    def test_register_uses_the_session_identity(self):
        """No student id is sent; the server takes it from the token.

        This is the security property the whole login system exists for: the
        body cannot say who is registering, so it cannot lie about it either.
        """
        status, body = self.call("POST", "/api/registrations",
                                 {"section_id": "2026-T1-EN150-A"},
                                 token=self.student_token)
        self.assertEqual(status, 201)
        reg = body["registration"]
        self.assertEqual(reg["student_id"], "S9001")
        self.assertEqual(reg["student_name"], "Test Student")

    def test_registration_cannot_be_forced_onto_another_student(self):
        status, body = self.call("POST", "/api/registrations",
                                 {"section_id": "2026-T1-EN150-A",
                                  "student_id": "S9002"},
                                 token=self.student_token)
        self.assertEqual((status, body["code"]), (403, server.ERROR_FORBIDDEN))

    def test_registrations_are_scoped_to_the_student(self):
        """A student asking for someone else's list still gets their own.

        Silently ignoring the query parameter is friendlier than answering 403
        here: the client never sends it, so a mismatch means a hand-made
        request, and returning the caller's own data leaks nothing.
        """
        self.call("POST", "/api/registrations", {"section_id": "2026-T1-EN150-A"},
                  token=self.student_token)
        _, body = self.call("GET", "/api/registrations?student_id=S9002",
                            token=self.student_token)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["registrations"][0]["student_id"], "S9001")

    def test_cancel_requires_ownership(self):
        _, body = self.call("POST", "/api/registrations",
                            {"section_id": "2026-T1-EN150-A"},
                            token=self.student_token)
        reg_id = body["registration"]["id"]

        other = self.login("S9002", "blocked123")      # a different student
        status, body = self.call("DELETE", f"/api/registrations/{reg_id}", token=other)
        self.assertEqual((status, body["code"]), (403, server.ERROR_FORBIDDEN))

        status, _ = self.call("DELETE", f"/api/registrations/{reg_id}",
                              token=self.student_token)
        self.assertEqual(status, 200)

    def test_blocked_student_cannot_register_over_http(self):
        token = self.login("S9002", "blocked123")
        status, body = self.call("POST", "/api/registrations",
                                 {"section_id": "2026-T1-EN150-A"}, token=token)
        self.assertEqual((status, body["code"]),
                         (403, server.ERROR_REGISTRATION_BLOCKED))

    # -- students and the admin API ---------------------------------------
    def test_student_dictionary_is_admin_only(self):
        status, body = self.call("GET", "/api/students", token=self.student_token)
        self.assertEqual((status, body["code"]), (403, server.ERROR_FORBIDDEN))
        status, body = self.call("GET", "/api/students", token=self.admin_token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["count"], 3)

    def test_student_can_read_only_their_own_record(self):
        status, _ = self.call("GET", "/api/students/S9001", token=self.student_token)
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/api/students/S9002", token=self.student_token)
        self.assertEqual((status, body["code"]), (403, server.ERROR_FORBIDDEN))
        status, body = self.call("GET", "/api/students/S0000", token=self.admin_token)
        self.assertEqual((status, body["code"]), (404, server.ERROR_NO_SUCH_STUDENT))

    def test_admin_can_list_accounts(self):
        status, body = self.call("GET", "/api/admin/students", token=self.admin_token)
        self.assertEqual(status, 200)
        entry = next(s for s in body["students"] if s["student_id"] == "S9002")
        self.assertFalse(entry["can_register"])
        self.assertIn("registrations", entry)

    def test_admin_can_add_a_student(self):
        status, body = self.call("POST", "/api/admin/students", {
            "student_id": "S9501", "name": "Https Student",
            "email": "https@school.edu", "password": "httpspass"}, token=self.admin_token)
        self.assertEqual(status, 201)
        self.assertEqual(body["student"]["student_id"], "S9501")
        # The new account can log in through the API straight away.
        token = self.login("S9501", "httpspass")
        status, body = self.call("GET", "/api/me", token=token)
        self.assertEqual(body["student"]["name"], "Https Student")

    def test_admin_can_toggle_registration_access(self):
        """Requirement 3, end to end: block, try, unblock, succeed."""
        status, body = self.call("POST", "/api/admin/students/S9001/access",
                                 {"can_register": False}, token=self.admin_token)
        self.assertEqual(status, 200)
        self.assertFalse(body["student"]["can_register"])

        status, body = self.call("POST", "/api/registrations",
                                 {"section_id": "2026-T1-EN150-A"},
                                 token=self.student_token)
        self.assertEqual((status, body["code"]),
                         (403, server.ERROR_REGISTRATION_BLOCKED))

        self.call("POST", "/api/admin/students/S9001/access",
                  {"can_register": True}, token=self.admin_token)
        status, _ = self.call("POST", "/api/registrations",
                              {"section_id": "2026-T1-EN150-A"},
                              token=self.student_token)
        self.assertEqual(status, 201)

    def test_admin_can_reset_a_password(self):
        status, _ = self.call("POST", "/api/admin/students/S9001/password",
                              {"password": "changed-1"}, token=self.admin_token)
        self.assertEqual(status, 200)
        self.login("S9001", "changed-1")          # new password works
        status, _ = self.call("POST", "/api/login",
                              {"student_id": "S9001", "password": "secret123"})
        self.assertEqual(status, 401)             # old one does not

    def test_student_cannot_use_the_admin_api(self):
        """Hiding the admin button in the client is cosmetic; this is the check.

        All three admin endpoints are tried with a student token and must answer
        403 FORBIDDEN - the server, not the GUI, is what enforces the role.
        """
        for method, path, payload in (
                ("GET", "/api/admin/students", None),
                ("POST", "/api/admin/students", {"student_id": "S9600", "name": "No",
                                                 "email": "no@x.edu",
                                                 "password": "nope123"}),
                ("POST", "/api/admin/students/S9002/access", {"can_register": True})):
            status, body = self.call(method, path, payload, token=self.student_token)
            self.assertEqual((status, body["code"]), (403, server.ERROR_FORBIDDEN), path)

    # -- misc --------------------------------------------------------------
    def test_error_shape(self):
        """Errors must always be JSON with a code - the client depends on it."""
        status, body = self.call("POST", "/api/registrations", {},
                                 token=self.student_token)
        self.assertEqual((status, body["code"]), (400, server.ERROR_VALIDATION))
        status, body = self.call("GET", "/api/nothing", token=self.student_token)
        self.assertEqual((status, body["code"]), (404, server.ERROR_NO_ROUTE))

    def test_html_course_sheet(self):
        """The browser view renders with a real course in it."""
        with urllib.request.urlopen(self.base + "/") as response:
            html = response.read().decode()
        self.assertIn("Course Sheet", html)
        self.assertIn("CS101", html)


# ---------------------------------------------------------------------------
# 6. The real project files
# ---------------------------------------------------------------------------
class TestRealSheet(unittest.TestCase):
    """Sanity checks against the project's own courses.csv and students.csv.

    The fixture-based groups above would all pass even if the shipped files
    were broken, so the real files are checked here: every term present, no
    repeated section id, no repeated student id, and a password for everyone.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = server.CourseDB(os.path.join(self.tmp, "real.db"),
                                  server.DEFAULT_COURSES, server.DEFAULT_STUDENTS)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sheet_has_several_terms(self):
        """The term selector needs something to select: three terms expected."""
        self.assertGreaterEqual(len(self.db.terms()), 3)

    def test_which_courses_are_offered_more_than_once(self):
        """Repeated codes are expected - they are the different sessions."""
        repeated = {code: len(sections)
                    for code, sections in self.db.course_index[(2026, 1)].items()
                    if len(sections) > 1}
        self.assertIn("CS101", repeated)
        self.assertGreaterEqual(repeated["CS101"], 3)

    def test_times_are_well_formed(self):
        """Every row has a sane HH:MM window and a positive capacity."""
        for course in self.db.courses():
            self.assertRegex(course["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(course["end"], r"^\d{2}:\d{2}$")
            self.assertLess(server.minutes(course["start"]),
                            server.minutes(course["end"]))
            self.assertGreater(course["capacity"], 0)

    def test_the_sheet_contains_overlaps(self):
        """Overlapping classes exist, otherwise the clash rule is untested."""
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

    def test_real_accounts_are_loaded_and_hashed(self):
        """The shipped students.csv gives every student a hashed password."""
        self.assertGreaterEqual(len(self.db.students), 15)
        self.assertEqual(self.db.student_report["skipped"], 0)
        for student_id, stored in self.db.student_secrets.items():
            self.assertTrue(stored.startswith("pbkdf2_sha256$"), student_id)

    def test_every_shipped_account_can_log_in(self):
        """Whatever password the CSV hands out must actually work."""
        with open(server.DEFAULT_STUDENTS, newline="", encoding="utf-8") as handle:
            import csv
            for row in csv.DictReader(handle):
                student = self.db.authenticate(row["student_id"], row["password"])
                self.assertEqual(student["student_id"], row["student_id"])

    def test_at_least_one_blocked_and_one_admin_account(self):
        """The shipped data demonstrates both access states out of the box."""
        self.assertTrue(any(not s["can_register"] for s in self.db.students.values()))
        self.assertTrue(any(s["role"] == "admin" for s in self.db.students.values())
                        or server.ADMIN_ACCOUNT is not None)


class TestMigration(unittest.TestCase):
    """A version 2 database must be upgraded in place, not thrown away.

    The v2 schema had no passwords, so a migration has to invent one: existing
    students keep their registrations and get `can_register = 1` and the plain
    `student` role, which is the least surprising outcome for a live system.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "old.db")
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE courses (section_id TEXT PRIMARY KEY, year INTEGER, term INTEGER,
                code TEXT, section TEXT, title TEXT, instructor TEXT, department TEXT,
                day TEXT, start TEXT, end TEXT, venue TEXT, capacity INTEGER,
                credits INTEGER, description TEXT);
            CREATE TABLE students (student_id TEXT PRIMARY KEY, name TEXT, email TEXT,
                created_at TEXT);
            CREATE TABLE registrations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id TEXT NOT NULL REFERENCES courses(section_id),
                student_id TEXT NOT NULL REFERENCES students(student_id),
                notes TEXT DEFAULT '', created_at TEXT,
                UNIQUE (section_id, student_id));
            INSERT INTO courses VALUES ('2026-T1-CS101-A', 2026, 1, 'CS101', 'A', 'Intro',
                'Dr. A', 'CS', 'Mon', '09:00', '10:30', 'Room 1', 40, 3, '');
            INSERT INTO students VALUES ('S1001', 'Amy Wong',
                'amy.wong@student.edu', '2026-01-01 00:00:00');
            INSERT INTO registrations (section_id, student_id, notes, created_at)
                VALUES ('2026-T1-CS101-A', 'S1001', 'legacy', '2026-01-02 00:00:00');
            PRAGMA user_version = 2;
        """)
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upgrade_keeps_data_and_adds_columns(self):
        db = server.CourseDB(self.db_path, server.DEFAULT_COURSES,
                             server.DEFAULT_STUDENTS)
        # The legacy registration survived the upgrade.
        self.assertEqual(len(db.registrations(student_id="S1001")), 1)
        # The schema version moved forward.
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
        # And the CSV password was applied to the pre-existing account.
        self.assertEqual(db.authenticate("S1001", "amy2026")["name"], "Amy Wong")


if __name__ == "__main__":
    unittest.main(verbosity=2)
