"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: Create indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup():
    # Create unique index on emp_code for employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    
    # Create compound index for attendance_logs: emp_code + date (unique)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    
    # Index for list_attendance: date desc, emp_code asc
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    
    # Index for analytics: date
    db.attendance_logs.create_index([("date", ASCENDING)])
    
    # Index for employee lookups
    db.employees.create_index([("department", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def epoch_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_epoch(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def ist_datetime_to_date(dt: datetime) -> str:
    """
    Convert UTC datetime to IST calendar date string (YYYY-MM-DD).
    Handles overnight shifts per R1.
    """
    ist_dt = dt.astimezone(IST)
    return ist_dt.date().isoformat()


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute the attendance date per R1.
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end
    belongs to the previous day's shift.
    """
    ist_time = punch_in_utc.astimezone(IST)
    ist_hour = ist_time.hour
    ist_minute = ist_time.minute
    ist_minutes = ist_hour * 60 + ist_minute
    
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    shift_start_minutes = sh * 60 + sm
    shift_end_minutes = eh * 60 + em
    
    # Check if overnight shift
    if shift_end_minutes <= shift_start_minutes:
        # If punch-in is before shift_end, it belongs to previous day's shift
        if ist_minutes < shift_end_minutes:
            # Move back one day
            ist_date = (ist_time - timedelta(days=1)).date()
        else:
            ist_date = ist_time.date()
    else:
        ist_date = ist_time.date()
    
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    Compute late minutes per R2.
    Grace period is 10 minutes. Late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = floor(minutes elapsed since shift_start).
    """
    ist_time = punch_in_utc.astimezone(IST)
    ist_hour = ist_time.hour
    ist_minute = ist_time.minute
    ist_seconds = ist_time.second
    ist_minutes = ist_hour * 60 + ist_minute + ist_seconds / 60
    
    sh, sm = map(int, shift_start.split(":"))
    shift_start_minutes = sh * 60 + sm
    
    elapsed = ist_minutes - shift_start_minutes
    
    # Grace period: only late if strictly more than 10 minutes
    if elapsed > 10:
        return int(elapsed)  # floor
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Compute work hours per R4.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(seconds / 3600)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, attendance_date: str) -> int:
    """
    Compute overtime minutes per R3.
    Overtime = floor(minutes between shift_end and punch_out), only when >= 30, otherwise 0.
    """
    ist_time = punch_out_utc.astimezone(IST)
    ist_hour = ist_time.hour
    ist_minute = ist_time.minute
    ist_seconds = ist_time.second
    ist_minutes = ist_hour * 60 + ist_minute + ist_seconds / 60
    
    eh, em = map(int, shift_end.split(":"))
    shift_end_minutes = eh * 60 + em
    
    # For overnight shifts, shift_end is on the next day
    # But punch_out is already in UTC, we just need to know if we crossed midnight
    # Actually, the comparison should be done in IST time space
    # For overnight shifts, if shift_end is 06:00 and punch_out is 06:40, that's 40 min overtime
    # If shift_end is 18:30 and punch_out is 19:10, that's 40 min overtime
    
    elapsed = ist_minutes - shift_end_minutes
    
    # If shift_end is after midnight (overnight shift), we need to handle the day boundary
    # But since we're comparing IST times, this should work naturally
    # For example: shift 22:00-06:00, punch_out at 06:40 IST
    # shift_end_minutes = 6*60 = 360, ist_minutes = 6*60 + 40 = 400, elapsed = 40
    
    # However, if shift_end is 18:30 and punch_out is 06:40 next day (overnight),
    # we need to account for the day boundary
    # Actually, punch_out should be on the same shift day or next day for overnight
    # The attendance_date is already computed, so we can trust the IST comparison
    
    if elapsed < 0:
        # This shouldn't happen with proper validation, but handle it
        elapsed = 0
    
    if elapsed >= 30:
        return int(elapsed)  # floor
    return 0


def compute_half_day(work_hours: float) -> bool:
    """
    Compute half_day per R5.
    True when rounded work_hours < 4.50.
    """
    return work_hours < 4.50


def recompute_derived_fields(punch_in_utc: Optional[datetime], 
                              punch_out_utc: Optional[datetime],
                              shift_start: str,
                              shift_end: str,
                              attendance_date: str,
                              status: str) -> dict:
    """
    Recompute all derived fields (late_minutes, work_hours, overtime_minutes, half_day).
    """
    if status in ["ABSENT", "LEAVE"]:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }
    
    if punch_in_utc is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }
    
    late_minutes = compute_late_minutes(punch_in_utc, shift_start)
    
    if punch_out_utc is None:
        return {
            "late_minutes": late_minutes,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }
    
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime_minutes(punch_out_utc, shift_end, attendance_date)
    half_day = compute_half_day(work_hours)
    
    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day
    }


