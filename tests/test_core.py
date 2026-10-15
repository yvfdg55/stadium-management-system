import sqlite3
from datetime import datetime, timedelta

import pytest
from conftest import db_query, login_as_admin, post_form

import app as stadium


@pytest.mark.parametrize("minute", range(1440))
def test_clock_roundtrip_every_minute(minute):
    value = stadium.format_clock(minute)
    assert value == f"{minute // 60:02d}:{minute % 60:02d}"
    assert stadium.clock_minutes(value) == minute


@pytest.mark.parametrize("minute", range(1440))
def test_arabic_time_every_minute(minute):
    hour, mins = divmod(minute, 60)
    assert stadium.format_arabic_time(f"{hour:02d}:{mins:02d}") == f"{hour % 12 or 12}:{mins:02d} {'ص' if hour < 12 else 'م'}"


@pytest.mark.parametrize("start", range(24))
@pytest.mark.parametrize("end", range(24))
def test_work_window_all_hour_pairs(start, end):
    settings = {"work_start": f"{start:02d}:00", "work_end": f"{end:02d}:00"}
    assert stadium.work_window(settings) == (start * 60, ((end - start) % 24 or 24) * 60)


@pytest.mark.parametrize("start", range(24))
@pytest.mark.parametrize("hour", range(24))
def test_cycle_offset_all_hour_pairs(start, hour):
    assert stadium.cycle_offset(f"{hour:02d}:00", start * 60) == ((hour - start) % 24) * 60


@pytest.mark.parametrize("start", range(24))
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_business_date_hour_boundary(start, delta):
    settings = {"work_start": f"{start:02d}:00", "work_end": f"{(start + 12) % 24:02d}:00"}
    moment = datetime(2025, 4, 10, start, 0) + timedelta(minutes=delta)
    expected = moment.date() - timedelta(days=1) if moment.hour * 60 + moment.minute < start * 60 else moment.date()
    assert stadium.stadium_business_date(moment, settings) == expected


@pytest.mark.parametrize("value", [None, "", "bad", "24:00", "12:60", "2025-01-01"])
def test_invalid_arabic_time_is_preserved(value):
    assert stadium.format_arabic_time(value) == (value or "")


@pytest.mark.parametrize("value,expected", [("#000000", "#000000"), ("#FFFFFF", "#FFFFFF"), ("#a1B2c3", "#a1B2c3"), ("red", stadium.DEFAULT_SETTINGS["primary_color"]), ("#123", stadium.DEFAULT_SETTINGS["primary_color"]), (None, stadium.DEFAULT_SETTINGS["primary_color"]), ("#12345678", stadium.DEFAULT_SETTINGS["primary_color"])])
def test_theme_color_validation(value, expected):
    assert stadium.safe_theme_color({"primary_color": value}, "primary_color") == expected


def test_init_db_seeds_full_schema_defaults_and_admin(fresh_db):
    settings = {r["setting_key"]: r["setting_value"] for r in db_query("SELECT setting_key, setting_value FROM app_settings")}
    assert settings == stadium.DEFAULT_SETTINGS
    assert db_query("SELECT username, session_version FROM admins") == [{"username": "qa-admin", "session_version": 0}]
    tables = {r["name"] for r in db_query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"admins", "login_attempts", "clients", "bookings", "booking_payments", "recurring_booking_rules", "recurring_deposit_transactions", "expenses", "other_income", "app_settings"} <= tables


def test_theme_css_uses_safe_values(client):
    with stadium.app.app_context():
        db = stadium.get_db()
        db.execute("UPDATE app_settings SET setting_value='#123456' WHERE setting_key='primary_color'")
        db.execute("UPDATE app_settings SET setting_value='bad' WHERE setting_key='secondary_color'")
        db.commit()
    response = client.get("/theme.css")
    body = response.get_data(as_text=True)
    assert response.status_code == 200 and "--blue: #123456" in body
    assert f"--blue-dark: {stadium.DEFAULT_SETTINGS['secondary_color']}" in body


def test_root_and_admin_authentication(client):
    assert client.get("/").headers["Location"].endswith("/login")
    for path in ["/admin", "/admin/account", "/admin/settings", "/admin/reports", "/admin/booking", "/admin/booking/hours", "/admin/bookings/new", "/admin/recurring-bookings/new", "/admin/expenses", "/admin/income", "/admin/clients", "/admin/clients/new"]:
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/login")


def test_security_headers_and_csrf(client):
    response = client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/logout", data={}).status_code == 400
    assert post_form(client, "/logout", {"notes": "x" * 10001}).status_code == 413


def test_login_success_failures_and_throttle(client):
    client.get("/login")
    bad = post_form(client, "/login", {"username": "qa-admin", "password": "wrong"})
    assert bad.status_code == 200 and db_query("SELECT failures FROM login_attempts")[0]["failures"] == 1
    good = post_form(client, "/login", {"username": "qa-admin", "password": "QA-only-password-2026-do-not-reuse"})
    assert good.status_code == 302 and good.headers["Location"].endswith("/admin")
    assert db_query("SELECT * FROM login_attempts") == []


def test_login_locks_after_five_bad_passwords(client):
    client.get("/login")
    for _ in range(5):
        assert post_form(client, "/login", {"username": "qa-admin", "password": "wrong"}).status_code == 200
    assert post_form(client, "/login", {"username": "qa-admin", "password": "QA-only-password-2026-do-not-reuse"}).status_code == 429


