# Registration test log

[Back to the group project report](../../GROUP_PROJECT_REPORT.md)

**Date:** 18 November 2026. All times below are UTC.

**Implementation:** `e847861bf71490fba5b7a5bcae559f75e1fa857e`, tested on Linux x86-64, Python 3.11.2, SQLite 3.40.1.

**Isolation:** Tests used temporary SQLite files. The HTTP integration tests listened on an ephemeral loopback port. The tracked databases, source files, and CSVs were not modified by testing.

| Run | Time (UTC) | What happened | Status / next action |
|---|---|---|---|
| R1 | 08:28:04–08:28:35 | Ran all 75 built-in tests. Exit 0. | **Pass.** No failures or skips in the built-in suite. |
| R2a | 08:30:09–08:30:10 | First supplemental attempt stopped when the current catalogue did not match the count in the application README. Exit 1. | **Corrected** the report’s expectation after checking `courses.csv`; the application README was not edited. |
| R4 | 08:30:09 | Tried to import Tkinter. Exit 1: Python could not import the module. | **Pending, not run:** M01–M03 require a Tk-enabled desktop. |
| R2b | 08:30:27–08:30:28 | Retried the probes; the expected exact section-label search returned an extra result. Exit 1. | **Pending:** kept this as S06 and made the final run continue to the other checks. |
| R3 | 08:30:27 | Ran the project’s comment-ratio tool. Exit 0. | **Pass:** 31.6% versus the tool’s 30% requirement. |
| R2 | 08:30:49–08:30:51 | Completed the replayable supplemental run. Six cases matched expectations, two did not. Exit 1. | **Pending:** S06 (search label) and S08 (one-section rule after CSV reload). Neither application issue was fixed in this report. |

## R1 Built-in suite

Command from `class_registration/`: `PYTHONDONTWRITEBYTECODE=1 python3 test_registration.py`. The following is the complete combined stdout/stderr. Each test is named so it can be located in [`test_registration.py`](../../class_registration/test_registration.py).

