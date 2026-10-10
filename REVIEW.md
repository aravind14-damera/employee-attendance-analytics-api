# REVIEW.md

## Defects found in the starter implementation

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `health()` | Returns success without checking MongoDB availability. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 if unavailable. |
| 2 | `create_employee()` | Checking for duplicates before inserting is race-prone. | Submit simultaneous requests with the same employee code. | Add a unique employee-code index and handle duplicate-key errors as 409. |
| 3 | `create_employee()` | Required fields and formats are insufficiently validated. | Submit invalid email, date, or shift time. | Add request validation and reject invalid input with 422. |
| 4 | `create_employee()` | Uses a naive creation timestamp. | Inspect stored timestamps across time zones. | Store a timezone-aware UTC timestamp. |
| 5 | `list_employees()` | Calculates the skip offset as `page * page_size`. | Request page 1 with page size 20; the first 20 employees are skipped. | Use `(page - 1) * page_size`. |
| 6 | `list_employees()` | Calculates `total` without applying the department filter. | Compare totals for all employees and one department. | Count using the same filter as the list query. |
| 7 | `list_employees()` | Does not sort results by employee code. | Insert employees in a different order and request the list. | Sort by `emp_code` in MongoDB before pagination. |
| 8 | `list_employees()` | Does not enforce the pagination limits. | Try page 0, page size 0, or page size 101. | Validate page and page size; cap page size at 100. |
| 9 | `compute_late_minutes()` | Uses the punch-in date and timezone naively when constructing shift start. | Test a late punch and a timestamp near midnight. | Compare timezone-aware timestamps against the correct shift start. |
| 10 | `compute_work_hours()` | Uses Python's default rounding, which uses ties-to-even. | Test a duration whose hour value is exactly halfway between two hundredths. | Use decimal half-up rounding to two decimal places. |
| 11 | `compute_overtime()` | Assumes the shift ends on the same date and does not enforce the 30-minute minimum. | Test an overnight shift and overtime of 29 versus 30 minutes. | Calculate the correct shift-end date and return overtime only when it reaches 30 minutes. |
| 12 | `punch_in()` | Does not return 404 when the employee does not exist before accessing shift information. | Punch in using an unknown employee code. | Check employee existence and return 404. |
| 13 | `punch_in()` | A pre-insert duplicate check cannot guarantee concurrency safety. | Send two simultaneous punch-ins for the same employee and date. | Enforce a unique `(emp_code, date)` index and convert duplicate-key errors to 409. |
| 14 | `punch_in()` | Treats a timestamp of zero as if no timestamp was supplied and uses a naive local timestamp. | Submit `punched_at: 0` or compare results across time zones. | Check explicitly for `None`, validate timestamps, and normalize time consistently. |
| 15 | `list_attendance()` | Fetches every matching record into Python before sorting and paginating. | Test with a large attendance collection and inspect memory and latency. | Filter, sort, count, skip, and limit in MongoDB using suitable indexes. |
| 16 | `list_attendance()` | Sorts only after retrieving records, and pagination relies on Python slicing. | Request later pages from a large dataset. | Apply deterministic database sorting and pagination. |
| 17 | `list_attendance()` | Does not validate pagination or date inputs. | Supply invalid dates, page 0, or an oversized page. | Validate inputs and return 422 for invalid parameters. |

## Areas examined and not considered defects

- The starter correctly uses FastAPI route decorators and Pydantic request models as a basic API structure.
- The duplicate employee check is useful for an ordinary request, but it is not sufficient to prevent concurrent duplicates.
- The attendance date range uses inclusive `$gte` and `$lte` comparisons, which are appropriate for inclusive date filters when the inputs are validated.
- Returning 409 for an already-recorded punch-in is the right conflict behavior; the original implementation needs a database constraint to make it race-safe.