def test_session_version_revokes_stale_session(client):
    login_as_admin(client)
    with stadium.app.app_context():
        db = stadium.get_db(); db.execute("UPDATE admins SET session_version=session_version+1"); db.commit()
    assert client.get("/admin").status_code == 302


def test_manifests_worker_and_icons_are_available(client):
    worker = client.get("/service-worker.js")
    assert worker.status_code == 200 and worker.headers["Service-Worker-Allowed"] == "/"
    assert "static-v12" in worker.get_data(as_text=True)
    for path in ["/static/manifest.webmanifest", "/static/customer-manifest.webmanifest"]:
        response = client.get(path)
        assert response.status_code == 200 and "Happy Family Resort" in response.get_data(as_text=True)
    source = worker.get_data(as_text=True)
    assert "CACHEABLE_PATHS.has(url.pathname)" in source and "'/public-schedule'" not in source and "'/admin'" not in source


def test_settings_validation_and_persistence(client):
    login_as_admin(client)
    valid = {"venue_name": "منتجع", "venue_phone": "", "venue_address": "", "venue_policy": "سياسة", "hourly_rate": "100", "work_start": "10:00", "work_end": "22:00", "booking_duration": "1", "primary_color": "#123456", "secondary_color": "#654321"}
    for change in [{"venue_name": ""}, {"hourly_rate": "x"}, {"hourly_rate": "-1"}, {"booking_duration": "0"}, {"booking_duration": "25"}, {"work_start": "10:30"}, {"work_end": "22:15"}, {"primary_color": "red"}, {"venue_policy": "x" * 8001}]:
        assert post_form(client, "/admin/settings", {**valid, **change}).status_code == 400
    assert post_form(client, "/admin/settings", valid).status_code == 302
    saved = {r["setting_key"]: r["setting_value"] for r in db_query("SELECT setting_key, setting_value FROM app_settings")}
    assert saved["venue_policy"] == "سياسة" and saved["primary_color"] == "#123456"


def test_password_change_revokes_other_sessions_and_matches_minimum(client):
    login_as_admin(client)
    other = stadium.app.test_client(); login_as_admin(other)
    response = post_form(client, "/admin/account", {"username": "qa-admin", "current_password": "QA-only-password-2026-do-not-reuse", "new_password": "New-QA-password-2026", "confirm_password": "New-QA-password-2026"})
    assert response.status_code == 302 and client.get("/admin/account").status_code == 200
    assert other.get("/admin").status_code == 302
    assert 'minlength="12"' in client.get("/admin/account").get_data(as_text=True)


def test_local_time_formatter():
    assert stadium.format_local_timestamp("2025-01-01 22:30:00") == "2025-01-02 01:30"
    assert stadium.format_local_timestamp("not-a-date") == "not-a-date"


def test_legacy_booking_migration_preserves_valid_rows_and_payments(tmp_path, monkeypatch):
    database = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, notes TEXT DEFAULT '')")
    db.execute("INSERT INTO clients VALUES (1, 'قديم', '0791', '')")
    db.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY, client_id INTEGER, booking_date TEXT, booking_time TEXT, total_amount INTEGER, paid_amount INTEGER, notes TEXT, status TEXT, created_at TEXT)")
    db.execute("INSERT INTO bookings VALUES (1,1,'2025-01-01','14:00 - 16:00',200,50,'ملاحظة','نشط','2024-12-01 10:00:00')")
    db.execute("INSERT INTO bookings VALUES (2,1,'2025-01-02','20:00 - 21:00',100,0,'','ملغى','2024-12-02 10:00:00')")
    db.execute("INSERT INTO bookings VALUES (3,1,'2025-01-03','غير صالح',100,0,'','نشط','2024-12-03 10:00:00')")
    db.commit(); db.close(); monkeypatch.setitem(stadium.app.config, "DATABASE", str(database))
    with stadium.app.app_context():
        stadium.init_db()
        bookings = [dict(r) for r in stadium.get_db().execute("SELECT id,start_time,end_time,total,status FROM bookings ORDER BY id")]
        payments = [dict(r) for r in stadium.get_db().execute("SELECT booking_id,amount FROM booking_payments")]
    assert bookings == [{"id": 1, "start_time": "14:00", "end_time": "16:00", "total": 200, "status": "active"}, {"id": 2, "start_time": "20:00", "end_time": "21:00", "total": 100, "status": "cancelled"}]
    assert payments == [{"booking_id": 1, "amount": 50}]


def test_legacy_brand_migration_preserves_custom_values(tmp_path, monkeypatch):
    database = tmp_path / "old-brand.sqlite3"; db = sqlite3.connect(database)
    db.execute("CREATE TABLE app_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
    db.execute("INSERT INTO app_settings VALUES ('venue_name','My custom venue')")
    db.execute("INSERT INTO app_settings VALUES ('primary_color','#2563eb')")
    db.execute("INSERT INTO app_settings VALUES ('secondary_color','#ff00aa')")
    db.commit(); db.close(); monkeypatch.setitem(stadium.app.config, "DATABASE", str(database))
    with stadium.app.app_context():
        stadium.init_db(); values = {r["setting_key"]: r["setting_value"] for r in stadium.get_db().execute("SELECT setting_key,setting_value FROM app_settings")}
    assert values["venue_name"] == "My custom venue"
    assert values["primary_color"] == stadium.DEFAULT_SETTINGS["primary_color"]
    assert values["secondary_color"] == "#ff00aa"
