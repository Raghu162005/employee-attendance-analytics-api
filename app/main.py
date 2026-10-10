import os
import json
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from pydantic import StrictInt

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.json_util import CANONICAL_JSON_OPTIONS


load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# ---------------------------------------------------------------------------
# Database startup
# ---------------------------------------------------------------------------

def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )

    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punch",
    )

    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="ix_attendance_date_status",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def from_epoch_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Timestamp must be an integer in milliseconds")

    if not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise ValueError("Timestamp is outside the permitted range")

    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def round_half_up(value, places=2):
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, "Invalid date; expected YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month; expected YYYY-MM")

    last_day = calendar.monthrange(parsed.year, parsed.month)[1]
    return parsed.replace(day=1), parsed.replace(day=last_day)


def validate_date_range(date_from, date_to):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def count_working_days(start: date, end: date) -> int:
    if start > end:
        return 0

    count = 0
    current = start

    while current <= end:
        if is_working_day(current):
            count += 1
        current += timedelta(days=1)

    return count


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = as_utc(punch_in).astimezone(IST)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]

    if (
        shift_end <= shift_start
        and local.strftime("%H:%M") < shift_end
    ):
        local -= timedelta(days=1)

    return local.date().isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    local = as_utc(punch_in).astimezone(IST)
    hour, minute = map(int, shift_start.split(":"))

    start = local.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    # For an overnight shift, a punch-in before shift_end belongs to the
    # shift that started on the previous calendar day. Compare it with that
    # day's shift_start rather than shift_start on the punch-in date.
    if (
        shift_end is not None
        and shift_end <= shift_start
        and local.strftime("%H:%M") < shift_end
    ):
        start -= timedelta(days=1)

    elapsed = (local - start).total_seconds()

    # The 10-minute grace period is inclusive. Once exceeded, count whole
    # elapsed minutes from shift_start, not from the end of the grace period.
    if elapsed <= 600:
        return 0

    return int(elapsed // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (
        as_utc(punch_out) - as_utc(punch_in)
    ).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_day: str,
    shift_start: str,
) -> int:
    """Return whole overtime minutes, but only when overtime reaches 30 minutes.

    ``attendance_day`` is the shift's R1 attendance date in IST. For an
    overnight shift, the scheduled shift end falls on the following calendar
    day. Timestamps are normalized to UTC and whole-second precision before
    comparison.
    """
    day = parse_date(attendance_day)
    hour, minute = map(int, shift_end.split(":"))

    if shift_end <= shift_start:
        day += timedelta(days=1)

    scheduled_end_ist = datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    punch_out_utc = truncate_seconds(as_utc(punch_out))
    scheduled_end_utc = scheduled_end_ist.astimezone(UTC)
    elapsed_seconds = int(
        (punch_out_utc - scheduled_end_utc).total_seconds()
    )

    # Do not count overtime unless at least 30 complete minutes have elapsed.
    if elapsed_seconds < 30 * 60:
        return 0

    # Floor to complete minutes; e.g. 30m59s becomes 30 minutes.
    return elapsed_seconds // 60


def compute_derived_fields(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    status: str,
    employee: dict,
    attendance_day: str,
) -> dict:
    if status not in PRESENCE_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = (
        compute_late_minutes(
            punch_in, employee["shift_start"], employee["shift_end"]
        )
        if punch_in
        else 0
    )

    if punch_in is None or punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }

    hours = compute_work_hours(punch_in, punch_out)

    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(
            punch_out,
            employee["shift_end"],
            attendance_day,
            employee["shift_start"],
        ),
        "half_day": hours < 4.50,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": {
                    field: {
                        "from": (
                            to_epoch_ms(values["from"])
                            if field in {"punch_in", "punch_out"}
                            else values["from"]
                        ),
                        "to": (
                            to_epoch_ms(values["to"])
                            if field in {"punch_in", "punch_out"}
                            else values["to"]
                        ),
                    }
                    for field, values in entry["changes"].items()
                },
            }
            for entry in doc.get("history", [])
        ],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def month_filter(start: date, end: date):
    return {
        "$gte": start.isoformat(),
        "$lte": end.isoformat(),
    }


def month_params(month: str):
    return parse_month(month)


