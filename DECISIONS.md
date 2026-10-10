# DECISIONS.md

### 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employees. The department and employee-code index supports department-based employee queries, while the `joined_on` index supports joining-date filtering. For attendance, a unique compound index on `emp_code` and `date` prevents duplicate daily records. The date and employee-code index supports attendance listing and sorting, the date and status index supports attendance filtering, and the employee-code and punch-in index supports employee attendance lookups. I did not add a standalone index on `late_minutes` because leaderboard results require aggregation and ranking.

### 2. Punch-in race

If two identical punch-in requests arrive simultaneously, both may attempt to insert a record. The unique employee/date index allows only one record. The successful request returns 201, while the duplicate-key error is handled as a conflict and returns 409.

### 3. Leaderboard ties

The leaderboard uses competition ranking, so employees with equal late minutes receive the same rank. The limit is applied after ranking, allowing additional employees tied at the cutoff rank to appear.

### 4. Headcount

The department summary starts with employee records and combines attendance data, so employees who have no attendance logs are still included in headcount.

### 5. Scaling to 100x data

I would review query execution plans, benchmark aggregation pipelines, and add or adjust compound indexes based on measured workloads. I would also consider caching frequently requested analytics.
