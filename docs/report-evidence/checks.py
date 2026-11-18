"""Replay the supplemental checks for the group project report.

Run with `python3 docs/report-evidence/checks.py`. All data changes use temporary
SQLite files. The client probes run logic only; they do not launch Tk. A nonzero
exit is expected on the reported baseline while either documented issue remains.
"""

import ast
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace

CLASS_REGISTRATION = Path(__file__).resolve().parents[2] / "class_registration"
sys.path.insert(0, str(CLASS_REGISTRATION))

import server
from test_registration import TEST_SHEET, TEST_STUDENTS


@contextmanager
def fixture(extra_rows=""):
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        courses, students = root / "courses.csv", root / "students.csv"
        courses.write_text(TEST_SHEET + extra_rows, encoding="utf-8")
        students.write_text(TEST_STUDENTS, encoding="utf-8")
        yield server.CourseDB(str(root / "test.db"), str(courses), str(students)), courses


def book(db, section_id, student_id="S9001"):
    return db.register({"section_id": section_id, "student_id": student_id,
                        "student_name": "Test Student", "email": "test@school.edu"})


def isolated_client_definitions():
    # Execute the unchanged definitions without importing Tk or opening a window.
    keep_classes = {"SearchIndex", "Timetable"}
    keep_constants = {"WEEKDAYS", "ALL_DAYS", "SLOT"}
    keep_functions = {"hhmm_to_minutes", "minutes_to_hhmm"}
    tree = ast.parse((CLASS_REGISTRATION / "client_gui.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if
                 (isinstance(node, ast.ClassDef) and node.name in keep_classes)
                 or (isinstance(node, ast.FunctionDef) and node.name in keep_functions)
                 or (isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id in keep_constants
                     for target in node.targets))]
    namespace = {"tk": SimpleNamespace(Frame=object, Misc=object), "time": time}
    exec(compile(tree, "client_gui.py", "exec"), namespace)
    return namespace


