# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | health (line 72-74) | Health check doesn't verify MongoDB connectivity | MongoDB down would still return 200 | Added db.command('ping') with try/except to return 503 on failure |
| 2 | create_employee (line 79-80) | Race condition: check-then-insert pattern allows concurrent duplicate creates | Two simultaneous POSTs with same emp_code could both succeed | Changed to rely on unique index with try/except DuplicateKeyError |
| 3 | create_employee (line 82) | created_at uses naive datetime (no timezone) | Timezone inconsistencies in stored data | Changed to datetime.now(UTC) for explicit UTC |
| 4 | create_employee (line 84) | Returns document with created_at as datetime instead of epoch ms | API contract violation (should be epoch ms) | Convert to epoch milliseconds before returning |
| 5 | list_employees (line 93) | Pagination uses page * page_size instead of (page-1) * page_size | Page 1 skips 20 items instead of 0 | Changed to (page - 1) * page_size |
| 6 | list_employees (line 94) | total counts all documents, not filtered set | Total count doesn't match filtered results | Changed to count_documents(q) with same filter |
| 7 | list_employees (line 95) | created_at returned as datetime instead of epoch ms | API contract violation | Convert to epoch milliseconds for each item |
| 8 | list_employees (line 95) | Missing sort by emp_code as required by contract | Results not sorted as specified | Added .sort("emp_code", ASCENDING) |
| 9 | punch_in (line 102) | punched_at uses fromtimestamp without timezone | Timezone ambiguity, wrong IST date calculation | Changed to epoch_to_datetime which returns UTC datetime |
| 10 | punch_in (line 103) | Date calculation doesn't handle overnight shifts (R1) | Wrong attendance date for overnight shifts | Implemented compute_attendance_date with overnight shift logic |
| 11 | punch_in (line 104) | Race condition: check-then-insert pattern | Concurrent punch-ins could both succeed | Changed to rely on unique index with try/except DuplicateKeyError |
| 12 | punch_in (line 113) | compute_late_minutes doesn't implement R2 correctly (uses > 10 instead of strictly more than 10:00) | Late minutes calculation incorrect per spec | Rewrote to use strict > 10 comparison and floor elapsed minutes |
| 13 | punch_in (line 119) | Returns "id" field which is not in the contract | API contract violation (no id field) | Removed the id field, only return contract-specified fields |
| 14 | punch_in (line 119) | punch_in returned as datetime instead of epoch ms | API contract violation | Convert to epoch milliseconds before returning |
| 15 | list_attendance (line 144-145) | Loads all documents into memory before sorting and pagination | Fails on large datasets (~100k records) | Changed to use MongoDB sort/skip/limit with find cursor |
| 16 | list_attendance (line 145) | Sort only by date, missing emp_code secondary sort | Incorrect order when dates match | Changed to sort by date desc, emp_code asc |
| 17 | list_attendance (line 149) | Returns "id" field which is not in the contract | API contract violation | Removed the id field |
| 18 | list_attendance (line 149) | punch_in/punch_out returned as datetime instead of epoch ms | API contract violation | Convert to epoch milliseconds for each document |
| 19 | compute_late_minutes (line 33-37) | Doesn't truncate punch_in to seconds before calculation (R1) | Sub-second times affect late calculation | Punch-in is truncated to seconds before calling this function |
| 20 | compute_work_hours (line 40-41) | Uses Python's round which is banker's rounding, not half-up (R4) | Work hours rounding incorrect | Changed to use Decimal with ROUND_HALF_UP |
| 21 | compute_overtime (line 44-47) | Doesn't implement R3 (minimum 30 minutes) | Overtime calculated even when < 30 minutes | Changed to only count overtime when >= 30 minutes |
| 22 | compute_overtime (line 46) | Uses datetime.fromisoformat which assumes UTC, wrong for IST | Incorrect overtime calculation for IST shifts | Removed this function, logic integrated into recompute_derived_fields |
| 23 | No validation for punched_at range (100000000000 to 4102444800000) | Invalid timestamps accepted | Added validation in Pydantic models with ge/le constraints |
| 24 | No indexes created at startup | Slow queries on large datasets | Added startup event to create required indexes |
| 25 | Missing all analytics endpoints | Contract not fulfilled | Implemented all analytics endpoints with MongoDB aggregations |
| 26 | Missing punch-out endpoint | Contract not fulfilled | Implemented with proper concurrency handling |
| 27 | Missing regularization endpoint | Contract not fulfilled | Implemented with history tracking |
| 28 | Missing explain endpoint | Contract not fulfilled | Implemented to return explain plans |

Also note anything you looked at and decided was **not** a defect, and why:
- The helper function structure was fine; I reused the concept but rewrote the implementations to match the business rules correctly.
- The basic Pydantic model structure was correct; I added proper validation constraints but the pattern was sound.
- The FastAPI app initialization was correct; no changes needed there.
