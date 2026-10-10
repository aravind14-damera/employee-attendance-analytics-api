import os
import math
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENT | {"ABSENT", "LEAVE"}

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.1.0",
)


# ---------------------------------------------------------
# Root and health
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "Employee Attendance & Analytics API is running",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(
            422,
            "Date must use YYYY-MM-DD",
        )


def month_range(month):
    try:
        start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
        if start.strftime("%Y-%m") != month:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(422, "month must use YYYY-MM")

    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)

    return start, end


def to_utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_to_datetime(value):
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)

    try:
        if value < 0:
            raise ValueError
        return datetime.fromtimestamp(value / 1000, UTC).replace(
            microsecond=0
        )
    except (ValueError, OverflowError, OSError, TypeError):
        raise HTTPException(422, "Invalid epoch-millisecond timestamp")


def datetime_to_epoch(value):
    if value is None:
        return None
    return int(to_utc(value).timestamp() * 1000)


def shift_datetime(day_text, time_text):
    day = parse_date(day_text)
    try:
        shift_time = datetime.strptime(time_text, "%H:%M").time()
    except ValueError:
        raise HTTPException(422, "Shift times must use HH:MM")

    return datetime.combine(day, shift_time, tzinfo=IST)


def scheduled_shift_end(day_text, start_time, end_time):
    start = shift_datetime(day_text, start_time)
    end = shift_datetime(day_text, end_time)

    if end <= start:
        end += timedelta(days=1)

    return end


def count_working_days(start, end):
    if start > end:
        return 0

    count = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)

    return count


def attendance_query(
    emp_code=None,
    date_from=None,
    date_to=None,
    status=None,
):
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status

    if date_from:
        parse_date(date_from)

    if date_to:
        parse_date(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")

    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter

    return query


def derive_attendance(record, employee):
    """Calculate work hours, late minutes, overtime and half-day."""

    if record["status"] not in PRESENT:
        record.update({
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })
        return record

    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")

    expected_start = to_utc(
        shift_datetime(record["date"], employee["shift_start"])
    )

    late_minutes = 0
    if punch_in:
        delay = math.floor(
            (to_utc(punch_in) - expected_start).total_seconds() / 60
        )
        if delay > 10:
            late_minutes = delay

    work_hours = None
    overtime_minutes = 0
    half_day = False

    if punch_in and punch_out:
        seconds = (
            to_utc(punch_out) - to_utc(punch_in)
        ).total_seconds()

        if seconds <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")

        work_hours = round_half_up(seconds / 3600, 2)

        scheduled_end = to_utc(
            scheduled_shift_end(
                record["date"],
                employee["shift_start"],
                employee["shift_end"],
            )
        )

        overtime = math.floor(
            (to_utc(punch_out) - scheduled_end).total_seconds() / 60
        )

        overtime_minutes = overtime if overtime >= 30 else 0
        half_day = work_hours < 4.50

    record.update({
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    })

    return record


def serialize_attendance(document):
    result = {
        key: value
        for key, value in document.items()
        if key not in {"_id", "id"}
    }

    for field in ("punch_in", "punch_out"):
        result[field] = datetime_to_epoch(result.get(field))

    for entry in result.get("history", []):
        entry["at"] = datetime_to_epoch(entry.get("at"))

        for change in entry.get("changes", {}).values():
            if isinstance(change, dict):
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = datetime_to_epoch(change[side])

    return result


def serialize_employee(document):
    result = {
        key: value
        for key, value in document.items()
        if key != "_id"
    }

    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = datetime_to_epoch(result["created_at"])

    return result


def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="employee_code_unique",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="employee_joined_on",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_date_status",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_punch_lookup",
    )


@app.on_event("startup")
def startup():
    create_indexes()


# ---------------------------------------------------------
# Request models
# ---------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None


class CorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