def main():
    findings = []
    with TemporaryDirectory() as tmp:
        db = server.CourseDB(str(Path(tmp) / "shipped.db"))
        layout = [(t["year"], t["term"], t["sections"], t["courses"]) for t in db.terms()]
        assert layout == [(2026, 1, 20, 13), (2026, 2, 13, 11), (2027, 1, 10, 9)], layout
        assert len(db.students) == 16
        overlaps = [(a["label"], b["label"]) for a, b in combinations(db.courses(2026, 1), 2)
                    if a["day"] == b["day"] and server.overlaps(a["start"], a["end"], b["start"], b["end"])]
        assert ("CS101-C", "MA110-A") in overlaps
        print(f"S01 pass: (year, term, sections, course codes) = {layout}; CSV accounts = {len(db.students)}; 2026 T1 overlap pairs = {len(overlaps)}")
        for section_id in ("2026-T1-CS101-A", "2027-T1-CS101-A"):
            book(db, section_id, "S1001")
        registered = [r["section_id"] for r in db.registrations(student_id="S1001")]
        assert registered == ["2026-T1-CS101-A", "2027-T1-CS101-A"], registered
        print(f"S02 pass: same code and time across academic years, registrations = {registered}")

    with fixture("2026,1,ZZ900,A,Touching,Dr. E,Test,Mon,10:30,11:30,R,5,3,\n"
                 "2026,1,ZZ901,A,Overlap,Dr. E,Test,Mon,10:29,11:29,R,5,3,\n") as (db, _):
        book(db, "2026-T1-CS101-A")
        try:
            book(db, "2026-T1-ZZ901-A")
        except server.ApiError as e:
            assert (e.status, e.code) == (409, "TIME_CONFLICT"), (e.status, e.code)
            denied = (e.status, e.code)
        else:
            raise AssertionError("one-minute overlap was accepted")
        accepted = book(db, "2026-T1-ZZ900-A")["section_id"]
        assert len(db.registrations(student_id="S9001")) == 2
        print(f"S03 pass: 10:29 start against 10:30 end = {denied}; 10:30 start = {accepted} accepted")

    with fixture() as (db, _):
        section = "2026-T1-CS101-A"
        book(db, section)  # 1 of 2 seats is taken
        for trial in range(1, 6):
            gate = Barrier(4)

            def try_for_last_seat(student_id):
                gate.wait(timeout=5)
                try:
                    return "accepted", book(db, section, student_id)["id"]
                except server.ApiError as e:
                    return e.code, None

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(try_for_last_seat, ("S9801", "S9802", "S9803", "S9804")))
            codes = sorted(code for code, _ in results)
            assert codes == ["FULL", "FULL", "FULL", "accepted"], codes
            assert db.resolve_section(None, None, section)["enrolled"] == 2
            print(f"S04 trial {trial}/5 pass: 1 accepted, 3 FULL, enrolled = 2/2")
            db.cancel(next(booking_id for code, booking_id in results if code == "accepted"))

    client = isolated_client_definitions()
    with fixture() as (db, _):
        index = client["SearchIndex"]()
        index.build(db.courses(2026, 1))
        for query, expected in {
            "": ["CS101-A", "CS101-B", "CS101-C", "EN150-A", "MA110-A", "MA110-B"],
            "CS101": ["CS101-A", "CS101-B", "CS101-C"],
            "mon calc": ["MA110-A"],
            "prog": ["CS101-A", "CS101-B", "CS101-C"],
            "CS101B": ["CS101-B"],
            "zzno": [],
        }.items():
            labels = [c["label"] for c in index.search(query)[0]]
            assert labels == expected, (query, labels)
            print(f"S05 pass: term 1 query {query!r} -> {labels}")
        index.build(db.courses(2026, 2))
        assert not index.search("CS101")[0]
        labels = [c["label"] for c in index.search("")[0]]
        assert labels == ["CS102-A"], labels
        print(f"S05 pass: after rebuild, term 2 empty query -> {labels}; 'CS101' -> []")

    with fixture() as (db, _):
        index = client["SearchIndex"]()
        index.build(db.courses(2026, 1))
        labels = [c["label"] for c in index.search("CS101-B")[0]]
        if labels == ["CS101-B"]:
            print("S06 pass: displayed label 'CS101-B' returned just that section")
        else:
            findings.append("S06")
            print(f"S06 pending (unexpected): displayed label 'CS101-B' -> {labels}; expected ['CS101-B']")

    widget = object.__new__(client["Timetable"])
    widget.rows = []
    assert widget._days() == ("Mon", "Tue", "Wed", "Thu", "Fri")
    assert widget._window() == (540, 900)
    print(f"S07 pass: empty timetable -> columns {widget._days()}, time range {widget._window()} minutes")
    widget.rows = [{"day": "Sat", "start": "08:20", "end": "09:35"},
                   {"day": "Sun", "start": "16:20", "end": "17:15"}]
    assert widget._days()[-2:] == ("Sat", "Sun")
    assert widget._window() == (480, 1080)
    print(f"S07 pass: weekend rows -> columns {widget._days()}, time range {widget._window()} minutes")

    with fixture() as (db, courses):
        book(db, "2026-T1-CS101-A")
        courses.write_text("".join(line for line in TEST_SHEET.splitlines(keepends=True)
                                   if not line.startswith("2026,1,CS101,A,")), encoding="utf-8")
        db.reload_courses()
        retained = [r["section_id"] for r in db.registrations(student_id="S9001")]
        assert retained == ["2026-T1-CS101-A"], retained
        labels = [c["label"] for c in db.courses(2026, 1) if c["code"] == "CS101"]
        siblings = db.sections_of(2026, 1, "CS101")
        print(f"S08 after reload: SQL labels = {labels}; course_index section IDs = {siblings}")
        try:
            book(db, "2026-T1-CS101-B")
        except server.ApiError as e:
            assert e.code == "ONE_SESSION", e.code
            print("S08 pass: existing booking in retained section still counts")
        else:
            ids = [r["section_id"] for r in db.registrations(student_id="S9001")]
            print(f"S08 pending (unexpected): expected ONE_SESSION; section B accepted; stored registrations = {ids}")
            findings.append("S08")

    print(f"Supplemental summary: {8 - len(findings)} pass, {len(findings)} pending "
          f"({', '.join(findings) or 'none'}).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