def get_working_days_in_month(year: int, month: int, joined_on: Optional[str] = None) -> int:
    """
    Count working days (Mon-Fri) in a month, optionally from joined_on date (R7).
    """
    from calendar import monthrange
    days_in_month = monthrange(year, month)[1]
    
    start_day = 1
    if joined_on:
        jy, jm, jd = map(int, joined_on.split("-"))
        if jy == year and jm == month:
            start_day = jd
    
    count = 0
    for day in range(start_day, days_in_month + 1):
        dt = datetime(year, month, day)
        if dt.weekday() < 5:  # Monday=0, Friday=4
            count += 1
    
    return count


def round_half_up(value: float, decimals: int = 2) -> float:
    """Round half-up to specified decimals (R8)."""
    return float(Decimal(str(value)).quantize(Decimal(f'0.{"0" * decimals}'), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r'^EMP\d{4,6}$')
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field(default="18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    
    @field_validator('shift_end')
    @classmethod
    def shift_must_differ(cls, v, info):
        if 'shift_start' in info.data and v == info.data['shift_start']:
            raise ValueError('shift_start must differ from shift_end')
        return v


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r'^(PRESENT|WFH|ON_DUTY)$')


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r'^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$')
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# System endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Health check - returns 200 if MongoDB is reachable, 503 otherwise."""
    try:
        db.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB not reachable")


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeCreate):
    """Create an employee."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    
    doc["created_at"] = datetime_to_epoch(doc["created_at"])
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    """List employees, sorted by emp_code ascending."""
    q = {}
    if department:
        q["department"] = department
    
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    
    items = list(db.employees.find(q, {"_id": 0})
                 .sort("emp_code", ASCENDING)
                 .skip(skip)
                 .limit(page_size))
    
    for item in items:
        item["created_at"] = datetime_to_epoch(item["created_at"])
    
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInRequest):
    """Record a punch-in."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at else datetime.now(UTC)
    punched_at = truncate_to_seconds(punched_at)
    
    attendance_date = compute_attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    
    doc["punch_in"] = datetime_to_epoch(doc["punch_in"])
    return doc


