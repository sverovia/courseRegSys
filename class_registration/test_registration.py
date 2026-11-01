#!/usr/bin/env python3
"""
test_registration.py - self-checks for the registration system.

Covers the database rules (validation, capacity, duplicates, timetable clashes)
and a live round-trip through the HTTP API.

    python3 test_registration.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import server

TEST_COURSES = """code,title,instructor,department,day,start,end,venue,capacity,credits,description
T101,Alpha Course,Dr. A,Testing,Mon,09:00,10:30,Room A,2,3,first
T102,Overlapping Course,Dr. B,Testing,Mon,10:00,11:30,Room B,5,3,clashes with T101
T103,Other Day Course,Dr. C,Testing,Tue,09:00,10:00,Room C,3,3,no clash
"""


class Base(unittest.TestCase):
    """Builds a throw-away database on a three-course sheet."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmp, "courses.csv")
        with open(self.csv_path, "w", encoding="utf-8") as fh:
            fh.write(TEST_COURSES)
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = server.CourseDB(self.db_path, self.csv_path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reg(self, code, sid="S9001", name="Test Student", email="t@school.edu"):
        return self.db.register({"course_code": code, "student_id": sid,
                                 "student_name": name, "email": email})


class DatabaseRules(Base):
    def test_sheet_loaded_with_seats(self):
        courses = {c["code"]: c for c in self.db.courses()}
        self.assertEqual(len(courses), 3)
        for c in courses.values():
            self.assertTrue(c["venue"])
            self.assertGreater(c["capacity"], 0)
            self.assertEqual(c["seats_left"], c["capacity"] - c["enrolled"])

    def test_validation(self):
        cases = [
            ({"course_code": "T101", "student_id": "x", "student_name": "A B",
              "email": "a@b.edu"}, "VALIDATION"),                      # short id
            ({"course_code": "T101", "student_id": "S1", "student_name": "A",
              "email": "a@b.edu"}, "VALIDATION"),                      # name too short
            ({"course_code": "T101", "student_id": "S1", "student_name": "A B",
              "email": "nope"}, "VALIDATION"),                         # bad e-mail
            ({"course_code": "ZZ999", "student_id": "S123", "student_name": "A B",
              "email": "a@b.edu"}, "NO_SUCH_COURSE"),
        ]
        for payload, code in cases:
            with self.assertRaises(server.ApiError) as ctx:
                self.db.register(payload)
            self.assertEqual(ctx.exception.code, code, payload)

    def test_duplicate_then_cancel(self):
        first = self.reg("T101")
        with self.assertRaises(server.ApiError) as ctx:
            self.reg("T101")
        self.assertEqual(ctx.exception.code, "DUPLICATE")
        self.db.cancel(first["id"])
        self.assertEqual(self.db.registrations(student_id="S9001"), [])

    def test_timetable_clash(self):
        self.reg("T101")                       # Mon 09:00-10:30
        self.reg("T103")                       # Tue 09:00-10:00 -> fine
        with self.assertRaises(server.ApiError) as ctx:
            self.reg("T102")                   # Mon 10:00-11:30 -> overlaps T101
        self.assertEqual(ctx.exception.code, "TIME_CONFLICT")

    def test_capacity_is_enforced(self):
        self.reg("T101", sid="CAP1")
        self.reg("T101", sid="CAP2")
        self.assertTrue(self.db.course("T101")["is_full"])
        with self.assertRaises(server.ApiError) as ctx:
            self.reg("T101", sid="CAP3")
        self.assertEqual(ctx.exception.code, "FULL")
        self.assertEqual(self.db.course("T101")["seats_left"], 0)

    def test_stats(self):
        self.reg("T101")
        self.reg("T103")
        stats = self.db.stats()
        self.assertEqual(stats["total_registrations"], 2)
        self.assertEqual(stats["unique_students"], 1)
        self.assertEqual(stats["courses"], 3)
        self.assertEqual(stats["seats_total"], 10)

    def test_real_course_sheet_parses(self):
        real = server.CourseDB(os.path.join(self.tmp, "real.db"), server.DEFAULT_COURSES)
        courses = real.courses()
        self.assertGreaterEqual(len(courses), 10)
        for c in courses:
            self.assertRegex(c["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(c["end"], r"^\d{2}:\d{2}$")
            self.assertGreater(c["capacity"], 0)


class HttpApi(Base):
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
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_health_and_courses(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body = self.call("GET", "/api/courses")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["courses"]), 3)

    def test_register_list_delete(self):
        status, body = self.call("POST", "/api/registrations", {
            "course_code": "T103", "student_id": "S7001",
            "student_name": "Http Tester", "email": "http@school.edu"})
        self.assertEqual(status, 201)
        reg_id = body["registration"]["id"]

        status, body = self.call("GET", "/api/registrations?student_id=S7001")
        self.assertEqual(body["count"], 1)

        status, body = self.call("DELETE", f"/api/registrations/{reg_id}")
        self.assertEqual((status, body["deleted"]), (200, reg_id))

        status, body = self.call("GET", "/api/registrations?student_id=S7001")
        self.assertEqual(body["count"], 0)

    def test_error_shape(self):
        status, body = self.call("POST", "/api/registrations", {"course_code": "T101"})
        self.assertEqual((status, body["code"]), (400, "VALIDATION"))
        status, body = self.call("GET", "/api/does-not-exist")
        self.assertEqual((status, body["code"]), (404, "NO_ROUTE"))

    def test_html_course_sheet(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        self.assertIn("Course Sheet", html)
        self.assertIn("T101", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