```text
test_access_can_be_switched_back_on (__main__.TestAccounts.test_access_can_be_switched_back_on) ... ok
test_access_can_be_switched_off (__main__.TestAccounts.test_access_can_be_switched_off) ... ok
test_access_switch_needs_a_known_student (__main__.TestAccounts.test_access_switch_needs_a_known_student) ... ok
test_add_student (__main__.TestAccounts.test_add_student)
A new account is usable immediately - including for logging in. ... ok
test_add_student_can_start_blocked (__main__.TestAccounts.test_add_student_can_start_blocked) ... ok
test_add_student_duplicate_is_refused (__main__.TestAccounts.test_add_student_duplicate_is_refused)
The students dictionary is the duplicate guard for account creation. ... ok
test_add_student_validation (__main__.TestAccounts.test_add_student_validation)
Bad id, short name, bad e-mail and a weak password are all refused. ... ok
test_admin_account_can_be_disabled (__main__.TestAccounts.test_admin_account_can_be_disabled)
A real deployment passes admin_account=None and gets no back door. ... ok
test_admin_account_rejects_a_wrong_password (__main__.TestAccounts.test_admin_account_rejects_a_wrong_password)
The built-in account is still password-protected. ... ok
test_admin_test_account_exists_without_the_database (__main__.TestAccounts.test_admin_test_account_exists_without_the_database)
The built-in admin works even with a completely empty sheet. ... ok
test_blocked_account_cannot_register (__main__.TestAccounts.test_blocked_account_cannot_register)
S9002 is blocked in the CSV, so registering fails with 403. ... ok
test_blocking_keeps_existing_registrations (__main__.TestAccounts.test_blocking_keeps_existing_registrations)
Blocking stops new registrations but never deletes the old ones. ... ok
test_can_register_flag_from_csv (__main__.TestAccounts.test_can_register_flag_from_csv)
A student can ship already blocked, straight from the CSV. ... ok
test_login_rejects_bad_credentials (__main__.TestAccounts.test_login_rejects_bad_credentials) ... ok
test_login_returns_the_public_record (__main__.TestAccounts.test_login_returns_the_public_record) ... ok
test_logout_forgets_the_token (__main__.TestAccounts.test_logout_forgets_the_token) ... ok
test_password_reset (__main__.TestAccounts.test_password_reset)
An admin reset invalidates the old password immediately. ... ok
test_password_verification (__main__.TestAccounts.test_password_verification) ... ok
test_passwords_are_hashed_not_stored (__main__.TestAccounts.test_passwords_are_hashed_not_stored)
The stored value is a salted PBKDF2 string, never the password. ... ok
test_role_comes_from_the_csv (__main__.TestAccounts.test_role_comes_from_the_csv)
The role column decides who may use the admin endpoints. ... ok
test_sessions_are_issued_and_expire (__main__.TestAccounts.test_sessions_are_issued_and_expire)
A token is only valid until its expiry time. ... ok
test_students_csv_is_loaded (__main__.TestAccounts.test_students_csv_is_loaded)
Every unique row of students.csv becomes an account. ... ok
test_tokens_are_unguessable (__main__.TestAccounts.test_tokens_are_unguessable)
Two logins produce different, long, random tokens. ... ok
test_course_index_groups_sessions (__main__.TestDictionaries.test_course_index_groups_sessions)
course_index[(year, term)][code] lists every session of that code. ... ok
test_course_index_is_per_term (__main__.TestDictionaries.test_course_index_is_per_term)
A code offered in term 1 is absent from term 2's dictionary. ... ok
test_new_student_requires_details (__main__.TestDictionaries.test_new_student_requires_details)
An unknown id without a name or e-mail is a validation error. ... ok
test_public_dictionary_has_no_password (__main__.TestDictionaries.test_public_dictionary_has_no_password)
The dictionary the API can leak never contains the hash. ... ok
test_reload_keeps_students (__main__.TestDictionaries.test_reload_keeps_students)
Reloading the course CSV does not lose the student dictionary. ... ok
test_student_details_are_not_overwritten (__main__.TestDictionaries.test_student_details_are_not_overwritten)
A typo in a later submission cannot rename an existing student. ... ok
test_student_dictionary_prevents_repetition (__main__.TestDictionaries.test_student_dictionary_prevents_repetition)
The first registration stores the student, later ones reuse it. ... ok
test_admin_can_add_a_student (__main__.TestHttpApi.test_admin_can_add_a_student) ... ok
test_admin_can_list_accounts (__main__.TestHttpApi.test_admin_can_list_accounts) ... ok
test_admin_can_reset_a_password (__main__.TestHttpApi.test_admin_can_reset_a_password) ... ok
test_admin_can_toggle_registration_access (__main__.TestHttpApi.test_admin_can_toggle_registration_access)
Requirement 3, end to end: block, try, unblock, succeed. ... ok
test_blocked_student_cannot_register_over_http (__main__.TestHttpApi.test_blocked_student_cannot_register_over_http) ... ok
test_cancel_requires_ownership (__main__.TestHttpApi.test_cancel_requires_ownership) ... ok
test_error_shape (__main__.TestHttpApi.test_error_shape)
Errors must always be JSON with a code - the client depends on it. ... ok
test_health_is_public (__main__.TestHttpApi.test_health_is_public) ... ok
test_html_course_sheet (__main__.TestHttpApi.test_html_course_sheet)
The browser view renders with a real course in it. ... ok
test_login_flow (__main__.TestHttpApi.test_login_flow) ... ok
test_me_and_logout (__main__.TestHttpApi.test_me_and_logout) ... ok
test_protected_endpoints_need_a_token (__main__.TestHttpApi.test_protected_endpoints_need_a_token)
Without a session the sheet is not readable at all. ... ok
test_register_uses_the_session_identity (__main__.TestHttpApi.test_register_uses_the_session_identity)
No student id is sent; the server takes it from the token. ... ok
test_registration_cannot_be_forced_onto_another_student (__main__.TestHttpApi.test_registration_cannot_be_forced_onto_another_student) ... ok
test_registrations_are_scoped_to_the_student (__main__.TestHttpApi.test_registrations_are_scoped_to_the_student)
A student asking for someone else's list still gets their own. ... ok
test_student_can_read_only_their_own_record (__main__.TestHttpApi.test_student_can_read_only_their_own_record) ... ok
test_student_cannot_use_the_admin_api (__main__.TestHttpApi.test_student_cannot_use_the_admin_api)
Hiding the admin button in the client is cosmetic; this is the check. ... ok
test_student_dictionary_is_admin_only (__main__.TestHttpApi.test_student_dictionary_is_admin_only) ... ok
test_terms_and_term_filtering (__main__.TestHttpApi.test_terms_and_term_filtering) ... ok
test_upgrade_keeps_data_and_adds_columns (__main__.TestMigration.test_upgrade_keeps_data_and_adds_columns) ... ok
test_at_least_one_blocked_and_one_admin_account (__main__.TestRealSheet.test_at_least_one_blocked_and_one_admin_account)
The shipped data demonstrates both access states out of the box. ... ok
test_every_shipped_account_can_log_in (__main__.TestRealSheet.test_every_shipped_account_can_log_in)
Whatever password the CSV hands out must actually work. ... ok
test_real_accounts_are_loaded_and_hashed (__main__.TestRealSheet.test_real_accounts_are_loaded_and_hashed)
The shipped students.csv gives every student a hashed password. ... ok
test_sheet_has_several_terms (__main__.TestRealSheet.test_sheet_has_several_terms)
The term selector needs something to select: three terms expected. ... ok
test_the_sheet_contains_overlaps (__main__.TestRealSheet.test_the_sheet_contains_overlaps)
Overlapping classes exist, otherwise the clash rule is untested. ... ok
test_times_are_well_formed (__main__.TestRealSheet.test_times_are_well_formed)
Every row has a sane HH:MM window and a positive capacity. ... ok
test_which_courses_are_offered_more_than_once (__main__.TestRealSheet.test_which_courses_are_offered_more_than_once)
Repeated codes are expected - they are the different sessions. ... ok
test_ambiguous_code_needs_a_section (__main__.TestRules.test_ambiguous_code_needs_a_section)
Sending only 'CS101' when three sessions exist is refused. ... ok
test_cancel_frees_the_seat (__main__.TestRules.test_cancel_frees_the_seat) ... ok
test_cancel_ownership_is_checked (__main__.TestRules.test_cancel_ownership_is_checked)
A student cannot cancel a registration that is not theirs. ... ok
test_capacity_is_enforced (__main__.TestRules.test_capacity_is_enforced)
CS101-A holds 2 students; the third is refused with FULL. ... ok
test_code_from_wrong_term_is_rejected (__main__.TestRules.test_code_from_wrong_term_is_rejected)
MA110 is not offered in term 2, so it cannot be registered there. ... ok
test_duplicate_registration (__main__.TestRules.test_duplicate_registration)
The same student cannot take the same section twice. ... ok
test_normal_registration (__main__.TestRules.test_normal_registration) ... ok
test_only_one_session_per_course_code (__main__.TestRules.test_only_one_session_per_course_code)
CS101-A and CS101-B are two sessions of ONE course: pick one. ... ok
test_other_courses_are_still_allowed (__main__.TestRules.test_other_courses_are_still_allowed)
The one-session rule must not block unrelated courses. ... ok
test_same_course_different_day_is_allowed (__main__.TestRules.test_same_course_different_day_is_allowed)
MA110-B is on Tuesday, so it does not clash with CS101-C. ... ok
test_same_course_in_another_term_is_allowed (__main__.TestRules.test_same_course_in_another_term_is_allowed)
A student may take a course again in the next term. ... ok
test_stats_respect_the_term (__main__.TestRules.test_stats_respect_the_term) ... ok
test_timetable_clash (__main__.TestRules.test_timetable_clash)
CS101-C (Mon 11:00) and MA110-A (Mon 11:00) cannot both be taken. ... ok
test_validation (__main__.TestRules.test_validation) ... ok
test_all_sections_loaded (__main__.TestSheetLoading.test_all_sections_loaded)
Seven good rows out of eight: the duplicate row is skipped. ... ok
test_duplicate_section_id_is_refused (__main__.TestSheetLoading.test_duplicate_section_id_is_refused)
The repeated CS101-A row must not overwrite the first one. ... ok
test_seats_are_computed (__main__.TestSheetLoading.test_seats_are_computed)
capacity, enrolled and seats_left always agree. ... ok
test_section_ids_are_well_formed (__main__.TestSheetLoading.test_section_ids_are_well_formed)
Every section id follows year-Tterm-CODE-SECTION. ... ok

----------------------------------------------------------------------
Ran 75 tests in 30.013s

OK
[server] database upgraded v2 -> v3 (students gained password_hash, can_register, role)
```

