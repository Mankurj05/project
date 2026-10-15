# DECISIONS.md

1. **Indexes.** Created unique index on `employees.emp_code` to enforce uniqueness. Created compound unique index on `attendance_logs(emp_code, date)` to prevent duplicate records and serve the natural key lookups. Created index on `attendance_logs(date DESC, emp_code ASC)` for the list endpoint's sort pattern. Created index on `attendance_logs(date ASC)` for analytics range queries. Created index on `employees(department ASC)` for department filtering. I considered an index on `attendance_logs.status` but rejected it because status filtering is combined with other filters and the compound indexes already cover the common access patterns.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Both attempt insert_one with the same (emp_code, date). The unique index guarantees only one succeeds with 201. The second hits DuplicateKeyError and returns 409. The first wins, the second loses - no check-then-insert gap.

3. **Ties.** Two employees are tied on late_minutes at the cutoff of limit. My leaderboard returns both employees at that rank because limit applies after ranking. For example, with limit=2 and ranks 1, 2, 2, 4 - it returns all three employees with ranks 1 and 2 (since rank 2 <= limit). Tied rows are ordered by emp_code ascending to ensure deterministic output.

4. **Headcount.** Department summary makes sure employees with zero logs are counted by using a $lookup aggregation stage to count all employees in the department whose joined_on is before the month end, regardless of whether they have attendance records. This ensures the headcount reflects the department size even for new hires or people with no punches.

5. **One thing you would change** if this had to serve 100x the data: I would add read replicas and implement query result caching for the analytics endpoints, especially department_trend which computes complex aggregations over date ranges. The current design is single-node synchronous, which would struggle with the increased load and query complexity at scale.
