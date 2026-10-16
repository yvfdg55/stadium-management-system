"""Shared fixtures for isolated Flask integration tests."""
import os
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Force the import-time bootstrap DB into /tmp; never open the preview or user DB.
os.environ["SECRET_KEY"] = "qa-only-secret-key-never-use-outside-tests-2026"
os.environ["ADMIN_USERNAME"] = "qa-admin"
os.environ["ADMIN_PASSWORD"] = "QA-only-password-2026-do-not-reuse"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["APP_UTC_OFFSET_HOURS"] = "3"
os.environ["APP_ENV"] = "test"
os.environ["TRUSTED_HOSTS"] = "localhost"
os.environ["DATABASE_PATH"] = str(
    Path(tempfile.gettempdir()) / f"stadium-qa-bootstrap-{uuid.uuid4().hex}.sqlite3"
)

import pytest

import app as stadium

flask_app = stadium.app


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Create a fresh, per-test database using the real schema initializer."""
    database = tmp_path / "stadium-test.sqlite3"
    monkeypatch.setitem(flask_app.config, "DATABASE", str(database))
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "SESSION_COOKIE_SECURE", False)
    with flask_app.app_context():
        stadium.init_db()
    return database


@pytest.fixture
def client(fresh_db):
    return flask_app.test_client()


def add_client(name="عميل اختبار", phone="0790000000", notes="ملاحظة"):
    with flask_app.app_context():
        db = stadium.get_db()
        cursor = db.execute(
            "INSERT INTO clients (name, phone, notes) VALUES (?, ?, ?)",
            (name, phone, notes),
        )
        db.commit()
        return cursor.lastrowid


def insert_booking(client_id, booking_date="2025-01-06", start_time="14:00",
                   duration=1, hourly_rate=100, total=None, status="active",
                   booking_type="single", weekly_rule_id=None, notes="private note"):
    total = hourly_rate * duration if total is None else total
    end_minutes = (int(start_time[:2]) * 60 + int(start_time[3:])) + duration * 60
    end_time = f"{(end_minutes // 60) % 24:02d}:{end_minutes % 60:02d}"
    with flask_app.app_context():
        db = stadium.get_db()
        cursor = db.execute(
            """INSERT INTO bookings
               (client_id, booking_date, start_time, end_time, hourly_rate,
                duration_hours, total, notes, status, booking_type, weekly_rule_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, booking_date, start_time, end_time, hourly_rate,
             duration, total, notes, status, booking_type, weekly_rule_id),
        )
        db.commit()
        return cursor.lastrowid


def db_query(sql, params=()):
    with flask_app.app_context():
        rows = stadium.get_db().execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def csrf_token(client):
    with client.session_transaction() as session:
        token = session.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
    return token


def post_form(client, path, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("_csrf_token", csrf_token(client))
    return client.post(path, data=payload, **kwargs)


def login_as_admin(client):
    """Install a valid test session while keeping all requests on real auth code."""
    client.get("/login")
    admins = db_query("SELECT id, username, session_version FROM admins ORDER BY id LIMIT 1")
    with client.session_transaction() as session:
        session["admin_id"] = admins[0]["id"]
        session["admin_username"] = admins[0]["username"]
        session["session_version"] = admins[0]["session_version"]
    return client


def booking_payload(client_id, **overrides):
    payload = {
        "client_id": str(client_id),
        "booking_date": "2025-01-07",
        "start_time": "14:00",
        "hourly_rate": "100",
        "duration_hours": "1",
        "initial_payment": "0",
        "notes": "اختبار",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload


def recurring_payload(client_id, **overrides):
    payload = {
        "client_id": str(client_id),
        "weekday": "0",
        "start_time": "14:00",
        "duration_hours": "1",
        "hourly_rate": "100",
        "deposit": "0",
        "notes": "قاعدة اختبار",
    }
    payload.update({key: str(value) for key, value in overrides.items()})
    return payload