## R2 Supplemental checks

Command from the repository root: `PYTHONDONTWRITEBYTECODE=1 python3 docs/report-evidence/checks.py`. The [replay script](checks.py) uses the existing fixture definitions and temporary database files. For S05–S07 it executes the relevant client definitions without importing Tkinter; S07 tests the day and time-window calculations but not drawing or clicking. A replay with the included script at 08:34:46–08:34:47 UTC produced identical output and exit status. A nonzero exit is expected on the evaluated revision because two cases do not match the desired behaviour.

```text
S01 pass: (year, term, sections, course codes) = [(2026, 1, 20, 13), (2026, 2, 13, 11), (2027, 1, 10, 9)]; CSV accounts = 16; 2026 T1 overlap pairs = 8
S02 pass: same code and time across academic years, registrations = ['2026-T1-CS101-A', '2027-T1-CS101-A']
S03 pass: 10:29 start against 10:30 end = (409, 'TIME_CONFLICT'); 10:30 start = 2026-T1-ZZ900-A accepted
S04 trial 1/5 pass: 1 accepted, 3 FULL, enrolled = 2/2
S04 trial 2/5 pass: 1 accepted, 3 FULL, enrolled = 2/2
S04 trial 3/5 pass: 1 accepted, 3 FULL, enrolled = 2/2
S04 trial 4/5 pass: 1 accepted, 3 FULL, enrolled = 2/2
S04 trial 5/5 pass: 1 accepted, 3 FULL, enrolled = 2/2
S05 pass: term 1 query '' -> ['CS101-A', 'CS101-B', 'CS101-C', 'EN150-A', 'MA110-A', 'MA110-B']
S05 pass: term 1 query 'CS101' -> ['CS101-A', 'CS101-B', 'CS101-C']
S05 pass: term 1 query 'mon calc' -> ['MA110-A']
S05 pass: term 1 query 'prog' -> ['CS101-A', 'CS101-B', 'CS101-C']
S05 pass: term 1 query 'CS101B' -> ['CS101-B']
S05 pass: term 1 query 'zzno' -> []
S05 pass: after rebuild, term 2 empty query -> ['CS102-A']; 'CS101' -> []
S06 pending (unexpected): displayed label 'CS101-B' -> ['CS101-B', 'CS101-C']; expected ['CS101-B']
S07 pass: empty timetable -> columns ('Mon', 'Tue', 'Wed', 'Thu', 'Fri'), time range (540, 900) minutes
S07 pass: weekend rows -> columns ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'), time range (480, 1080) minutes
S08 after reload: SQL labels = ['CS101-A', 'CS101-B', 'CS101-C']; course_index section IDs = ['2026-T1-CS101-B', '2026-T1-CS101-C']
S08 pending (unexpected): expected ONE_SESSION; section B accepted; stored registrations = ['2026-T1-CS101-A', '2026-T1-CS101-B']
Supplemental summary: 6 pass, 2 pending (S06, S08).
```

