from fastapi.testclient import TestClient
from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_list_employees_endpoint():
    with TestClient(app) as client:
        response = client.get("/employees?page=1&page_size=20")

    assert response.status_code == 200

    data = response.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


def test_employee_pagination_validation():
    with TestClient(app) as client:
        response = client.get("/employees?page=0&page_size=20")

    assert response.status_code == 422


def test_employee_page_size_validation():
    with TestClient(app) as client:
        response = client.get("/employees?page=1&page_size=101")

    assert response.status_code == 422
from uuid import uuid4


def test_create_employee():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"

    payload = {
        "emp_code": emp_code,
        "name": "Automated Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    with TestClient(app) as client:
        response = client.post("/employees", json=payload)

        assert response.status_code == 201
        assert response.json()["emp_code"] == emp_code

        duplicate_response = client.post("/employees", json=payload)
        assert duplicate_response.status_code == 409
def test_duplicate_punch_in():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"

    employee = {
        "emp_code": emp_code,
        "name": "Attendance Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        punch_payload = {
            "emp_code": emp_code,
            "status": "PRESENT"
        }

        first_response = client.post(
            "/attendance/punch-in",
            json=punch_payload
        )
        assert first_response.status_code == 201

        second_response = client.post(
            "/attendance/punch-in",
            json=punch_payload
        )
        assert second_response.status_code == 409
def test_punch_in_rejects_invalid_timestamp():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"

    employee = {
        "emp_code": emp_code,
        "name": "Timestamp Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        response = client.post(
            "/attendance/punch-in",
            json={
                "emp_code": emp_code,
                "status": "PRESENT",
                "punched_at": 123.5
            }
        )

        assert response.status_code == 422
    
from datetime import datetime, timezone
from app.main import compute_late_minutes


def test_overnight_shift_late_calculation():
    punch_in = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)

    result = compute_late_minutes(
        punch_in,
        "22:00",
        "06:00",
    )

    assert result == 0


def test_overnight_shift_late_after_midnight():
    punch_in = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)

    result = compute_late_minutes(
        punch_in,
        "22:00",
        "06:00",
    )

    assert result == 150


def test_overnight_shift_late_more_than_grace_period():
    punch_in = datetime(2026, 10, 10, 16, 40, 1, tzinfo=timezone.utc)

    result = compute_late_minutes(
        punch_in,
        "22:00",
        "06:00",
    )

    assert result == 10


def test_arrival_exactly_at_shift_start():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)

    assert compute_late_minutes(punch_in, "09:30") == 0


def test_arrival_exactly_at_grace_period():
    punch_in = datetime(2026, 10, 10, 4, 10, 0, tzinfo=timezone.utc)

    assert compute_late_minutes(punch_in, "09:30") == 0


def test_arrival_one_second_after_grace_period():
    punch_in = datetime(2026, 10, 10, 4, 10, 1, tzinfo=timezone.utc)

    assert compute_late_minutes(punch_in, "09:30") == 10


def test_arrival_eleven_minutes_late():
    punch_in = datetime(2026, 10, 10, 4, 41, 0, tzinfo=timezone.utc)

    assert compute_late_minutes(punch_in, "09:30") == 41


from datetime import date
from app.main import compute_overtime

def test_overtime_below_30_minutes():
    punch_out = datetime(2026, 10, 10, 12, 45, 0, tzinfo=timezone.utc)

    assert compute_overtime(
        punch_out, "18:00", "2026-10-10", "09:30"
    ) == 0


def test_overtime_exactly_30_minutes():
    punch_out = datetime(2026, 10, 10, 13, 0, 0, tzinfo=timezone.utc)

    assert compute_overtime(
        punch_out, "18:00", "2026-10-10", "09:30"
    ) == 30


def test_overtime_rounds_down_to_whole_minutes():
    punch_out = datetime(2026, 10, 10, 13, 0, 59, tzinfo=timezone.utc)

    assert compute_overtime(
        punch_out, "18:00", "2026-10-10", "09:30"
    ) == 30


def test_overtime_45_minutes():
    punch_out = datetime(2026, 10, 10, 13, 15, 0, tzinfo=timezone.utc)

    assert compute_overtime(
        punch_out, "18:00", "2026-10-10", "09:30"
    ) == 45

from app.main import compute_work_hours


def test_work_hours_rounding():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 10, 8, 15, 0, tzinfo=timezone.utc)

    assert compute_work_hours(punch_in, punch_out) == 4.25


def test_work_hours_half_up_rounding():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 10, 8, 27, 0, tzinfo=timezone.utc)

    assert compute_work_hours(punch_in, punch_out) == 4.45