# ---------------------------------------------------------
# Employees
# ---------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    parse_date(body.joined_on)

    for value in (body.shift_start, body.shift_end):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise HTTPException(422, "Shift times must use HH:MM")

    document = body.model_dump()
    document["created_at"] = datetime.now(UTC)

    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = datetime_to_epoch(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------
# Attendance: punch in and punch out
# ---------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found")

    if body.status not in PRESENT:
        raise HTTPException(422, "status must be PRESENT, WFH or ON_DUTY")

    instant = epoch_to_datetime(body.punched_at)
    local = instant.astimezone(IST).replace(microsecond=0)
    date_text = local.date().isoformat()

    record = {
        "emp_code": body.emp_code,
        "date": date_text,
        "status": body.status,
        "punch_in": to_utc(local),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    derive_attendance(record, employee)

    try:
        result = db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Attendance already exists for this employee and date",
        )

    record["_id"] = result.inserted_id
    return serialize_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found")

    instant = to_utc(epoch_to_datetime(body.punched_at))

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$ne": None, "$lt": instant},
            "status": {"$in": list(PRESENT)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No open punch-in found")

    if instant > to_utc(record["punch_in"]) + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be within 24 hours")

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": instant}},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Attendance already punched out")

    derive_attendance(updated, employee)

    db.attendance_logs.update_one(
        {"_id": updated["_id"]},
        {
            "$set": {
                key: updated[key]
                for key in (
                    "work_hours",
                    "late_minutes",
                    "overtime_minutes",
                    "half_day",
                )
            }
        },
    )

    return serialize_attendance(updated)


# ---------------------------------------------------------
# Attendance list and pagination
# ---------------------------------------------------------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)

    documents = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(doc) for doc in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------
# Attendance correction with audit history
# ---------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: CorrectionIn,
):
    parse_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    if not record:
        raise HTTPException(404, "Attendance record not found")

    status = body.status if body.status is not None else record["status"]
    if status not in STATUSES:
        raise HTTPException(422, "Invalid status")

    punch_in_time = (
        epoch_to_datetime(body.punch_in)
        if body.punch_in is not None
        else record.get("punch_in")
    )
    punch_out_time = (
        epoch_to_datetime(body.punch_out)
        if body.punch_out is not None
        else record.get("punch_out")
    )

    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot have punch times",
            )
        punch_in_time = None
        punch_out_time = None
    elif punch_in_time is None:
        raise HTTPException(422, "Presence status requires punch_in")

    if (
        punch_in_time is not None
        and punch_in_time.astimezone(IST).date().isoformat() != date
    ):
        raise HTTPException(422, "punch_in must fall on the attendance date")

    if punch_out_time is not None:
        if (
            punch_in_time is None
            or to_utc(punch_out_time) <= to_utc(punch_in_time)
            or to_utc(punch_out_time)
            > to_utc(punch_in_time) + timedelta(hours=24)
        ):
            raise HTTPException(422, "Invalid punch_out")

    proposed = dict(
        record,
        status=status,
        punch_in=to_utc(punch_in_time) if punch_in_time else None,
        punch_out=to_utc(punch_out_time) if punch_out_time else None,
    )
    derive_attendance(proposed, employee)

    fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )

    changes = {
        key: {
            "from": record.get(key),
            "to": proposed.get(key),
        }
        for key in fields
        if record.get(key) != proposed.get(key)
    }

    if not changes:
        raise HTTPException(422, "Request does not change the record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": {key: proposed.get(key) for key in fields},
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Record changed concurrently; retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# ---------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    start, end = month_range(month)
    joined = parse_date(employee["joined_on"])
    effective_start = max(start, joined)

    working = count_working_days(
        effective_start,
        end - timedelta(days=1),
    )

    query = {
        "emp_code": emp_code,
        "date": {
            "$gte": effective_start.isoformat(),
            "$lt": end.isoformat(),
        },
    }

    logs = list(db.attendance_logs.find(query))

    present_days = sum(
        0.5 if log.get("half_day") else 1.0
        for log in logs
        if log.get("status") in PRESENT
        and parse_date(log["date"]).weekday() < 5
    )

    leave_days = sum(1 for log in logs if log.get("status") == "LEAVE")
    late_count = sum(1 for log in logs if log.get("late_minutes", 0) > 0)
    total_late = sum(log.get("late_minutes", 0) for log in logs)
    total_overtime = sum(log.get("overtime_minutes", 0) for log in logs)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": (
            round_half_up(present_days / working * 100, 4)
            if working > 0 else None
        ),
    }