**Open findings**

| Case | Reproduction and likely cause | Follow-up |
|---|---|---|
| S06 · Section-label search | In the fixture, search for the displayed label `CS101-B`. The term-one index returns B and C. The search splits on `-`, and section C matches both tokens (`CS101` in the code, `B` in `Dr. B`). | Add direct matching for complete section labels. Retest the label and ordinary multi-word searches. |
| S08 · One-section rule after reload | With CS101-A already booked, remove that section from the fixture CSV and reload. SQLite keeps A to preserve its booking, but the rebuilt sibling index omits it. The same student’s attempt to book the Wednesday section B then misses the sibling check. | Check the one-section invariant using saved bookings in the transaction, and define which retained sections remain selectable. Retest that A is preserved, B is refused, and unrelated bookings still work. |

### Earlier attempts (excerpts)

- **R2a:** the probe expected 2026 Term 1 to have 19 sections and 12 course codes, as described in the README. Actual SQL grouping: `[(2026, 1, 20, 13), (2026, 2, 13, 11), (2027, 1, 10, 9)]`. The current CSV also includes `SV1001-A` in 2026 Term 1. The assertion was updated to reflect the CSV, which is the input to the program.
- **R2b:** the exploratory search assertion halted before the timetable and reload checks: `AssertionError: ('CS101-B', ['CS101-B', 'CS101-C'])`. The final run reports this result as S06 and proceeds through S08.

## R3 Comment-ratio check

Command from `class_registration/`: `PYTHONDONTWRITEBYTECODE=1 python3 comment_ratio.py`. The tool counts comment and docstring lines against nonblank lines in five selected files.

```text
file                       lines  comments    ratio
---------------------------------------------------
server.py                   1601       540    33.7%
client_gui.py               1522       460    30.2%
server_gui.py               1004       303    30.2%
theme.py                     171        66    38.6%
test_registration.py         813       245    30.1%
---------------------------------------------------
TOTAL                       5111      1614    31.6%

Requirement: at least 30%  ->  PASS
```

## R4 GUI environment check

Command: `PYTHONDONTWRITEBYTECODE=1 python3 -c 'import tkinter'`.

```text
Traceback (most recent call last):
  File "<string>", line 1, in <module>
ModuleNotFoundError: No module named 'tkinter'
```

Desktop sign-in, term switching, timetable drawing and selection, and console CSV export are **not verified** by this run. Run M01–M03 in a Tk-enabled desktop session using a fresh demo database, and save the actual results alongside this log.