def test_work_hours_rounds_to_450():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 10, 8, 30, 0, tzinfo=timezone.utc)

    assert compute_work_hours(punch_in, punch_out) == 4.5

from app.main import compute_derived_fields


def test_half_day_when_work_hours_below_450():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 10, 8, 29, 0, tzinfo=timezone.utc)

    employee = {
        "shift_start": "09:30",
        "shift_end": "18:00",
    }

    result = compute_derived_fields(
        punch_in,
        punch_out,
        "PRESENT",
        employee,
        "2026-10-10",
    )

    assert result["work_hours"] == 4.48
    assert result["half_day"] is True


def test_half_day_when_work_hours_equal_450():
    punch_in = datetime(2026, 10, 10, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 10, 10, 8, 30, 0, tzinfo=timezone.utc)

    employee = {
        "shift_start": "09:30",
        "shift_end": "18:00",
    }

    result = compute_derived_fields(
        punch_in,
        punch_out,
        "PRESENT",
        employee,
        "2026-10-10",
    )

    assert result["work_hours"] == 4.5
    assert result["half_day"] is False
def test_monthly_analytics_for_employee():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"

    employee = {
        "emp_code": emp_code,
        "name": "Monthly Analytics Test",
        "email": f"{emp_code.lower()}@example.com",
        "department": "AnalyticsTesting",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-10-01",
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        punch_response = client.post(
            "/attendance/punch-in",
            json={
                "emp_code": emp_code,
                "punched_at": 1790875800000,
                "status": "PRESENT",
            },
        )
        assert punch_response.status_code == 201

        response = client.get(
            f"/analytics/employees/{emp_code}/monthly?month=2026-10"
        )

        assert response.status_code == 200

        data = response.json()

        assert data["emp_code"] == emp_code
        assert data["month"] == "2026-10"
        assert data["working_days"] == 22
        assert data["present_days"] == 1
        assert data["leave_days"] == 0
        assert data["attendance_pct"] is not None


def test_monthly_analytics_unknown_employee():
    emp_code = "EMP999999"

    with TestClient(app) as client:
        response = client.get(
            f"/analytics/employees/{emp_code}/monthly?month=2026-10"
        )

    assert response.status_code == 404


def test_monthly_analytics_invalid_month():
    with TestClient(app) as client:
        response = client.get(
            "/analytics/employees/EMP999999/monthly?month=2026-13"
        )

    assert response.status_code == 422

def test_department_summary_endpoint():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"

    employee = {
        "emp_code": emp_code,
        "name": "Department Summary Test",
        "email": f"{emp_code.lower()}@example.com",
        "department": "SummaryTesting",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        response = client.get(
            "/analytics/departments/summary"
            "?month=2026-10&department=SummaryTesting"
        )

        assert response.status_code == 200

        data = response.json()
        assert data["month"] == "2026-10"
        assert len(data["items"]) == 1

        summary = data["items"][0]
        assert summary["department"] == "SummaryTesting"
        assert summary["headcount"] == 1
        assert summary["present_days"] == 0
        assert summary["avg_work_hours"] is None

def test_department_summary_endpoint():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"
    department = f"SummaryTesting{uuid4().hex[:8]}"

    employee = {
        "emp_code": emp_code,
        "name": "Department Summary Test",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        response = client.get(
            f"/analytics/departments/summary"
            f"?month=2026-10&department={department}"
        )

        assert response.status_code == 200

        data = response.json()
        assert data["month"] == "2026-10"
        assert len(data["items"]) == 1

        summary = data["items"][0]
        assert summary["department"] == department
        assert summary["headcount"] == 1
        assert summary["present_days"] == 0
        assert summary["avg_work_hours"] is None


def test_late_leaderboard_endpoint():
    emp_code = f"EMP{uuid4().int % 1000000:06d}"
    department = f"LeaderboardTesting{uuid4().hex[:8]}"

    employee = {
        "emp_code": emp_code,
        "name": "Leaderboard Test",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }

    with TestClient(app) as client:
        create_response = client.post("/employees", json=employee)
        assert create_response.status_code == 201

        punch_response = client.post(
            "/attendance/punch-in",
            json={
                "emp_code": emp_code,
                "punched_at": 1790919600000,
                "status": "PRESENT",
            },
        )
        assert punch_response.status_code == 201

        response = client.get(
            f"/analytics/leaderboard/late"
            f"?month=2026-10&limit=10&department={department}"
        )

        assert response.status_code == 200

        data = response.json()
        assert data["month"] == "2026-10"
        assert isinstance(data["items"], list)
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["emp_code"] == emp_code
        assert item["department"] == department
        assert item["total_late_minutes"] > 0
        assert item["rank"] == 1