@app.post("/attendance/punch-out", response_model=AttendanceRecord)
def punch_out(body: PunchOutRequest):
    """Record a punch-out."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at else datetime.now(UTC)
    punched_at = truncate_to_seconds(punched_at)
    
    # Find the most recent record with punch_in at or before punched_at
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punched_at},
        "punch_out": None
    }, sort=[("punch_in", DESCENDING)])
    
    if not record:
        raise HTTPException(404, "no punch-in found")
    
    if punched_at <= record["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")
    
    # Check if punch_out is more than 24 hours after punch_in
    if (punched_at - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    
    # Check if already punched out (concurrent punch-outs)
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    
    # Update with punch_out and compute derived fields
    derived = recompute_derived_fields(
        record["punch_in"],
        punched_at,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
        record["status"]
    )
    
    update_doc = {
        "$set": {
            "punch_out": punched_at,
            **derived
        }
    }
    
    # Use atomic update to handle concurrent punch-outs
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        update_doc
    )
    
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)
    updated["punch_in"] = datetime_to_epoch(updated["punch_in"])
    updated["punch_out"] = datetime_to_epoch(updated["punch_out"])
    for h in updated.get("history", []):
        h["at"] = datetime_to_epoch(h["at"])
        for change in h.get("changes", {}).values():
            if isinstance(change.get("from"), datetime):
                change["from"] = datetime_to_epoch(change["from"])
            if isinstance(change.get("to"), datetime):
                change["to"] = datetime_to_epoch(change["to"])
    
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    """List attendance records, sorted by date desc, emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    
    docs = list(db.attendance_logs.find(q, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip(skip)
                .limit(page_size))
    
    for doc in docs:
        if doc.get("punch_in"):
            doc["punch_in"] = datetime_to_epoch(doc["punch_in"])
        if doc.get("punch_out"):
            doc["punch_out"] = datetime_to_epoch(doc["punch_out"])
        for h in doc.get("history", []):
            h["at"] = datetime_to_epoch(h["at"])
            for change in h.get("changes", {}).values():
                if isinstance(change.get("from"), datetime):
                    change["from"] = datetime_to_epoch(change["from"])
                if isinstance(change.get("to"), datetime):
                    change["to"] = datetime_to_epoch(change["to"])
    
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Regularize (manually correct) one day's record."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")
    
    # Build changes for history
    changes = {}
    old_values = {}
    new_values = {}
    
    # Validate: setting ABSENT/LEAVE requires no punch times
    new_status = body.status if body.status is not None else record["status"]
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot set punch times with ABSENT or LEAVE status")
    
    # Validate: presence status requires punch_in
    if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        punch_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        if punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
    
    # Track changes
    if body.status is not None and body.status != record["status"]:
        changes["status"] = {"from": record["status"], "to": body.status}
        old_values["status"] = record["status"]
        new_values["status"] = body.status
    
    if body.punch_in is not None:
        new_punch_in = truncate_to_seconds(epoch_to_datetime(body.punch_in))
        if new_punch_in != record.get("punch_in"):
            changes["punch_in"] = {"from": datetime_to_epoch(record.get("punch_in")) if record.get("punch_in") else None, 
                                   "to": datetime_to_epoch(new_punch_in)}
            old_values["punch_in"] = record.get("punch_in")
            new_values["punch_in"] = new_punch_in
    
    if body.punch_out is not None:
        new_punch_out = truncate_to_seconds(epoch_to_datetime(body.punch_out))
        if new_punch_out != record.get("punch_out"):
            changes["punch_out"] = {"from": datetime_to_epoch(record.get("punch_out")) if record.get("punch_out") else None,
                                     "to": datetime_to_epoch(new_punch_out)}
            old_values["punch_out"] = record.get("punch_out")
            new_values["punch_out"] = new_punch_out
    
    if not changes:
        raise HTTPException(422, "no changes to apply")
    
    # Recompute derived fields
    final_punch_in = new_values.get("punch_in", record.get("punch_in"))
    final_punch_out = new_values.get("punch_out", record.get("punch_out"))
    final_status = new_values.get("status", record["status"])
    
    derived = recompute_derived_fields(
        final_punch_in,
        final_punch_out,
        emp["shift_start"],
        emp["shift_end"],
        date,
        final_status
    )
    
    # Check if derived fields changed
    for field in ["late_minutes", "work_hours", "overtime_minutes", "half_day"]:
        old_val = record.get(field)
        new_val = derived[field]
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}
    
    # Build update document
    update_fields = {}
    if "status" in changes:
        update_fields["status"] = changes["status"]["to"]
    if "punch_in" in changes:
        update_fields["punch_in"] = epoch_to_datetime(changes["punch_in"]["to"])
    if "punch_out" in changes:
        update_fields["punch_out"] = epoch_to_datetime(changes["punch_out"]["to"])
    update_fields.update(derived)
    
    # Append history entry
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    update_fields["history"] = record.get("history", []) + [history_entry]
    
    # Update record
    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": update_fields}
    )
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)
    updated["punch_in"] = datetime_to_epoch(updated["punch_in"]) if updated.get("punch_in") else None
    updated["punch_out"] = datetime_to_epoch(updated["punch_out"]) if updated.get("punch_out") else None
    for h in updated.get("history", []):
        h["at"] = datetime_to_epoch(h["at"])
        for change in h.get("changes", {}).values():
            if isinstance(change.get("from"), datetime):
                change["from"] = datetime_to_epoch(change["from"])
            if isinstance(change.get("to"), datetime):
                change["to"] = datetime_to_epoch(change["to"])
    
    return updated


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    
    year, month_num = map(int, month.split("-"))
    
    # Working days per R7
    working_days = get_working_days_in_month(year, month_num, emp["joined_on"])
    
    # Aggregation for attendance logs in this month
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
        }},
        {"$addFields": {
            "is_weekday": {
                "$cond": {
                    "if": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    "then": True,
                    "else": False
                }
            }
        }},
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$_id", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$eq": ["$is_weekday", True]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            }
        }}
    ]
    
    results = list(db.attendance_logs.aggregate(pipeline))
    
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    
    for r in results:
        if r["_id"] in ["PRESENT", "WFH", "ON_DUTY"]:
            present_days += r.get("present_days", 0)
            late_count += r.get("late_count", 0)
            total_late_minutes += r.get("total_late_minutes", 0)
            total_overtime_minutes += r.get("total_overtime_minutes", 0)
        elif r["_id"] == "LEAVE":
            leave_days += r["count"]
    
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    """Department summary for a month."""
    year, month_num = map(int, month.split("-"))
    month_start = f"{month}-01"
    month_end = f"{month}-31"
    
    # Build match for employees
    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department
    
    # Aggregation
    pipeline = [
        # Join employees with attendance logs
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$match": {
            "employee.joined_on": {"$lte": month_end},
            "date": {"$gte": month_start, "$lte": month_end}
        }},
        {"$addFields": {
            "is_weekday": {
                "$cond": {
                    "if": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    "then": True,
                    "else": False
                }
            },
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "has_work_hours": {"$ne": ["$work_hours", None]}
        }},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": ["$is_present", True]},
                            {"$eq": ["$is_weekday", True]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "total_work_hours": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": ["$is_present", True]},
                            {"$eq": ["$has_work_hours", True]}
                        ]},
                        "$work_hours",
                        0
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": ["$is_present", True]},
                            {"$eq": ["$has_work_hours", True]}
                        ]},
                        1,
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
            }
        }},
        # Get headcount (employees with joined_on <= month end)
        {"$lookup": {
            "from": "employees",
            "let": {"dept": "$_id"},
            "pipeline": [
                {"$match": {
                    "joined_on": {"$lte": month_end},
                    "$expr": {"$eq": ["$department", "$$dept"]}
                }},
                {"$count": "headcount"}
            ],
            "as": "headcount_info"
        }},
        {"$unwind": "$headcount_info"},
        {"$addFields": {
            "headcount": "$headcount_info.headcount",
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$total_work_hours", "$work_hours_count"]},
                    None
                ]
            }
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "_id": 0
        }},
        {"$sort": {"department": ASCENDING}}
    ]
    
    results = list(db.attendance_logs.aggregate(pipeline))
    
    # Filter out departments with headcount 0
    results = [r for r in results if r["headcount"] > 0]
    
    # Round avg_work_hours
    for r in results:
        if r["avg_work_hours"] is not None:
            r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2)
    
    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers leaderboard for a month."""
    month_start = f"{month}-01"
    month_end = f"{month}-31"
    
    # Match for employees
    emp_match = {}
    if department:
        emp_match["department"] = department
    
    # Aggregation
    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$match": emp_match if emp_match else {}},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            }
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
            "_id": 0
        }}
    ]
    
    results = list(db.attendance_logs.aggregate(pipeline))
    
    # Apply competition ranking (1, 2, 2, 4)
    ranked = []
    for i, r in enumerate(results):
        if i == 0:
            rank = 1
        else:
            prev = results[i - 1]
            if r["total_late_minutes"] == prev["total_late_minutes"]:
                rank = ranked[-1]["rank"]
            else:
                rank = i + 1
        ranked.append({**r, "rank": rank})
    
    # Filter by limit (after ranking)
    ranked = [r for r in ranked if r["rank"] <= limit]
    
    return {"month": month, "items": ranked}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., pattern=r'^\d{4}-\d{2}-\d{2}$')
):
    """Daily attendance trend for a department."""
    if from_date > to_date:
        raise HTTPException(422, "from must be <= to")
    
    # Check range doesn't exceed 92 days
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date)
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "range cannot exceed 92 days")
    
    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")
    
    # Generate all dates in range
    dates = []
    current = from_dt
    while current <= to_dt:
        dates.append(current.date().isoformat())
        current += timedelta(days=1)
    
    # Get attendance data
    pipeline = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            }
        }},
        {"$sort": {"_id": ASCENDING}}
    ]
    
    attendance_data = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}
    
    # Build trend items
    items = []
    for date_str in dates:
        dt = datetime.fromisoformat(date_str)
        is_working_day = dt.weekday() < 5
        
        # Headcount: employees with joined_on <= this date
        headcount = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": date_str}
        })
        
        # Get attendance data for this date
        att_data = attendance_data.get(date_str, {"present_count": 0, "late_count": 0})
        present_count = att_data["present_count"]
        late_count = att_data["late_count"]
        
        # Attendance rate
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up((present_count / headcount), 4)
        elif is_working_day and headcount == 0:
            attendance_rate = 0.0
        
        items.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None  # Will compute after
        })
    
    # Compute moving average (7-day window, within range)
    for i, item in enumerate(items):
        if not item["is_working_day"] or item["headcount"] == 0:
            continue
        
        # Get non-null rates in window (current + up to 6 preceding)
        window_start = max(0, i - 6)
        rates = []
        for j in range(window_start, i + 1):
            if items[j]["is_working_day"] and items[j]["headcount"] > 0 and items[j]["attendance_rate"] is not None:
                rates.append(items[j]["attendance_rate"])
        
        if rates:
            moving_avg = sum(rates) / len(rates)
            item["moving_avg_7d"] = round_half_up(moving_avg, 4)
    
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None
):
    """MongoDB explain for an endpoint's main query."""
    collection = None
    pipeline_or_query = None
    
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        
        page_val = page if page else 1
        page_size_val = page_size if page_size else 20
        skip = (page_val - 1) * page_size_val
        
        pipeline_or_query = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": skip},
            {"$limit": page_size_val}
        ]
    
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        collection = "attendance_logs"
        pipeline_or_query = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
            }},
            {"$addFields": {
                "is_weekday": {
                    "$cond": {
                        "if": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                        "then": True,
                        "else": False
                    }
                }
            }},
            {"$group": {
                "_id": "$status",
                "count": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$_id", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$eq": ["$is_weekday", True]}
                            ]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                }
            }}
        ]
    
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        collection = "attendance_logs"
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        
        emp_match = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        
        pipeline_or_query = [
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }},
            {"$unwind": "$employee"},
            {"$match": {
                "employee.joined_on": {"$lte": month_end},
                "date": {"$gte": month_start, "$lte": month_end}
            }},
            {"$addFields": {
                "is_weekday": {
                    "$cond": {
                        "if": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                        "then": True,
                        "else": False
                    }
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "has_work_hours": {"$ne": ["$work_hours", None]}
            }},
            {"$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": ["$is_present", True]},
                                {"$eq": ["$is_weekday", True]}
                            ]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": ["$is_present", True]},
                                {"$eq": ["$has_work_hours", True]}
                            ]},
                            "$work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": ["$is_present", True]},
                                {"$eq": ["$has_work_hours", True]}
                            ]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                }
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"dept": "$_id"},
                "pipeline": [
                    {"$match": {
                        "joined_on": {"$lte": month_end},
                        "$expr": {"$eq": ["$department", "$$dept"]}
                    }},
                    {"$count": "headcount"}
                ],
                "as": "headcount_info"
            }},
            {"$unwind": "$headcount_info"},
            {"$addFields": {
                "headcount": "$headcount_info.headcount",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$total_work_hours", "$work_hours_count"]},
                        None
                    ]
                }
            }},
            {"$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "_id": 0
            }},
            {"$sort": {"department": 1}}
        ]
    
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        collection = "attendance_logs"
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        
        emp_match = {}
        if department:
            emp_match["department"] = department
        
        limit_val = limit if limit else 10
        
        pipeline_or_query = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }},
            {"$unwind": "$employee"},
            {"$match": emp_match if emp_match else {}},
            {"$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
                "_id": 0
            }}
        ]
    
    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, "from and to required")
        collection = "attendance_logs"
        
        pipeline_or_query = [
            {"$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }},
            {"$sort": {"_id": 1}}
        ]
    
    else:
        raise HTTPException(422, "invalid endpoint")
    
    explain_result = db[collection].aggregate(pipeline_or_query).explain()
    
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_result
    }
