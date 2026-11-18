# Group Project Report: Class Registration System

**Revision evaluated:** `e847861` · **Test date:** 18 November 2026 (UTC)

[Introduction](#1-introduction) · [Algorithms](#2-algorithm-specifications) · [Testing](#3-testing-results) · [Analysis](#4-analysis-and-comments)

## 1. Introduction

### Problem description

The Class Registration System lets students browse a term’s course catalogue, enrol in a section, view their weekly timetable, and drop a class. The course and its sections are different things: a course code identifies the subject, while a section is one offering of that subject at a specific time and venue. The system must enforce a seat limit for each section, allow at most one section of a course per student per term, and prevent a student from booking classes that overlap. Staff can create accounts, control who may register, and review enrolment by term.

These rules belong on the server. A student’s screen may still show an available seat that someone else has just taken. The server must recheck availability when it saves the request, and an unsuccessful request must not create a registration. Students whose registration access is blocked can still sign in, view the catalogue and their own timetable, and drop an existing class.

### Background and example

A university might offer the same introductory course on several days so students can fit it around their other classes. In the shipped catalogue, **CS101-C** and **MA110-A** both meet on Monday from 11:00 to 12:30 in 2026 Term 1. A student who needs both subjects can instead choose the Wednesday section **CS101-B**. They cannot keep both CS101 sections in that term. At the same time, the course office needs enrolment counts that reflect both new bookings and cancellations.

The project is implemented in Python with Tkinter desktop interfaces, a JSON HTTP API, and SQLite. The shipped catalogue contains **43 sections** across three terms; `students.csv` contains **16 demo accounts**, separate from the built-in test administrator. The following counts were verified from a fresh database (case S01):

| Academic term | Sections | Distinct course codes in that term |
|---|---:|---:|
| 2026 Term 1 | 20 | 13 |
| 2026 Term 2 | 13 | 11 |
| 2027 Term 1 | 10 | 9 |

This report covers the runnable system in [`class_registration/`](class_registration/README.md). The separate [`main/app.py`](main/app.py) Flask prototype displays a form confirmation but does not store registrations or enforce these rules. Prerequisites and waitlists are not implemented in the evaluated system.

## 2. Algorithm specifications

### 2.1 Architecture and data structures

```text
Student/admin desktop client ──► JSON API ──┐
Local staff console ─────────────────────────┼──► CourseDB ──┬─► SQLite
Course and account CSVs ────────► Import ────┘                └─► Server dictionaries
```

[`client_gui.py`](class_registration/client_gui.py) uses the API and never writes to SQLite directly. [`server_gui.py`](class_registration/server_gui.py) runs the API in a background thread and uses the same `CourseDB` instance as the API. [`server.py`](class_registration/server.py) owns validation, business rules, and storage. The threaded API can handle concurrent requests; registration writes are serialized in the database layer.

| Structure | Key fields or mapping | Purpose |
|---|---|---|
| Course and account CSVs | Course row: year, term, code, section, day, start/end, venue, capacity, and descriptive fields. Account row: ID, name, email, demo password, role, and access flag. | Seed the catalogue and accounts. |
| SQLite `courses` | Primary key `section_id`, built as `year-Tterm-CODE-SECTION`. | Distinguishes individual sessions, e.g. `2026-T1-CS101-B`. |
| SQLite `students` | Primary key `student_id`; profile, role, `can_register`, and `password_hash`. | Stores each student once. The server stores a salted hash rather than the CSV’s readable password. |
| SQLite `registrations` | Numeric primary key `id`; foreign keys to student and section; `UNIQUE(section_id, student_id)`; indexes on section and student. | Links students to sections and supports seat counts, duplicate checks, and timetable reads. |
| Server dictionaries | `course_index[(year, term)][code] → list of section IDs`; `students[id] → public profile`; `student_secrets[id] → hash`; `sessions[token] → identity, role, and expiry`. | Average constant-time key lookup. Traversing a section list or cleaning up sessions still takes time proportional to the number of entries examined. |
| Client search dictionaries | `index[word] → set of section IDs`; `prefix[first 1 or 2 characters] → set of section IDs`; `tokens[section_id] → course record`. | Support local search on the selected term without sending a request per keystroke. |

### 2.2 Start-up, CSV import, and catalogue queries

1. **Prepare storage.** Create the version-3 schema if needed. For a version-2 database, add any missing `password_hash`, `can_register`, and `role` columns to `students` and update the schema version in place. Existing registrations remain linked to their students and sections. An incompatible database file is moved aside before a new one is created.
2. **Load sections.** Read `courses.csv`, skip rows without a course code, trim fields, and normalize course codes and section letters to uppercase. Build the section ID. A `seen` set rejects repeated IDs (the first row wins), while a nested dictionary groups accepted IDs by term and course code. In SQLite, insert new sections or update matching records. If at least one row was accepted, remove sections omitted from the CSV only if they have no registrations. Then publish the new dictionary. The import normalizes fields but does not fully validate schedules or capacities.
3. **Load accounts.** Read SQLite profiles and password hashes into separate dictionaries. For `students.csv`, normalize student IDs, skip missing and duplicate IDs, and insert or update each account. Hash a nonblank CSV password before writing it; a blank password cell does not overwrite an existing hash. Accounts absent from the CSV are not deleted. Rebuild both student dictionaries after synchronization.
4. **Serve a term catalogue.** Group sections in SQLite by `(year, term)` to populate the term selector. To serve a catalogue, apply the chosen term filters, left-join registrations, and count them per section. `seats_left = max(capacity − enrolled, 0)`. Sort by year, term, course code, section, weekday, and start time. The desktop client rebuilds its local search index when it receives a refreshed catalogue.

To resolve a selection, use an explicit section ID as a primary-key lookup. If the request gives only a course code, both year and term are required. Look up the code in `course_index`: zero matches means `NO_SUCH_COURSE`, more than one means `AMBIGUOUS_SECTION`, and exactly one resolves to its section ID.

### 2.3 Authentication, sessions, and staff actions

```text
LOGIN(student_id, password):
    Normalize the student ID
    If this is the enabled test administrator:
        Check the supplied password against the configured test password
    Otherwise:
        Look up the student and stored salted password hash
        If either is missing: return 401 BAD_LOGIN
        Recompute PBKDF2-HMAC-SHA256 with the stored salt and iteration count
        Check the recomputed digest against the stored digest
    If the check failed: return 401 BAD_LOGIN, without issuing a token
    For a verified ordinary account, rehash if the stored cost is outdated
    Remove expired entries from the sessions dictionary
    Generate a random session token with an expiry 8 hours from issue
    Store the session and return the token, role, and public profile
```

New hashes use **120,000 iterations** and a random salt. Verification uses `hmac.compare_digest`. The client opens the registration window only after login succeeds and sends the token in `X-Auth-Token` on later requests. The handler checks that the session exists and has not expired; staff API routes also check the role. Sign-out removes the server’s token when the request reaches it and always forgets the token on the client. Sessions are in memory, so a server restart ends them. The health check and read-only HTML course sheet do not require a session.

Staff account creation validates the ID, name, email, minimum password length, and role; it rejects an existing or reserved ID before inserting the new account. Access changes update the registration flag without deleting bookings. Password reset replaces the stored hash. A password reset does not currently revoke existing sessions.

### 2.4 Client-side course search

The index lowercases and tokenizes course code, section, title, instructor, venue, department, weekday, and display label. It stores each distinct word per section, one- and two-character prefix buckets, and a compact code-plus-section token such as `cs101b`.

```text
SEARCH(query):
    Lowercase and trim query
    If blank: return all indexed sections in display order
    Replace hyphens with spaces and split into query words
    For each word:
        Use its exact word bucket if present
        Otherwise, find the bucket for the word's first two characters
            (one character if the word is one character long)
            and keep candidates whose original searchable fields contain the word
        Intersect the matches with those for previous words
        Stop early if the intersection is empty
    Return matching records sorted by course code, section, and day
```

For example, `mon calc` can find a Monday Calculus section because both words match the same record, even though they occur in different fields. This is an AND search, not a phrase search. The fallback is limited by the prefix buckets; it is not a full substring scan. **Hide full** is applied afterward. The API has a separate course-search endpoint that scans the term’s catalogue for a substring; the desktop search does not use it.

### 2.5 Registration and cancellation

```text
REGISTER(request, authenticated session):
    Use the signed-in student's identity; only staff may name another student
    Validate the input; check can_register; resolve the target section

    Acquire the write lock and BEGIN IMMEDIATE in SQLite
        If the section's enrolment count is at or above capacity: reject FULL
        If this student already holds this section: reject DUPLICATE
        If this student holds another indexed section of the same course
          in this year and term: reject ONE_SESSION
        For each of the student's registrations in this year and term:
            If the days match and the time intervals overlap: reject TIME_CONFLICT
        Reuse the student record, or create one for a valid new ID on this path
        Insert the registration and commit
    Return HTTP 201 with the saved registration
```

The validation step requires a section or course choice, a valid student ID, and a name and email when the server-side registration path is given a new ID. Business-rule rejections inside the transaction roll back the write. The lock and transaction keep the seat count and insert together, so requests cannot each claim the last seat against the same unchanged capacity. Since the full check runs first, an already-booked request for a full section may receive `FULL` before `DUPLICATE`.

The overlap check uses minutes after midnight. For classes on the same day, half-open intervals `[start, end)` conflict exactly when **`start₁ < end₂` and `start₂ < end₁`**. A class starting at 10:30 can therefore follow one ending at 10:30, while a 10:29 start would overlap it. Classes in different terms never compete for the same student timetable slot.

To **drop** a section, look up the registration by ID. Reject a missing ID or a student who is not the owner; staff can cancel on a student’s behalf. Delete the row and commit. Because enrolment is counted from registrations rather than stored in a separate counter, the next read shows the freed seat.

### 2.6 Timetable, reporting, and export

The client fetches the signed-in student’s registrations for the selected term. The timetable draws weekday columns and adds weekend columns only when there are weekend bookings. Its default time range is 09:00–15:00; it expands to cover the earliest start and latest end, rounding outward to whole hours. The canvas uses 30-minute rows. For a class time `t` in minutes, `y = header + ((t − window_start) / 30) × row_height` gives each block’s top or bottom. The weekday determines the column; the sum of the course code’s character values modulo the palette size selects a stable colour. Canvas tags link a clicked block to its registration ID. The client refreshes the catalogue and timetable after a successful booking or drop. Background HTTP workers enqueue results for the Tk event thread to apply to widgets.

For staff, SQLite joins registration, section, and student records, applies term and text filters, and sorts results for display or CSV export. The overview counts registrations and distinct students and sums section capacities and occupancies; the account list groups registrations by student. The browser’s read-only course sheet uses the same catalogue data and cannot submit a booking.

## 3. Testing results

### 3.1 Method and outcome

Testing was performed on Linux x86-64 with Python 3.11.2 and SQLite 3.40.1. The suite used temporary databases; HTTP integration cases used a real loopback socket. The shipped CSV checks read the actual files but did not write to the tracked databases. The [test log](docs/report-evidence/test-log.md) records the exact runs, timestamps, and output. Commands from the repository root:

```sh
(cd class_registration && python3 test_registration.py)
python3 docs/report-evidence/checks.py
(cd class_registration && python3 comment_ratio.py)
```

| Automated suite group | Passed | Coverage |
|---|---:|---|
| `TestSheetLoading` | 4/4 | CSV import, duplicate section IDs, seat calculations |
| `TestDictionaries` | 7/7 | Term-specific course groups, student record reuse, public profiles |
| `TestRules` | 14/14 | Validation, capacity, duplicates, conflicts, cancellation, and term statistics |
| `TestAccounts` | 23/23 | Passwords, account management, access control, and sessions |
| `TestHttpApi` | 19/19 | Real HTTP requests, role checks, registration, and read-only HTML |
| `TestRealSheet` | 7/7 | Shipped catalogue and accounts |
| `TestMigration` | 1/1 | Version-2 to version-3 migration |
| **Total** | **75/75** | **30.013 seconds; exit 0 (R1)** |

The supplemental run had **six passes and two open findings** (R2). It exercised client logic without opening a GUI. The comment-ratio tool measured **31.6%** across its five source files, above the 30% project target (R3). This environment did not have Tkinter, so the desktop interaction cases remain untested (R4).

**Pass** means the observed result matched the expectation; **Pending** means the check exposed an open finding or was not run, as labelled. The log marks the initial catalogue-count expectation as **Corrected** because the README was out of date.

### 3.2 Test-case table

The `T` rows below are representative cases from the 75-test suite; the linked R1 log lists every automated case. The `S` rows account for all eight final supplemental cases.

| Case / purpose | Expected result | Actual behaviour | Test log / status |
|---|---|---|---|
| T01 · Import the fixture with a repeated CS101-A row | Load 7 of 8 rows; retain the first copy. | 7 loaded, 1 skipped; title remained “Intro Programming.” | [R1](docs/report-evidence/test-log.md#r1-built-in-suite) · `TestSheetLoading` · Pass |
| T02 · Reuse a student for a second booking | Keep one profile for the ID; reuse the name and email. | The account count only grew on the first booking; the second reused the profile. | R1 · `test_student_dictionary_prevents_repetition` · Pass |
| T03 · Select catalogue term or submit a course code only | Return only the requested term; ask for a section when a code is ambiguous. | Term 2 returned only `CS102` in the fixture; a code-only multi-section request returned `AMBIGUOUS_SECTION`. | R1 · `test_terms_and_term_filtering`, `test_ambiguous_code_needs_a_section` · Pass |
| T04 · Sign in with valid and invalid credentials | Valid login issues a token; wrong password returns 401. | HTTP 200 with a token and role; HTTP 401 `BAD_LOGIN` for the wrong password. | R1 · `test_login_flow` · Pass |
| T05 · Check how passwords are represented | Use salted hashes; omit credentials from public profiles. | The credential cache used PBKDF2 strings; two hashes of the same input differed; the public dictionary contained no password fields. | R1 · `test_passwords_are_hashed_not_stored`, `test_public_dictionary_has_no_password` · Pass |
| T06 · Call protected routes without a live token | Reject missing, expired, or revoked tokens. | No-token and signed-out requests returned 401 `AUTH_REQUIRED`; a session with its expiry set in the past was discarded. | R1 · `test_protected_endpoints_need_a_token`, `test_sessions_are_issued_and_expire`, `test_me_and_logout` · Pass |
| T07 · Register with a valid session | Book for the signed-in student without resending their ID. | HTTP 201; the saved record belonged to the fixture student who signed in. | R1 · `test_register_uses_the_session_identity` · Pass |
| T08 · Fill a section with a capacity of 2 | Admit two students and refuse the third. | Two bookings succeeded; the section was marked full and the third returned `FULL`. | R1 · `test_capacity_is_enforced` · Pass |
| T09 · Re-book the exact same section with seats remaining | Reject with `DUPLICATE`. | The repeat attempt returned `DUPLICATE`. | R1 · `test_duplicate_registration` · Pass |
| T10 · Book a second section of CS101 in the same term | Reject with `ONE_SESSION`. | `CS101-B` was refused after booking `CS101-A`. | R1 · `test_only_one_session_per_course_code` · Pass |
| T11 · Book two sections with a timetable clash | Reject Monday’s overlap but allow a non-clashing day. | `CS101-C` plus `MA110-A` returned `TIME_CONFLICT`; `MA110-B` on Tuesday was allowed after `CS101-C`. | R1 · `test_timetable_clash`, `test_same_course_different_day_is_allowed` · Pass |
| T12 · Block then restore registration access | Block new bookings only; preserve existing classes and allow drops. | Blocked booking returned 403 `REGISTRATION_BLOCKED`, then 201 after access was restored. A blocked student could still drop an existing booking. | R1 · `test_admin_can_toggle_registration_access`, `test_blocking_keeps_existing_registrations` · Pass |
| T13 · Cancel another student’s booking, then cancel as owner | Refuse the other student; allow the owner and free the seat. | Non-owner received 403; owner received 200. The cancellation rule test confirmed enrolment fell to 0. | R1 · `test_cancel_requires_ownership`, `test_cancel_frees_the_seat` · Pass |
| T14 · Create an account and check staff permissions | A regular student cannot use account administration; staff can add a usable account. | Student requests returned 403; an admin got 201 and the new account signed in successfully. | R1 · `test_student_cannot_use_the_admin_api`, `test_admin_can_add_a_student` · Pass |
| T15 · Reset an account’s password | New password works; old password no longer works. | The reset returned 200; new login succeeded and old login returned 401. | R1 · `test_admin_can_reset_a_password` · Pass |
| T16 · Read statistics for two terms after booking one section in each | Count one registration per term, not two. | Both terms reported one registration; Term 1 also reported one distinct student. | R1 · `test_stats_respect_the_term` · Pass |
| T17 · Start with a version-2 database | Upgrade to version 3 and retain the legacy booking. | Schema version became 3; the booking survived and the existing student signed in using the synchronized account. | R1 · `test_upgrade_keeps_data_and_adds_columns` · Pass |
| T18 · Check the read-only sheet and the API error format | Root page renders a course; invalid and unknown API requests return structured errors. | HTML contained “Course Sheet” and `CS101`; the API returned 400 `VALIDATION` and 404 `NO_ROUTE`, respectively. | R1 · `test_html_course_sheet`, `test_error_shape` · Pass |
| S01 · Load the shipped catalogue and accounts | Match the current CSVs: 43 sections, 3 terms, and 16 accounts. | Terms contained 20, 13, and 10 sections; 16 accounts loaded. There were 8 same-day overlapping pairs in 2026 Term 1. | [R2](docs/report-evidence/test-log.md#r2-supplemental-checks) · Pass after correcting the README-based expectation |
| S02 · Take the same course code in two academic years | The one-section rule applies within a year and term, not across years. | `CS101-A` booked successfully in both 2026 Term 1 and 2027 Term 1. | R2 · Pass |
| S03 · Compare touching classes and a one-minute overlap | 10:30 start allowed after a 10:30 end; 10:29 start refused. | The touching section was booked; the one-minute overlap returned 409 `TIME_CONFLICT`. | R2 · Pass |
| S04 · Compete for the last seat | With four requests and one remaining seat, exactly one request succeeds. | In all five trials, one was accepted, three returned `FULL`, and occupancy stayed at 2/2. | R2 · Pass |
| S05 · Run exact, multi-word, prefix, and term-switch searches | Return only matching sections in the current index. | `CS101` returned A/B/C; `mon calc` returned `MA110-A`; `prog` matched the three programming sections. After rebuilding for Term 2, `CS101` returned none. | R2 · Pass |
| S06 · Search by the displayed section label `CS101-B` | Return only section B. | Both B and C returned. Splitting the label lets `b` match section C’s instructor, `Dr. B`; `CS101B` returned B alone. | R2 · **Pending: open finding** |
| S07 · Compute timetable days and time range | Empty: Mon–Fri, 09:00–15:00. Weekend bookings: add weekend columns and expand hours. | Helpers returned the default range and expanded it to 08:00–18:00 for the weekend fixture. No canvas was drawn. | R2 · Pass (logic only) |
| S08 · Remove a booked section from the CSV and reload | Keep the booking and still enforce the one-section rule for that course. | Section A stayed in SQLite but left `course_index`; booking section B then succeeded, leaving two CS101 registrations in the same term. | R2 · **Pending: open finding** |

### 3.3 Test log summary

All times are UTC on 18 November 2026. The [full test log](docs/report-evidence/test-log.md) has the commands and captured output.

| Run / time | Activity and observation | Status / follow-up |
|---|---|---|
| R1 · 08:28:04–08:28:35 | All 75 automated tests completed; exit 0. | **Pass.** |
| R2a · 08:30:09 | The first catalogue count disagreed with the README (43 actual sections, not 42). | **Corrected** the test expectation after checking the source CSV; the README was not edited. |
| R2b · 08:30:27 | An exploratory run first exposed the extra result for `CS101-B`. | **Pending.** Recorded separately as S06 so the other checks could finish. |
| R2 · 08:30:49–08:30:51 | All eight supplemental checks reached a result; six passed. | **Pending.** Retest S06 (label search) and S08 (enrolment after reload) after fixes. |
| R3 · 08:30:27 | Comment-ratio tool measured 31.6%; exit 0. | **Pass.** |
| R4 · 08:30:09 | Python reported `ModuleNotFoundError` for Tkinter. | **Pending, not run.** Use a Tk-enabled machine for M01–M03. |

### 3.4 GUI test plan and status

| Case / purpose | Expected result | Actual behaviour | Test log / status |
|---|---|---|---|
| M01 · Sign in, change terms, and sign out in the desktop client | Wrong credentials leave the login window open. A valid login opens the catalogue; term changes show the matching data; sign-out returns to login. | Not run; Tkinter unavailable. | [R4](docs/report-evidence/test-log.md#r4-gui-environment-check) · Pending |
| M02 · Register, resize the timetable, select a block, and drop it | The block is drawn at the correct time, remains selectable after resize, and disappears on drop; the available seats update. | Not run; layout helpers were checked in S07, not the canvas or widgets. | R4 · Pending |
| M03 · Add and block an account, then filter and export in the staff console | Staff can manage the account; CSV export contains only the registrations matching the selected filters. | Not run; account operations were tested through the API, not the console. | R4 · Pending |

## 4. Analysis and comments

### 4.1 Time and space complexity

Let `C` be the number of stored sections, `c` the sections in a catalogue result, `F` the course CSV rows, `U` the student CSV rows, `N` the stored students, and `R` the stored registrations. For one registration request, `e` is the occupancy of its target section, `s` the number of same-course sections, `a` the student’s total registrations, and `k` their registrations in the requested term. `A` is the number of cached sessions and `I` the PBKDF2 iteration count. The estimates assume bounded record fields and average-case hash operations. SQLite work is noted separately because its query planner and disk state affect timings.

| Algorithm | Time | Space |
|---|---|---|
| Course import | Expected `O(F)` in Python to parse, deduplicate, and group rows. SQLite adds one B-tree upsert per accepted row (roughly `O(log(C + F))` each) and a scan/probe step to remove stale sections. | `O(F)` temporary rows and IDs; `O(F)` term-index entries at most. |
| Account import and login | Import: `O(U + N + hI)` application work for `h` passwords hashed, plus indexed DB writes. Ordinary password verification is `O(I)` at fixed password length. Issuing the token also scans `A` cached sessions, so login is `O(I + A)`; a token check or logout is expected `O(1)`. | `O(N + A)` for account/hash and session maps, plus a temporary duplicate-ID set on import and up to `O(A)` keys on session cleanup. |
| Staff account operations | Creation hashes once and reloads all profiles, giving `O(I + N)` application work. Access changes are expected `O(1)` in the profile map, plus the indexed DB update; password reset is `O(I)` plus its update. Sorting accounts for display takes `O(N log N)`. | `O(N)` to load or sort accounts; `O(1)` per access change or reset. |
| Term listing, catalogue, and staff reports | Term grouping reads the `C` section rows. A catalogue read counts matching enrolments in SQL and sorts `c` results in `O(c log c)` time in Python. Stats and exports add joins, aggregation, or sorting; a leading-wildcard registration text search can scan all rows in scope. Export writes `O(z)` rows for `z` results. | Application memory is `O(c)` for a catalogue and `O(z)` for a registration export; SQLite can use temporary storage for grouping and ordering. |
| Registration | Section lookup is about `O(log C)`; an indexed seat count takes about `O(log R + e)` and an exact duplicate check `O(log R)`. The Python rule work is `O(s + k)`. SQLite also probes for bookings among the `s` sibling IDs and can join up to `a` candidate bookings to sections before filtering to the requested term. | `O(s + k)` in Python for sibling IDs and retrieved timetable rows. |
| Cancellation | Indexed lookup and deletion by registration ID, approximately `O(log R)` for fixed indexes, without scanning the catalogue. | `O(1)` application space per cancellation. |
| Timetable layout | `O(k + D + H)` for `D ≤ 7` day columns and `H` half-hour slots; a click also searches up to `k` registration rows after canvas hit testing. | `O(k + D + H)` for the rows, grid, and blocks. |

For **client search**, let `W` be the number of characters indexed and `P` the number of distinct word–section associations. Building the index takes expected `O(W + c + P)` time. Its maps use `O(c + P)` entries, with at most two added prefix associations per word–section pair. With exact matches, each query word does an expected `O(1)` dictionary lookup; the first result set is copied and each intersection scans the smaller of the current result set and the next bucket on average. A fallback costs an additional `O(B + b)` for `B` prefix keys visited and `b` candidates checked, assuming bounded text fields. Finally, `m` matches are sorted in `O(m log m)` time (an empty query sorts all `c` results). Temporary candidate and result sets can each hold up to `c` section IDs. Thus hash lookups are fast, but a broad query, a fallback, or a full redraw is not constant-time.

The database stores `O(C + N + R)` rows, with a constant number of index entries per record. A single write lock simplifies the last-seat rule but serializes bookings during a registration rush. S04 provides a small correctness check, not a throughput benchmark.

### 4.2 Comments and possible improvements

1. **Fix the catalogue reload invariant first (S08).** Mark omitted sections with bookings as retired rather than treating them as ordinary offerings. Check the one-section rule against all of the student’s saved bookings for the course and term inside the write transaction, not just IDs in the current CSV index. Validate imports before publishing them, and read current capacity and access state inside the same transaction as the decision to enrol. Add a regression test for an omitted-but-booked section.
2. **Make section-label search unambiguous (S06).** Recognize a complete `CODE-SECTION` label as an exact section lookup before splitting free text. Use a direct dictionary lookup for the fallback bucket instead of scanning all prefix keys. Test punctuation and multi-word matching separately so improving exact labels does not change ordinary search behaviour.
3. **Verify desktop workflows on a Tk-enabled machine.** Run M01–M03 and deliberately delay a response while switching terms. The queue should keep being polled while a request is outstanding, and a reply for an old term should not replace the newly selected term. Add these cases and a larger concurrent booking test before relying on the demo during a real enrolment rush.
4. **Separate demo data from real accounts.** Disable the built-in test administrator in deployment, replace readable CSV passwords with an initial-password setup flow, and use HTTPS. Define whether staff changes or CSV updates take precedence: the current startup sync can overwrite an access change or password reset for a CSV-listed account. Revoke outstanding sessions after a password reset or role change.
5. **Measure before scaling and extend the rules as needed.** Check query plans for term filtering and try an index on `(year, term, code)` if catalogue scans become expensive. Track response latency and failed writes under realistic load. Waitlists, enforced prerequisites, and courses with multiple meeting times would make the system more representative of a university’s registration process.

The baseline suite passed all 75 cases. The most important next step is to fix and retest the catalogue reload issue because it permits a registration that violates a core rule. The search-label issue and untested GUI workflows should remain open until verified on the updated system.