def json_safe(value):
    """Convert BSON values to JSON-compatible Extended JSON structures.

    Do not use json_util.loads() here: it reconstructs BSON objects such as
    Timestamp, which FastAPI's response encoder cannot serialize.
    """
    return json.loads(
        json_util.dumps(value, json_options=CANONICAL_JSON_OPTIONS)
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: date

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            from_epoch_ms(value)
        return value


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(mode="json")
    doc["created_at"] = truncate_seconds(datetime.now(UTC))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = db.employees.find(
        query,
        {"_id": 0},
    ).sort(
        "emp_code", ASCENDING
    ).skip(
        (page - 1) * page_size
    ).limit(page_size)

    return {
        "items": [serialize_employee(doc) for doc in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if employee is None:
        raise HTTPException(404, "Employee not found")

    timestamp = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(datetime.now(UTC))
    )

    day = attendance_date(timestamp, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            timestamp,
            employee["shift_start"],
            employee["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Attendance already exists for this employee and date",
        )

    return serialize_attendance(doc)


# ---------------------------------------------------------------------------
# Attendance punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if employee is None:
        raise HTTPException(404, "Employee not found")

    timestamp = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(datetime.now(UTC))
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": timestamp},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = record["punch_in"]

    if timestamp <= punch_in_time:
        raise HTTPException(422, "Punch-out must be after punch-in")

    if timestamp > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "Punch-out cannot be more than 24 hours after punch-in")

    derived = compute_derived_fields(
        punch_in_time,
        timestamp,
        record["status"],
        employee,
        record["date"],
    )

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": timestamp,
                **derived,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance was already updated")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Attendance list
# ---------------------------------------------------------------------------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_date_range(date_from, date_to)

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    cursor = db.attendance_logs.find(query).sort(
        [
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ]
    ).skip(
        (page - 1) * page_size
    ).limit(page_size)

    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance regularization
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    parse_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if record is None:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set

    new_status = (
        body.status
        if "status" in supplied
        else record["status"]
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot have supplied punch times",
            )

        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            from_epoch_ms(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else record.get("punch_in")
        )

        new_punch_out = (
            from_epoch_ms(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else record.get("punch_out")
        )

        if new_punch_in is None:
            raise HTTPException(
                422,
                "Presence statuses require a punch-in",
            )

        if attendance_date(new_punch_in, employee) != date:
            raise HTTPException(
                422,
                "Punch-in does not belong to this attendance date",
            )

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    422,
                    "Punch-out must be after punch-in",
                )

            if new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(
                    422,
                    "Punch-out cannot be more than 24 hours after punch-in",
                )

    derived = compute_derived_fields(
        new_punch_in,
        new_punch_out,
        new_status,
        employee,
        date,
    )

    proposed = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        **derived,
    }

    old_values = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    changes = {}

    for field, new_value in proposed.items():
        old_value = old_values.get(field)

        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "Request does not change the record")

    history_entry = {
        "at": truncate_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    update_result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
    )

    if update_result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance changed concurrently; retry the request",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    start, end = month_params(month)

    effective_start = max(
        start,
        parse_date(employee["joined_on"]),
    )

    working_days = count_working_days(effective_start, end)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": month_filter(start, end),
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = round_half_up(stats.get("present_days", 0))
    attendance_pct = (
        round_half_up(present_days / working_days * 100)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department monthly summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start, end = month_params(month)

    employee_match = {
        "joined_on": {"$lte": end.isoformat()}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = []

    for row in db.employees.aggregate(pipeline):
        values = [v for v in row.pop("hours", []) if v is not None]

        results.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"]),
            "avg_work_hours": (
                round_half_up(sum(values) / len(values))
                if values
                else None
            ),
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": results}


# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_params(month)

    pipeline = [
        {
            "$match": {
                "date": month_filter(start, end),
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
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
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

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
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
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


# ---------------------------------------------------------------------------
# Department trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)

    if end < start:
        raise HTTPException(422, "'to' cannot be before 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_exclusive_dt = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )

    # Start with a seed date so $densify also returns a complete date range
    # when the department has no attendance logs at all.
    pipeline = [
        {
            "$documents": [
                {
                    "date": start_dt,
                    "present_count": 0,
                    "late_count": 0,
                }
            ]
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": start.isoformat(),
                                "$lte": end.isoformat(),
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": {
                                "$dateFromString": {"dateString": "$_id"}
                            },
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive_dt],
                },
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "day_text": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_text": "$day_text"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_text"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.headcount", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day_text",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "attendance_rate": {"$round": ["$attendance_rate", 4]},
                "moving_avg_7d": {"$round": ["$moving_avg_raw", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    try:
        items = list(db.aggregate(pipeline))
    except PyMongoError as exc:
        # Keep database failures from becoming an unhandled 500 response.
        raise HTTPException(503, "Unable to calculate department trend") from exc

    return {"department": department, "items": json_safe(items)}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        validate_date_range(date_from, date_to)

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            if status not in ALL_STATUSES:
                raise HTTPException(422, "Invalid status")
            query["status"] = status

        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_safe(explain),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required",
            )

        start, end = month_params(month)

        pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": month_filter(start, end),
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_safe(explain),
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_params(month)
        employee_match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None:
            employee_match["department"] = department
        pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json_safe(explain),
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_params(month)
        pipeline = [
        {
            "$match": {
                "date": month_filter(start, end),
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
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
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
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {
                "_id": 0, "rank": 1, "emp_code": "$_id",
                "name": "$employee.name", "department": "$employee.department",
                "total_late_minutes": 1, "late_count": 1,
            }},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_safe(explain),
        }

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date)
        end = parse_date(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")
        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "Department not found")
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        end_exclusive_dt = datetime.combine(
            end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        pipeline = [
        {
            "$documents": [
                {
                    "date": start_dt,
                    "present_count": 0,
                    "late_count": 0,
                }
            ]
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": start.isoformat(),
                                "$lte": end.isoformat(),
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": {
                                "$dateFromString": {"dateString": "$_id"}
                            },
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive_dt],
                },
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "day_text": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_text": "$day_text"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_text"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.headcount", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day_text",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "attendance_rate": {"$round": ["$attendance_rate", 4]},
                "moving_avg_7d": {"$round": ["$moving_avg_raw", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]
        # Atlas requires a collection name for aggregate explain commands.
        # The complete department-trend pipeline begins with $documents, which
        # cannot be explained through this collection-based command form.
        # Explain the index-relevant attendance sub-pipeline separately.
        trend_pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": end.isoformat(),
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                {
                                    "$cond": [
                                        {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": trend_pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain_scope": "attendance filtering, employee lookup, department filtering, and daily grouping; excludes the $documents/$densify/headcount/moving-average stages",
            "explain": json_safe(explain),
        }

    raise HTTPException(422, "Unsupported endpoint")