# ---------------------------------------------------------
# Department monthly summary
# ---------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    start, end = month_range(month)
    month_end = (end - timedelta(days=1)).isoformat()

    employee_query = {"joined_on": {"$lte": month_end}}
    if department:
        employee_query["department"] = department

    employees = list(db.employees.find(employee_query, {"_id": 0}))

    grouped = {}

    for employee in employees:
        dept = employee["department"]
        grouped.setdefault(dept, {
            "department": dept,
            "headcount": 0,
            "present_days": 0.0,
            "hours": [],
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        })
        grouped[dept]["headcount"] += 1

        logs = db.attendance_logs.find({
            "emp_code": employee["emp_code"],
            "date": {
                "$gte": max(start, parse_date(employee["joined_on"])).isoformat(),
                "$lt": end.isoformat(),
            },
        })

        for log in logs:
            day = parse_date(log["date"])

            if (
                log.get("status") in PRESENT
                and day.weekday() < 5
            ):
                grouped[dept]["present_days"] += (
                    0.5 if log.get("half_day") else 1.0
                )

            if log.get("work_hours") is not None:
                grouped[dept]["hours"].append(log["work_hours"])

            if log.get("late_minutes", 0) > 0:
                grouped[dept]["late_count"] += 1

            grouped[dept]["total_late_minutes"] += log.get("late_minutes", 0)

            if log.get("status") == "LEAVE":
                grouped[dept]["leave_count"] += 1

            if log.get("status") == "ON_DUTY":
                grouped[dept]["on_duty_count"] += 1

    items = []
    for dept in sorted(grouped):
        item = grouped[dept]
        hours = item.pop("hours")
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = (
            round_half_up(sum(hours) / len(hours), 2)
            if hours else None
        )
        items.append(item)

    return {"month": month, "items": items}


# ---------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_range(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start.isoformat(),
                    "$lt": end.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


# ---------------------------------------------------------
# Department trend
# ---------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)

    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    employees = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    ))

    if not employees:
        raise HTTPException(404, "Unknown department")

    codes = [employee["emp_code"] for employee in employees]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {
                    "$gte": from_date,
                    "$lte": to_date,
                },
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENT)]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    rows = {
        row["_id"]: row
        for row in db.attendance_logs.aggregate(pipeline)
    }

    items = []
    rates = []
    current = start

    while current <= end:
        day_text = current.isoformat()
        working = current.weekday() < 5

        headcount = sum(
            1 for employee in employees
            if employee["joined_on"] <= day_text
        )

        row = rows.get(day_text, {})
        present = row.get("present_count", 0)

        rate = (
            round_half_up(present / headcount, 4)
            if working and headcount > 0
            else None
        )

        rates.append(rate)
        recent = [
            value for value in rates[-7:]
            if value is not None
        ]

        items.append({
            "date": day_text,
            "is_working_day": working,
            "headcount": headcount,
            "present_count": present,
            "late_count": row.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": (
                round_half_up(sum(recent) / len(recent), 4)
                if recent else None
            ),
        })

        current += timedelta(days=1)

    return {
        "department": department,
        "items": items,
    }


# ---------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = attendance_query(
            emp_code,
            date_from,
            date_to,
            status,
        )

        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

        return db.command(
            "explain",
            command,
            verbosity="executionStats",
        )

    if endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(
                422,
                "month and emp_code are required",
            )

        start, end = month_range(month)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start.isoformat(),
                        "$lt": end.isoformat(),
                    },
                }
            }
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        start, end = month_range(month)
        query = {
            "date": {
                "$gte": start.isoformat(),
                "$lt": end.isoformat(),
            }
        }

        return db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
            },
            verbosity="executionStats",
        )

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        start, end = month_range(month)

        query = {
            "date": {
                "$gte": start.isoformat(),
                "$lt": end.isoformat(),
            },
            "late_minutes": {"$gt": 0},
        }

        return db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"late_minutes": -1},
                "limit": limit,
            },
            verbosity="executionStats",
        )

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422,
                "department, from and to are required",
            )

        start = parse_date(from_date)
        end = parse_date(to_date)

        if end < start:
            raise HTTPException(422, "'to' must not be before 'from'")

        if (end - start).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        employees = list(db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1},
        ))

        if not employees:
            raise HTTPException(404, "Unknown department")

        pipeline = [
            {
                "$match": {
                    "emp_code": {
                        "$in": [employee["emp_code"] for employee in employees]
                    },
                    "date": {
                        "$gte": from_date,
                        "$lte": to_date,
                    },
                }
            }
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    raise HTTPException(
        422,
        "Unsupported endpoint. Use attendance_list, employee_monthly, "
        "department_summary, late_leaderboard or department_trend.",
    )