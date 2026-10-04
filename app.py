import calendar
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)
if ENV_PATH.exists():
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
APP_TIMEZONE = timezone(timedelta(hours=int(os.getenv("APP_UTC_OFFSET_HOURS", "3"))))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "instance" / "stadium.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY.startswith("replace-"):
    raise RuntimeError("Set SECRET_KEY to a unique random value of at least 32 characters.")
HOURLY_RATE = int(os.getenv("HOURLY_RATE", "10000"))
DEFAULT_SETTINGS = {
    "venue_name": "الملعب",
    "venue_phone": "",
    "venue_address": "",
    "hourly_rate": str(HOURLY_RATE),
    "work_start": "10:00",
    "work_end": "10:00",
    "booking_duration": "1",
    "primary_color": "#2563eb",
    "secondary_color": "#1d4ed8",
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    DATABASE=str(DATABASE_PATH),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"},
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=1024 * 1024,
    TRUSTED_HOSTS=[host.strip() for host in os.getenv("TRUSTED_HOSTS", "").split(",") if host.strip()] or None,
)
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
if TRUSTED_PROXY_HOPS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_state_changing_requests():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="Invalid CSRF token")
        if any(len(value) > 10000 for _key, values in request.form.lists() for value in values):
            abort(413, description="Form field is too long")


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def app_now():
    """Return the current app-local wall time as a naive datetime."""
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


class BookingConflict(ValueError):
    """Raised when a booking overlaps another active booking."""


class PaymentError(ValueError):
    """Raised when a payment cannot be recorded for a booking."""


def get_db():
    if "db" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(app.config["DATABASE"])
        try:
            os.chmod(DATABASE_PATH, 0o600)
        except OSError:
            app.logger.warning("Could not restrict database file permissions")
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def get_app_settings():
    settings = dict(DEFAULT_SETTINGS)
    rows = get_db().execute("SELECT setting_key, setting_value FROM app_settings").fetchall()
    settings.update({row["setting_key"]: row["setting_value"] for row in rows})
    return settings


def safe_theme_color(settings, key):
    value = settings.get(key, DEFAULT_SETTINGS[key])
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value or "") else DEFAULT_SETTINGS[key]


def clock_minutes(value):
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def format_clock(minutes):
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def work_window(settings=None):
    settings = settings or get_app_settings()
    start_minutes = clock_minutes(settings["work_start"])
    end_minutes = clock_minutes(settings["work_end"])
    duration_minutes = (end_minutes - start_minutes) % (24 * 60)
    if duration_minutes == 0:
        duration_minutes = 24 * 60
    return start_minutes, duration_minutes


def cycle_offset(value, cycle_start_minutes):
    return (clock_minutes(value) - cycle_start_minutes) % (24 * 60)


def stadium_business_date(moment=None, settings=None):
    """Return the date on which the current stadium-day cycle began."""
    moment = moment or app_now()
    settings = settings or get_app_settings()
    business_date = moment.date()
    if moment.hour * 60 + moment.minute < clock_minutes(settings["work_start"]):
        business_date -= timedelta(days=1)
    return business_date


def migrate_legacy_bookings(db):
    """Migrate the earlier local booking schema without dropping existing data."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
    if not columns or "start_time" in columns:
        return
    if not {"booking_time", "total_amount", "paid_amount"}.issubset(columns):
        raise sqlite3.OperationalError("Unsupported bookings schema")

    legacy_rows = db.execute("SELECT * FROM bookings ORDER BY id").fetchall()
    db.execute("ALTER TABLE bookings RENAME TO bookings_legacy")
    db.execute(
        """
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            booking_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            hourly_rate INTEGER NOT NULL CHECK (hourly_rate >= 0),
            duration_hours INTEGER NOT NULL CHECK (duration_hours > 0),
            total INTEGER NOT NULL CHECK (total >= 0),
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE booking_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT
        )
        """
    )
    for row in legacy_rows:
        times = re.findall(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)", row["booking_time"] or "")
        if len(times) < 2:
            continue
        start_time = f"{times[0][0]}:{times[0][1]}"
        end_time = f"{times[1][0]}:{times[1][1]}"
        start_minutes = clock_minutes(start_time)
        end_minutes = clock_minutes(end_time)
        duration_hours = ((end_minutes - start_minutes) % (24 * 60)) // 60 or 24
        status = "cancelled" if row["status"] in {"ملغى", "cancelled"} else "active"
        total = int(row["total_amount"] or 0)
        hourly_rate = total // duration_hours if duration_hours else HOURLY_RATE
        db.execute(
            """
            INSERT INTO bookings
                (id, client_id, booking_date, start_time, end_time, hourly_rate,
                 duration_hours, total, notes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                row["id"], row["client_id"], row["booking_date"], start_time,
                end_time, hourly_rate, duration_hours, total, row["notes"] or "",
                status, row["created_at"],
            ),
        )
        paid = int(row["paid_amount"] or 0)
        if paid > 0:
            db.execute(
                "INSERT INTO booking_payments (booking_id, amount) VALUES (?, ?)",
                (row["id"], paid),
            )
    db.execute("DROP TABLE bookings_legacy")


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            session_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    admin_columns = {row[1] for row in db.execute("PRAGMA table_info(admins)").fetchall()}
    legacy_admin_schema = "session_version" not in admin_columns
    if "session_version" not in admin_columns:
        db.execute("ALTER TABLE admins ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            remote_addr TEXT PRIMARY KEY,
            failures INTEGER NOT NULL DEFAULT 0,
            window_started REAL NOT NULL,
            locked_until REAL NOT NULL DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    migrate_legacy_bookings(db)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            booking_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            hourly_rate INTEGER NOT NULL CHECK (hourly_rate >= 0),
            duration_hours INTEGER NOT NULL CHECK (duration_hours > 0),
            total INTEGER NOT NULL CHECK (total >= 0),
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
        )
        """
    )
    booking_columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
    if "booking_type" not in booking_columns:
        db.execute("ALTER TABLE bookings ADD COLUMN booking_type TEXT NOT NULL DEFAULT 'single'")
    if "weekly_rule_id" not in booking_columns:
        db.execute("ALTER TABLE bookings ADD COLUMN weekly_rule_id INTEGER")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookings_date_status_time "
        "ON bookings (booking_date, status, start_time, end_time)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_booking_payments_booking "
        "ON booking_payments (booking_id, paid_at, id)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring_booking_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
            start_time TEXT NOT NULL,
            duration_hours INTEGER NOT NULL CHECK (duration_hours > 0),
            hourly_rate INTEGER NOT NULL DEFAULT 0 CHECK (hourly_rate >= 0),
            deposit INTEGER NOT NULL CHECK (deposit >= 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
        )
        """
    )
    recurring_columns = {row[1] for row in db.execute("PRAGMA table_info(recurring_booking_rules)").fetchall()}
    if "hourly_rate" not in recurring_columns:
        db.execute("ALTER TABLE recurring_booking_rules ADD COLUMN hourly_rate INTEGER NOT NULL DEFAULT 0")
    if "active" not in recurring_columns:
        db.execute("ALTER TABLE recurring_booking_rules ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring_deposit_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            booking_id INTEGER,
            transaction_type TEXT NOT NULL CHECK (transaction_type IN ('collect', 'refund')),
            amount INTEGER NOT NULL CHECK (amount > 0),
            payment_method TEXT NOT NULL DEFAULT 'cash',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rule_id) REFERENCES recurring_booking_rules(id) ON DELETE RESTRICT,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_recurring_booking_rules_weekday_time "
        "ON recurring_booking_rules (weekday, start_time)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount INTEGER NOT NULL CHECK (amount > 0),
            category TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT 'cash' CHECK (payment_method IN ('cash', 'transfer')),
            expense_date TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS other_income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount INTEGER NOT NULL CHECK (amount > 0),
            category TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT 'cash' CHECK (payment_method IN ('cash', 'transfer')),
            income_date TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
        """
    )
    db.executemany(
        "INSERT OR IGNORE INTO app_settings (setting_key, setting_value) VALUES (?, ?)",
        tuple(DEFAULT_SETTINGS.items()),
    )
    admin = db.execute("SELECT id, password_hash FROM admins LIMIT 1").fetchone()
    if admin is None:
        admin_username = os.getenv("ADMIN_USERNAME", "admin").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        if (not admin_username or len(admin_username) > 64 or len(admin_password) < 16
                or len(admin_password) > 1024 or admin_password.startswith("replace-")):
            raise RuntimeError(
                "For a new database, set ADMIN_USERNAME and a unique ADMIN_PASSWORD of at least 16 characters."
            )
        db.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            (admin_username, generate_password_hash(admin_password)),
        )
    elif legacy_admin_schema:
        replacement_password = os.getenv("ADMIN_PASSWORD", "")
        if len(replacement_password) < 16 or replacement_password.startswith("replace-"):
            raise RuntimeError("The default admin password is still active; set a unique ADMIN_PASSWORD before starting.")
        db.execute(
            "UPDATE admins SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
            (generate_password_hash(replacement_password), admin["id"]),
        )
    db.commit()


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("admin_id") is None:
            return redirect(url_for("login"))
        admin = get_db().execute(
            "SELECT session_version FROM admins WHERE id = ?", (session["admin_id"],)
        ).fetchone()
        if admin is None or session.get("session_version") != admin["session_version"]:
            session.clear()
            return redirect(url_for("login"))
        session.permanent = True
        session.modified = True
        return view(*args, **kwargs)

    return wrapped_view


def get_active_booking(booking_id):
    return get_db().execute(
        """
        SELECT b.*, c.name AS client_name,
               COALESCE((SELECT SUM(p.amount) FROM booking_payments AS p WHERE p.booking_id = b.id), 0) AS paid
        FROM bookings AS b
        JOIN clients AS c ON c.id = b.client_id
        WHERE b.id = ? AND b.status = 'active'
        """,
        (booking_id,),
    ).fetchone()


def get_booking_clients():
    return get_db().execute(
        "SELECT id, name, phone FROM clients ORDER BY name COLLATE NOCASE, id DESC"
    ).fetchall()


def get_booking_payment_details(booking_id):
    row = get_db().execute(
        """
        SELECT b.*, c.name AS client_name,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM bookings AS b
        JOIN clients AS c ON c.id = b.client_id
        LEFT JOIN booking_payments AS p ON p.booking_id = b.id
        WHERE b.id = ?
        GROUP BY b.id
        """,
        (booking_id,),
    ).fetchone()
    if row is None:
        return None
    booking = dict(row)
    booking["remaining"] = booking["total"] - booking["paid"]
    deposit = get_db().execute(
        """
        SELECT COALESCE(SUM(CASE WHEN transaction_type = 'collect' THEN amount ELSE 0 END), 0) AS collected,
               COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount ELSE 0 END), 0) AS refunded
        FROM recurring_deposit_transactions
        WHERE rule_id = (SELECT weekly_rule_id FROM bookings WHERE id = ?)
        """,
        (booking_id,),
    ).fetchone()
    booking["deposit_collected"] = int(deposit["collected"] or 0)
    booking["deposit_refunded"] = int(deposit["refunded"] or 0)
    booking["deposit_held"] = max(0, booking["deposit_collected"] - booking["deposit_refunded"])
    booking["deposit_status"] = "مسترد" if booking["deposit_held"] == 0 and booking["deposit_collected"] else ("محفوظ" if booking["deposit_held"] else "لا يوجد")
    booking["payment_status"] = (
        "مدفوع بالكامل" if booking["paid"] == booking["total"] else "عربون"
    )
    return booking


def get_payment_page(booking_id, error=None, amount="", status=200):
    booking = get_booking_payment_details(booking_id)
    if booking is None:
        return None
    payments = get_db().execute(
        "SELECT id, amount, paid_at FROM booking_payments "
        "WHERE booking_id = ? ORDER BY paid_at DESC, id DESC",
        (booking_id,),
    ).fetchall()
    return render_template(
        "booking_payments.html",
        booking=booking,
        payments=payments,
        error=error,
        amount=amount,
    ), status


def get_booking_report_summary(start_date, end_date):
    work_start = get_app_settings()["work_start"]
    row = get_db().execute(
        """
        SELECT COUNT(*) AS booking_count,
               COALESCE(SUM(b.total), 0) AS total_amount,
               COALESCE(SUM(COALESCE(p.paid, 0)), 0) AS total_paid,
               COALESCE(SUM(b.total - COALESCE(p.paid, 0)), 0) AS total_remaining,
               COALESCE(SUM(b.duration_hours), 0) AS booked_hours
        FROM bookings AS b
        LEFT JOIN (
            SELECT booking_id, SUM(amount) AS paid
            FROM booking_payments
            GROUP BY booking_id
        ) AS p ON p.booking_id = b.id
        WHERE b.status = 'active'
          AND CASE WHEN b.start_time < ?
                   THEN date(b.booking_date, '-1 day')
                   ELSE b.booking_date END >= ?
          AND CASE WHEN b.start_time < ?
                   THEN date(b.booking_date, '-1 day')
                   ELSE b.booking_date END <= ?
        """,
        (work_start, start_date, work_start, end_date),
    ).fetchone()
    summary = dict(row)
    deposit = get_db().execute(
        """
        SELECT COALESCE(SUM(CASE WHEN transaction_type = 'collect' THEN amount ELSE 0 END), 0) AS deposit_collected,
               COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount ELSE 0 END), 0) AS deposit_refunded
        FROM recurring_deposit_transactions
        WHERE date(created_at) >= ? AND date(created_at) <= ?
        """, (start_date, end_date)
    ).fetchone()
    summary.update(dict(deposit))
    summary["deposit_held"] = summary["deposit_collected"] - summary["deposit_refunded"]
    return summary


def get_finance_summary(start_date, end_date):
    booking = get_booking_report_summary(start_date, end_date)
    db = get_db()
    other_income = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM other_income WHERE income_date >= ? AND income_date <= ?",
        (start_date, end_date),
    ).fetchone()[0]
    expenses = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE expense_date >= ? AND expense_date <= ?",
        (start_date, end_date),
    ).fetchone()[0]
    booking_income = int(booking["total_paid"] or 0)
    other_income = int(other_income or 0)
    expenses = int(expenses or 0)
    return {
        "booking_income": booking_income,
        "other_income": other_income,
        "total_income": booking_income + other_income,
        "expenses": expenses,
        "net_income": booking_income + other_income - expenses,
    }


def get_dashboard_performance(stadium_today, settings):
    """Build a compact seven-day performance series from the existing reports."""
    _cycle_start_minutes, window_minutes = work_window(settings)
    series = []
    for offset in range(6, -1, -1):
        day = stadium_today - timedelta(days=offset)
        day_key = day.isoformat()
        finance = get_finance_summary(day_key, day_key)
        summary = get_booking_report_summary(day_key, day_key)
        series.append({
            "date": day_key,
            "label": day.strftime("%d/%m"),
            "income": finance["total_income"],
            "booked_hours": summary["booked_hours"],
            "available_hours": max(0, window_minutes // 60 - summary["booked_hours"]),
        })
    max_income = max((item["income"] for item in series), default=0)
    for item in series:
        item["bar_percent"] = round(item["income"] / max_income * 100) if max_income else 0
    today_summary = series[-1]
    total_hours = today_summary["booked_hours"] + today_summary["available_hours"]
    occupancy_percent = round(today_summary["booked_hours"] / total_hours * 100) if total_hours else 0
    return {
        "series": series,
        "max_income": max_income,
        "today_occupancy": occupancy_percent,
        "today_booked_hours": today_summary["booked_hours"],
        "today_available_hours": today_summary["available_hours"],
        "window_hours": window_minutes // 60,
    }


def render_booking_form(booking=None, booking_id=None, form_values=None, error=None, status=200):
    settings = get_app_settings()
    booking_deposit = 0
    if booking_id:
        deposit_row = get_db().execute(
            """
            SELECT COALESCE(SUM(CASE WHEN t.transaction_type = 'collect' THEN t.amount ELSE -t.amount END), 0) AS held
            FROM recurring_deposit_transactions AS t
            WHERE t.rule_id = (SELECT weekly_rule_id FROM bookings WHERE id = ?)
            """, (booking_id,)
        ).fetchone()
        booking_deposit = max(0, int(deposit_row["held"] or 0))
    cycle_start_minutes, window_minutes = work_window(settings)
    window_hours = window_minutes // 60
    default_booking_date = stadium_business_date(settings=settings).isoformat()
    if form_values is None:
        if booking is not None:
            form_values = dict(booking)
        else:
            form_values = {
                "booking_date": request.args.get("date", default_booking_date),
                "start_time": request.args.get("start_time", settings["work_start"]),
                "client_id": "",
                "hourly_rate": settings["hourly_rate"],
                "duration_hours": settings["booking_duration"],
                "initial_payment": "0",
                "notes": "",
            }

    values = {
        "client_id": str(form_values.get("client_id", "")),
        "booking_date": str(form_values.get("booking_date", default_booking_date)),
        "start_time": str(form_values.get("start_time", settings["work_start"])),
        "hourly_rate": str(form_values.get("hourly_rate", settings["hourly_rate"])),
        "duration_hours": str(form_values.get("duration_hours", settings["booking_duration"])),
        "initial_payment": str(form_values.get("initial_payment", "0")),
        "notes": str(form_values.get("notes", "")),
    }
    try:
        start_minutes = clock_minutes(values["start_time"])
        duration_hours = int(values["duration_hours"])
        end_time = format_clock(start_minutes + duration_hours * 60)
    except (ValueError, IndexError):
        end_time = ""
    try:
        total = int(values["hourly_rate"]) * int(values["duration_hours"])
    except ValueError:
        total = ""
    try:
        already_paid = int(form_values.get("paid", (booking or {}).get("paid", 0)) or 0)
        initial_payment = int(values["initial_payment"] or 0)
    except (TypeError, ValueError):
        already_paid = 0
        initial_payment = 0
    try:
        selected_start_offset = cycle_offset(values["start_time"], cycle_start_minutes) // 60
    except (ValueError, IndexError):
        selected_start_offset = 0
    maximum_duration = max(1, window_hours - selected_start_offset)
    start_time_options = [format_clock(cycle_start_minutes + hour * 60) for hour in range(window_hours)]
    if values["start_time"] not in start_time_options:
        start_time_options.insert(0, values["start_time"])
    paid_preview = already_paid if booking_id else initial_payment
    remaining_preview = total - paid_preview if isinstance(total, int) else ""
    payment_status_preview = (
        "مدفوع بالكامل" if isinstance(total, int) and paid_preview == total else "عربون"
    )
    clients = get_booking_clients()
    selected_client = next(
        (client for client in clients if str(client["id"]) == values["client_id"]),
        None,
    )

    return render_template(
        "booking_form.html",
        booking_id=booking_id,
        form=values,
        end_time=end_time,
        total=total,
        already_paid=already_paid,
        booking_deposit=booking_deposit,
        remaining_preview=remaining_preview,
        payment_status_preview=payment_status_preview,
        start_time_options=start_time_options,
        window_hours=window_hours,
        cycle_start=settings["work_start"],
        clients=clients,
        selected_client=selected_client,
        error=error,
        maximum_duration=maximum_duration,
    ), status


def parse_booking_form(form):
    try:
        booking_date = datetime.strptime(form.get("booking_date", ""), "%Y-%m-%d").date()
        start = datetime.strptime(form.get("start_time", ""), "%H:%M")
        client_id = int(form.get("client_id", ""))
        hourly_rate = int(form.get("hourly_rate", ""))
        duration_hours = int(form.get("duration_hours", ""))
        initial_payment = int(form.get("initial_payment", "0") or 0)
    except (TypeError, ValueError):
        raise ValueError("أكمل الحقول المطلوبة واختر تاريخًا ووقتًا صحيحين.")

    if client_id < 1:
        raise ValueError("اختر العميل.")
    settings = get_app_settings()
    cycle_start_minutes, window_minutes = work_window(settings)
    start_offset = cycle_offset(start.strftime("%H:%M"), cycle_start_minutes)
    if start.minute != 0 or start_offset >= window_minutes:
        raise ValueError("وقت البداية يجب أن يكون ضمن دورة عمل الملعب وعلى رأس الساعة.")
    if duration_hours < 1:
        raise ValueError("عدد الساعات يجب أن يكون ساعة واحدة على الأقل.")
    if hourly_rate < 0:
        raise ValueError("سعر الساعة لا يمكن أن يكون سالبًا.")
    total = hourly_rate * duration_hours
    if initial_payment < 0 or initial_payment > total:
        raise ValueError("قيمة العربون لا يمكن أن تكون سالبة أو تتجاوز إجمالي الحجز.")
    maximum_duration = (window_minutes - start_offset) // 60
    if duration_hours > maximum_duration:
        raise ValueError("مدة الحجز تتجاوز نهاية دورة عمل الملعب.")
    end_time = format_clock(clock_minutes(start.strftime("%H:%M")) + duration_hours * 60)

    return {
        "client_id": client_id,
        "booking_date": booking_date.isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end_time,
        "hourly_rate": hourly_rate,
        "duration_hours": duration_hours,
        "total": total,
        "initial_payment": initial_payment,
        "notes": form.get("notes", "").strip(),
    }


def save_booking(values, booking_id=None):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        client = db.execute("SELECT id FROM clients WHERE id = ?", (values["client_id"],)).fetchone()
        if client is None:
            raise ValueError("العميل المحدد غير موجود.")

        if booking_id is not None:
            current = db.execute(
                "SELECT id FROM bookings WHERE id = ? AND status = 'active'", (booking_id,)
            ).fetchone()
            if current is None:
                raise ValueError("الحجز غير موجود أو تم إلغاؤه بالفعل.")
            paid = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS paid FROM booking_payments WHERE booking_id = ?",
                (booking_id,),
            ).fetchone()["paid"]
            if values["total"] < paid:
                raise ValueError("لا يمكن خفض إجمالي الحجز إلى أقل من المبلغ المدفوع.")

        settings = get_app_settings()
        cycle_start_minutes, _window_minutes = work_window(settings)
        new_start_offset = cycle_offset(values["start_time"], cycle_start_minutes)
        new_end_offset = new_start_offset + values["duration_hours"] * 60
        conflict_query = """
            SELECT id, start_time, duration_hours FROM bookings
            WHERE booking_date = ? AND status = 'active'
        """
        conflict_params = [values["booking_date"]]
        if booking_id is not None:
            conflict_query += " AND id != ?"
            conflict_params.append(booking_id)
        existing_bookings = db.execute(conflict_query, conflict_params).fetchall()
        for existing in existing_bookings:
            existing_start = cycle_offset(existing["start_time"], cycle_start_minutes)
            existing_end = existing_start + existing["duration_hours"] * 60
            if new_start_offset < existing_end and new_end_offset > existing_start:
                raise BookingConflict("هذا الوقت يتعارض مع حجز آخر. اختر وقتًا مختلفًا.")

        if booking_id is None:
            cursor = db.execute(
                """
                INSERT INTO bookings
                    (client_id, booking_date, start_time, end_time, hourly_rate,
                     duration_hours, total, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["client_id"], values["booking_date"], values["start_time"],
                    values["end_time"], values["hourly_rate"], values["duration_hours"],
                    values["total"], values["notes"],
                ),
            )
            saved_id = cursor.lastrowid
            if values.get("initial_payment", 0) > 0:
                db.execute(
                    "INSERT INTO booking_payments (booking_id, amount) VALUES (?, ?)",
                    (saved_id, values["initial_payment"]),
                )
        else:
            db.execute(
                """
                UPDATE bookings
                SET client_id = ?, booking_date = ?, start_time = ?, end_time = ?,
                    hourly_rate = ?, duration_hours = ?, total = ?, notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'active'
                """,
                (
                    values["client_id"], values["booking_date"], values["start_time"],
                    values["end_time"], values["hourly_rate"], values["duration_hours"],
                    values["total"], values["notes"], booking_id,
                ),
            )
            saved_id = booking_id

        db.commit()
        return saved_id
    except Exception:
        db.rollback()
        raise


@app.route("/", methods=["GET"])
def home():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:64]
        password = request.form.get("password", "")[:1025]
        db = get_db()
        remote_addr = (request.remote_addr or "unknown")[:128]
        now = time.time()
        attempt = db.execute(
            "SELECT failures, window_started, locked_until FROM login_attempts WHERE remote_addr = ?",
            (remote_addr,),
        ).fetchone()
        if attempt is not None and attempt["locked_until"] > now:
            flash("اسم المستخدم أو كلمة المرور غير صحيحة. حاول لاحقًا.", "error")
            settings = get_app_settings()
            venue_phone = settings.get("venue_phone", "").strip()
            return render_template("login.html", venue_phone=venue_phone, whatsapp_number=re.sub(r"\D", "", venue_phone)), 429
        admin = get_db().execute(
            "SELECT id, username, password_hash, session_version FROM admins WHERE username = ?",
            (username,),
        ).fetchone()
        password_matches = check_password_hash(
            admin["password_hash"] if admin is not None else DUMMY_PASSWORD_HASH, password
        )
        if admin is not None and password_matches:
            db.execute("DELETE FROM login_attempts WHERE remote_addr = ?", (remote_addr,))
            db.commit()
            session.clear()
            session.permanent = True
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            session["session_version"] = admin["session_version"]
            return redirect(url_for("admin_home"))
        if attempt is not None and now - attempt["window_started"] < 900:
            failures = attempt["failures"] + 1
            window_started = attempt["window_started"]
        else:
            failures = 1
            window_started = now
        locked_until = now + 900 if failures >= 5 else 0
        db.execute(
            """
            INSERT INTO login_attempts (remote_addr, failures, window_started, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(remote_addr) DO UPDATE SET
                failures=excluded.failures, window_started=excluded.window_started,
                locked_until=excluded.locked_until
            """,
            (remote_addr, failures, window_started, locked_until),
        )
        db.execute("DELETE FROM login_attempts WHERE window_started < ?", (now - 86400,))
        db.commit()
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    settings = get_app_settings()
    venue_phone = settings.get("venue_phone", "").strip()
    return render_template("login.html", venue_phone=venue_phone, whatsapp_number=re.sub(r"\D", "", venue_phone))


@app.route("/admin", methods=["GET"])
@login_required
def admin_home():
    settings = get_app_settings()
    today_date = stadium_business_date(settings=settings)
    today = today_date.isoformat()
    booking_summary = get_booking_report_summary(today, today)
    finance_summary = get_finance_summary(today, today)
    cycle_start_minutes, window_minutes = work_window(settings)
    current_time = app_now()
    current_minutes = current_time.hour * 60 + current_time.minute
    current_offset = (current_minutes - cycle_start_minutes) % (24 * 60)
    current_booking = None
    current_rows = get_db().execute(
        "SELECT start_time, duration_hours, booking_type FROM bookings "
        "WHERE booking_date = ? AND status = 'active'", (today,)
    ).fetchall()
    for row in current_rows:
        start_offset = cycle_offset(row["start_time"], cycle_start_minutes)
        if start_offset <= current_offset < start_offset + row["duration_hours"] * 60:
            current_booking = row
            break
    upcoming_rows = get_db().execute(
        """
        SELECT b.booking_date, b.start_time, b.duration_hours, b.booking_type,
               c.name AS client_name, b.total, COALESCE(SUM(p.amount), 0) AS paid
        FROM bookings AS b
        JOIN clients AS c ON c.id = b.client_id
        LEFT JOIN booking_payments AS p ON p.booking_id = b.id
        WHERE b.status = 'active' AND b.booking_date >= ?
        GROUP BY b.id ORDER BY b.booking_date ASC LIMIT 30
        """, (today,)
    ).fetchall()
    upcoming = []
    for row in upcoming_rows:
        item = dict(row)
        item["remaining"] = item["total"] - item["paid"]
        item["offset"] = cycle_offset(item["start_time"], cycle_start_minutes)
        upcoming.append(item)
    upcoming.sort(key=lambda item: (item["booking_date"], item["offset"]))
    dashboard_stats = {
        "booking_income": booking_summary["total_paid"],
        "booking_count": booking_summary["booking_count"],
        "occupied_hours": booking_summary["booked_hours"],
        "available_hours": max(0, window_minutes // 60 - booking_summary["booked_hours"]),
        "paid": booking_summary["total_paid"],
        "remaining": booking_summary["total_remaining"],
        "expenses": finance_summary["expenses"],
        "net_income": finance_summary["net_income"],
    }
    dashboard_performance = get_dashboard_performance(today_date, settings)
    return render_template(
        "admin_home.html", username=session.get("admin_username"),
        venue_name=settings["venue_name"],
        dashboard_date=today,
        current_time=current_time.strftime("%H:%M"),
        dashboard_finance=get_finance_summary(today, today),
        dashboard_stats=dashboard_stats,
        dashboard_performance=dashboard_performance,
        current_booking=current_booking,
        upcoming_bookings=upcoming[:5],
    )


@app.route("/admin/account", methods=["GET", "POST"])
@login_required
def admin_account():
    db = get_db()
    admin_id = session["admin_id"]
    admin = db.execute(
        "SELECT id, username, password_hash, session_version FROM admins WHERE id = ?", (admin_id,)
    ).fetchone()
    if admin is None:
        session.clear()
        return redirect(url_for("login"))
    form = {"username": admin["username"]}
    error = None
    if request.method == "POST":
        form["username"] = request.form.get("username", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        try:
            if not form["username"] or len(form["username"]) > 64:
                raise ValueError("اسم المستخدم مطلوب ويجب ألا يتجاوز 64 حرفًا.")
            if not check_password_hash(admin["password_hash"], current_password):
                raise ValueError("كلمة المرور الحالية غير صحيحة.")
            if new_password and not 12 <= len(new_password) <= 1024:
                raise ValueError("كلمة المرور الجديدة يجب أن تكون بين 12 و1024 حرفًا.")
            if new_password != confirm_password:
                raise ValueError("تأكيد كلمة المرور غير مطابق.")
            duplicate = db.execute(
                "SELECT id FROM admins WHERE username = ? AND id != ?", (form["username"], admin_id)
            ).fetchone()
            if duplicate is not None:
                raise ValueError("اسم المستخدم مستخدم بالفعل.")
            if new_password:
                db.execute(
                    "UPDATE admins SET username = ?, password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                    (form["username"], generate_password_hash(new_password), admin_id),
                )
                session["session_version"] = admin["session_version"] + 1
            else:
                db.execute("UPDATE admins SET username = ? WHERE id = ?", (form["username"], admin_id))
            db.commit()
            session["admin_username"] = form["username"]
            flash("تم تحديث بيانات الحساب بنجاح.", "success")
            return redirect(url_for("admin_account"))
        except ValueError as exc:
            error = str(exc)
    return render_template("admin_account.html", form=form, error=error)


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "GET":
        return render_template("settings.html", settings=get_app_settings(), error=None)

    values = {
        "venue_name": request.form.get("venue_name", "").strip(),
        "venue_phone": request.form.get("venue_phone", "").strip(),
        "venue_address": request.form.get("venue_address", "").strip(),
        "hourly_rate": request.form.get("hourly_rate", "").strip(),
        "work_start": request.form.get("work_start", "").strip(),
        "work_end": request.form.get("work_end", "").strip(),
        "booking_duration": request.form.get("booking_duration", "").strip(),
        "primary_color": request.form.get("primary_color", "").strip(),
        "secondary_color": request.form.get("secondary_color", "").strip(),
    }
    try:
        hourly_rate = int(values["hourly_rate"])
        booking_duration = int(values["booking_duration"])
    except ValueError:
        return render_template(
            "settings.html", settings=values,
            error="أدخل سعر ساعة ومدة حجز صحيحين.",
        ), 400

    try:
        if not values["venue_name"]:
            raise ValueError("اسم الملعب مطلوب.")
        if any(not re.fullmatch(r"#[0-9a-fA-F]{6}", values[key])
               for key in ("primary_color", "secondary_color")):
            raise ValueError("اختر ألوانًا صالحة من منتقي الألوان.")
        if hourly_rate < 0:
            raise ValueError("سعر الساعة لا يمكن أن يكون سالبًا.")
        if booking_duration < 1 or booking_duration > 24:
            raise ValueError("مدة الحجز الافتراضية يجب أن تكون بين ساعة و24 ساعة.")
        start_time = datetime.strptime(values["work_start"], "%H:%M")
        end_time = datetime.strptime(values["work_end"], "%H:%M")
        if start_time.minute != 0 or end_time.minute != 0:
            raise ValueError("أوقات التشغيل يجب أن تكون على رأس الساعة.")
        start_minutes = start_time.hour * 60
        end_minutes = end_time.hour * 60
        window_minutes = (end_minutes - start_minutes) % (24 * 60) or 24 * 60
        if booking_duration > window_minutes // 60:
            raise ValueError("مدة الحجز الافتراضية لا يمكن أن تتجاوز ساعات التشغيل.")
        values["hourly_rate"] = str(hourly_rate)
        values["booking_duration"] = str(booking_duration)
        values["work_start"] = start_time.strftime("%H:%M")
        values["work_end"] = end_time.strftime("%H:%M")
    except ValueError as exc:
        return render_template("settings.html", settings=values, error=str(exc)), 400

    db = get_db()
    db.executemany(
        """
        INSERT INTO app_settings (setting_key, setting_value) VALUES (?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
        """,
        tuple(values.items()),
    )
    db.commit()
    flash("تم حفظ الإعدادات بنجاح.", "success")
    return redirect(url_for("settings_page"))


@app.route("/theme.css", methods=["GET"])
def theme_css():
    settings = get_app_settings()
    css = (
        ":root {\n"
        f"  --blue: {safe_theme_color(settings, 'primary_color')};\n"
        f"  --blue-dark: {safe_theme_color(settings, 'secondary_color')};\n"
        "}\n"
    )
    response = Response(css, mimetype="text/css")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/reports", methods=["GET"])
@login_required
def admin_reports():
    settings = get_app_settings()
    stadium_today = stadium_business_date(app_now(), settings)
    month_start = stadium_today.replace(day=1)
    year_start = date(stadium_today.year, 1, 1)
    cycle_start_minutes, cycle_minutes = work_window(settings)
    cycle_end = format_clock(cycle_start_minutes + cycle_minutes)
    next_day_suffix = " في اليوم التالي" if cycle_start_minutes + cycle_minutes >= 24 * 60 else ""
    cycle_description = (
        f"من {settings['work_start']} إلى {cycle_end}{next_day_suffix}"
    )
    custom_start = request.args.get("start_date", "").strip()
    custom_end = request.args.get("end_date", "").strip()
    custom_summary = None
    custom_label = None
    error = None

    if custom_start or custom_end:
        if not custom_start or not custom_end:
            error = "اختر تاريخ البداية وتاريخ النهاية للتقرير المخصص."
        else:
            try:
                start_day = datetime.strptime(custom_start, "%Y-%m-%d").date()
                end_day = datetime.strptime(custom_end, "%Y-%m-%d").date()
                if start_day > end_day:
                    error = "تاريخ البداية يجب ألا يتجاوز تاريخ النهاية."
                else:
                    custom_summary = get_booking_report_summary(
                        start_day.isoformat(), end_day.isoformat()
                    )
                    custom_summary["finance"] = get_finance_summary(start_day.isoformat(), end_day.isoformat())
                    custom_label = f"{start_day.strftime('%d/%m/%Y')} — {end_day.strftime('%d/%m/%Y')}"
            except ValueError:
                error = "أدخل تاريخًا صحيحًا للبداية والنهاية."

    return render_template(
        "reports.html",
        today_label=f"{stadium_today.strftime('%d/%m/%Y')} · {cycle_description}",
        month_label=(
            f"من بداية الشهر ({month_start.strftime('%d/%m/%Y')}) "
            f"حتى يوم الملعب {stadium_today.strftime('%d/%m/%Y')}"
        ),
        year_label=(
            f"من بداية السنة ({year_start.strftime('%d/%m/%Y')}) "
            f"حتى يوم الملعب {stadium_today.strftime('%d/%m/%Y')}"
        ),
        cycle_description=cycle_description,
        today_summary=get_booking_report_summary(stadium_today.isoformat(), stadium_today.isoformat()),
        month_summary=get_booking_report_summary(month_start.isoformat(), stadium_today.isoformat()),
        year_summary=get_booking_report_summary(year_start.isoformat(), stadium_today.isoformat()),
        today_finance=get_finance_summary(stadium_today.isoformat(), stadium_today.isoformat()),
        month_finance=get_finance_summary(month_start.isoformat(), stadium_today.isoformat()),
        year_finance=get_finance_summary(year_start.isoformat(), stadium_today.isoformat()),
        custom_summary=custom_summary,
        custom_label=custom_label,
        custom_start=custom_start or month_start.isoformat(),
        custom_end=custom_end or stadium_today.isoformat(),
        error=error,
    ), 400 if error else 200


def build_booking_slots(selected_day, settings):
    db = get_db()
    for rule in db.execute("SELECT id FROM recurring_booking_rules WHERE active = 1").fetchall():
        db.execute("BEGIN IMMEDIATE")
        try:
            materialize_recurring_rule(db, rule["id"])
            db.commit()
        except Exception:
            db.rollback()
            raise
    booking_rows = db.execute(
        """
        SELECT b.*, c.name AS client_name, c.phone AS client_phone,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM bookings AS b
        JOIN clients AS c ON c.id = b.client_id
        LEFT JOIN booking_payments AS p ON p.booking_id = b.id
        WHERE b.booking_date = ?
        GROUP BY b.id
        ORDER BY b.id DESC
        """,
        (selected_day.isoformat(),),
    ).fetchall()
    cycle_start_minutes, window_minutes = work_window(settings)
    window_hours = window_minutes // 60
    bookings = []
    for row in booking_rows:
        booking = dict(row)
        booking["cycle_start_offset"] = cycle_offset(booking["start_time"], cycle_start_minutes) // 60
        if booking["status"] == "cancelled":
            booking["card_state"] = "cancelled"
            booking["status_label"] = "ملغى"
        elif booking.get("booking_type") == "recurring":
            booking["card_state"] = "recurring"
            booking["status_label"] = "متكرر"
        elif booking["paid"] < booking["total"]:
            booking["card_state"] = "deposit"
            booking["status_label"] = "عربون"
        else:
            booking["card_state"] = "booked"
            booking["status_label"] = "محجوز"
        phone = booking["client_phone"] or ""
        booking["short_phone"] = f"{phone[:3]}…" if len(phone) > 3 else phone
        booking["priority"] = 0
        bookings.append(booking)

    bookings.sort(key=lambda item: (item["cycle_start_offset"], item["priority"], -item["id"]))
    slots = []
    for slot_offset in range(window_hours):
        slot_start = format_clock(cycle_start_minutes + slot_offset * 60)
        slot_end = format_clock(cycle_start_minutes + (slot_offset + 1) * 60)
        covering = [
            item for item in bookings
            if item["cycle_start_offset"] < slot_offset + 1
            and item["cycle_start_offset"] + item["duration_hours"] > slot_offset
        ]
        booking = next((item for item in sorted(covering, key=lambda item: item["priority"]) if item["status"] == "active"), None)
        slots.append({"start": slot_start, "end": slot_end, "booking": booking})
    return slots, window_hours, cycle_start_minutes


def build_public_slots(selected_day, settings):
    """Build a read-only public schedule, exposing only the occupying customer's name."""
    db = get_db()
    cycle_start_minutes, window_minutes = work_window(settings)
    window_hours = window_minutes // 60
    rows = db.execute(
        "SELECT b.start_time, b.duration_hours, b.booking_type, c.name AS client_name "
        "FROM bookings AS b JOIN clients AS c ON c.id = b.client_id "
        "WHERE b.booking_date = ? AND b.status = 'active' ORDER BY b.start_time, b.id",
        (selected_day.isoformat(),),
    ).fetchall()
    recurring_rules = db.execute(
        "SELECT r.start_time, r.duration_hours, c.name AS client_name "
        "FROM recurring_booking_rules AS r JOIN clients AS c ON c.id = r.client_id "
        "WHERE r.weekday = ? AND r.active = 1 ORDER BY r.start_time, r.id",
        (selected_day.weekday(),),
    ).fetchall()
    occupied = []
    for row in rows:
        occupied.append({
            "start": cycle_offset(row["start_time"], cycle_start_minutes) // 60,
            "duration": row["duration_hours"],
            "state": "fixed" if row["booking_type"] == "recurring" else "booked",
            "client_name": row["client_name"],
        })
    for row in recurring_rules:
        occupied.append({
            "start": cycle_offset(row["start_time"], cycle_start_minutes) // 60,
            "duration": row["duration_hours"],
            "state": "fixed",
            "client_name": row["client_name"],
        })
    slots = []
    for slot_offset in range(window_hours):
        covering = [
            item for item in occupied
            if item["start"] < slot_offset + 1
            and item["start"] + item["duration"] > slot_offset
        ]
        state = "empty"
        if any(item["state"] == "booked" for item in covering):
            state = "booked"
        elif any(item["state"] == "fixed" for item in covering):
            state = "fixed"
        client_names = list(dict.fromkeys(
            item["client_name"] for item in covering
            if item["state"] == state and item.get("client_name")
        ))
        slots.append({
            "start": format_clock(cycle_start_minutes + slot_offset * 60),
            "end": format_clock(cycle_start_minutes + (slot_offset + 1) * 60),
            "state": state,
            "client_names": client_names,
        })
    return slots


@app.route("/public-schedule", methods=["GET"])
def public_schedule():
    settings = get_app_settings()
    today = stadium_business_date(settings=settings)
    month_names = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
    ]
    weekday_names = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    days = []
    for offset in range(7):
        current = today + timedelta(days=offset)
        days.append({
            "date": current.isoformat(),
            "weekday": weekday_names[current.weekday()],
            "label": f"{current.day} {month_names[current.month - 1]}",
            "is_today": offset == 0,
        })
    selected_value = request.args.get("date", today.isoformat())
    try:
        selected_day = datetime.strptime(selected_value, "%Y-%m-%d").date()
    except ValueError:
        selected_day = today
    valid_dates = {item["date"] for item in days}
    if selected_day.isoformat() not in valid_dates:
        selected_day = today
    selected_date = selected_day.isoformat()
    venue_phone = settings.get("venue_phone", "").strip()
    whatsapp_number = re.sub(r"\D", "", venue_phone)
    return render_template(
        "public_schedule.html",
        venue_name=settings["venue_name"],
        venue_phone=venue_phone,
        whatsapp_number=whatsapp_number,
        days=days,
        selected_date=selected_date,
        slots=build_public_slots(selected_day, settings),
    )


@app.route("/admin/booking", methods=["GET"])
@login_required
def booking_screen():
    settings = get_app_settings()
    today = stadium_business_date(settings=settings)
    month_names = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
    ]
    weekday_names = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    show_previous = request.args.get("range") == "previous"
    range_start = today - timedelta(days=7) if show_previous else today
    end_day = range_start + timedelta(days=6)
    count_rows = get_db().execute(
        """
        SELECT booking_date, COUNT(*) AS booking_count
        FROM bookings
        WHERE status = 'active' AND booking_date >= ? AND booking_date <= ?
        GROUP BY booking_date
        """,
        (range_start.isoformat(), end_day.isoformat()),
    ).fetchall()
    booking_counts = {row["booking_date"]: row["booking_count"] for row in count_rows}
    days = []
    for offset in range(7):
        current_day = range_start + timedelta(days=offset)
        days.append({
            "date": current_day.isoformat(),
            "weekday": weekday_names[current_day.weekday()],
            "date_label": f"{current_day.day} {month_names[current_day.month - 1]}",
            "booking_count": booking_counts.get(current_day.isoformat(), 0),
            "is_today": current_day == today,
        })

    try:
        selected_day = datetime.strptime(
            request.args.get("date", range_start.isoformat()), "%Y-%m-%d"
        ).date()
    except ValueError:
        flash("اختر تاريخًا صحيحًا لعرض الساعات.", "error")
        selected_day = range_start
    valid_days = {range_start + timedelta(days=offset) for offset in range(7)}
    if selected_day not in valid_days:
        selected_day = range_start

    slots, window_hours, cycle_start_minutes = build_booking_slots(selected_day, settings)
    cycle_start_minutes, window_minutes = work_window(settings)
    cycle_end_minutes = cycle_start_minutes + window_minutes
    return render_template(
        "booking_screen.html",
        days=days,
        work_start=settings["work_start"],
        work_end_label=format_clock(cycle_end_minutes),
        work_end_day="اليوم التالي" if cycle_end_minutes >= 24 * 60 else "اليوم نفسه",
        work_window_hours=window_minutes // 60,
        selected_date=selected_day.isoformat(),
        slots=slots,
        show_previous=show_previous,
        range_title="الأيام السبعة السابقة" if show_previous else "الأيام السبعة القادمة",
    )


@app.route("/admin/booking/hours", methods=["GET"])
@login_required
def booking_hours():
    return redirect(url_for("booking_screen", date=request.args.get("date", "")))


@app.route("/admin/bookings/new", methods=["GET", "POST"])
@login_required
def booking_new():
    if request.method == "GET":
        return render_booking_form()
    try:
        values = parse_booking_form(request.form)
        save_booking(values)
    except ValueError as error:
        status = 409 if isinstance(error, BookingConflict) else 400
        return render_booking_form(form_values=request.form.to_dict(), error=str(error), status=status)
    flash("تم إنشاء الحجز بنجاح.", "success")
    return redirect(url_for("booking_hours", date=values["booking_date"]))


def materialize_recurring_rule(db, rule_id, weeks=52):
    rule = db.execute(
        "SELECT * FROM recurring_booking_rules WHERE id = ? AND active = 1", (rule_id,)
    ).fetchone()
    if rule is None:
        return 0
    if rule["hourly_rate"] <= 0:
        return 0
    settings = get_app_settings()
    cycle_start_minutes, _ = work_window(settings)
    start_offset = cycle_offset(rule["start_time"], cycle_start_minutes)
    end_time = format_clock(clock_minutes(rule["start_time"]) + rule["duration_hours"] * 60)
    first_day = stadium_business_date(settings=settings)
    days_until = (rule["weekday"] - first_day.weekday()) % 7
    created = 0
    for week in range(weeks):
        booking_date = first_day + timedelta(days=days_until + week * 7)
        existing_rule_booking = db.execute(
            "SELECT id FROM bookings WHERE weekly_rule_id = ? AND booking_date = ?",
            (rule_id, booking_date.isoformat()),
        ).fetchone()
        if existing_rule_booking:
            continue
        new_end = start_offset + rule["duration_hours"] * 60
        conflict = False
        for row in db.execute(
            "SELECT start_time, duration_hours FROM bookings WHERE booking_date = ? AND status = 'active'",
            (booking_date.isoformat(),),
        ).fetchall():
            old_start = cycle_offset(row["start_time"], cycle_start_minutes)
            old_end = old_start + row["duration_hours"] * 60
            if start_offset < old_end and new_end > old_start:
                conflict = True
                break
        if conflict:
            continue
        db.execute(
            """
            INSERT INTO bookings
                (client_id, booking_date, start_time, end_time, hourly_rate,
                 duration_hours, total, notes, booking_type, weekly_rule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recurring', ?)
            """,
            (
                rule["client_id"], booking_date.isoformat(), rule["start_time"], end_time,
                rule["hourly_rate"], rule["duration_hours"],
                rule["hourly_rate"] * rule["duration_hours"], rule["notes"], rule_id,
            ),
        )
        created += 1
    return created


@app.route("/admin/recurring-bookings/new", methods=["GET", "POST"])
@app.route("/admin/recurring-bookings/<int:rule_id>/edit", methods=["GET", "POST"])
@login_required
def recurring_booking_new(rule_id=None):
    settings = get_app_settings()
    existing_rule = None
    if rule_id is not None:
        existing_rule = get_db().execute(
            "SELECT id, client_id, weekday, start_time, duration_hours, deposit, notes "
            "FROM recurring_booking_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if existing_rule is None:
            flash("الحجز المتكرر غير موجود.", "error")
            return redirect(url_for("booking_screen"))
    defaults = {
        "client_id": request.form.get("client_id", str(existing_rule["client_id"] if existing_rule else "")),
        "weekday": request.form.get("weekday", str(existing_rule["weekday"] if existing_rule else stadium_business_date(settings=settings).weekday())),
        "start_time": request.form.get("start_time", existing_rule["start_time"] if existing_rule else settings["work_start"]),
        "duration_hours": request.form.get("duration_hours", str(existing_rule["duration_hours"] if existing_rule else settings["booking_duration"])),
        "hourly_rate": request.form.get("hourly_rate", str(existing_rule["hourly_rate"] if existing_rule else settings["hourly_rate"])),
        "deposit": request.form.get("deposit", str(existing_rule["deposit"] if existing_rule else "0")),
        "notes": request.form.get("notes", existing_rule["notes"] if existing_rule else ""),
    }
    if request.method == "GET":
        return render_template("recurring_booking_form.html", clients=get_booking_clients(), form=defaults, rule_id=rule_id, error=None)

    try:
        client_id = int(defaults["client_id"])
        weekday = int(defaults["weekday"])
        start = datetime.strptime(defaults["start_time"], "%H:%M")
        duration_hours = int(defaults["duration_hours"])
        hourly_rate = int(defaults["hourly_rate"])
        deposit = int(defaults["deposit"])
    except (TypeError, ValueError):
        return render_template("recurring_booking_form.html", clients=get_booking_clients(), form=defaults, rule_id=rule_id, error="أكمل بيانات الحجز المتكرر بشكل صحيح."), 400

    try:
        if client_id < 1 or weekday not in range(7):
            raise ValueError("اختر العميل واليوم بشكل صحيح.")
        if start.minute != 0 or hourly_rate < 0 or deposit < 0 or duration_hours < 1:
            raise ValueError("وقت البداية والسعر والعربون والمدة يجب أن تكون صحيحة.")
        cycle_start_minutes, window_minutes = work_window(settings)
        start_offset = cycle_offset(start.strftime("%H:%M"), cycle_start_minutes)
        if start_offset >= window_minutes or duration_hours > (window_minutes - start_offset) // 60:
            raise ValueError("مدة الحجز تتجاوز دورة عمل الملعب.")
    except ValueError as error:
        return render_template("recurring_booking_form.html", clients=get_booking_clients(), form=defaults, rule_id=rule_id, error=str(error)), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
            raise ValueError("العميل المحدد غير موجود.")
        if rule_id is None:
            db.execute(
                """
                INSERT INTO recurring_booking_rules
                    (client_id, weekday, start_time, duration_hours, hourly_rate, deposit, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (client_id, weekday, start.strftime("%H:%M"), duration_hours, hourly_rate, deposit, defaults["notes"].strip()),
            )
            rule_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if deposit > 0:
                db.execute(
                    "INSERT INTO recurring_deposit_transactions (rule_id, transaction_type, amount, note) VALUES (?, 'collect', ?, ?)",
                    (rule_id, deposit, "عربون الحجز الأسبوعي"),
                )
        else:
            db.execute(
                """
                UPDATE recurring_booking_rules
                SET client_id = ?, weekday = ?, start_time = ?, duration_hours = ?, hourly_rate = ?, notes = ?
                WHERE id = ?
                """,
                (client_id, weekday, start.strftime("%H:%M"), duration_hours, hourly_rate, defaults["notes"].strip(), rule_id),
            )
            today_iso = stadium_business_date(settings=settings).isoformat()
            db.execute(
                "DELETE FROM bookings WHERE weekly_rule_id = ? AND status = 'active' AND booking_date > ?",
                (rule_id, today_iso),
            )
        materialize_recurring_rule(db, rule_id)
        db.commit()
    except Exception as error:
        db.rollback()
        if isinstance(error, (ValueError, BookingConflict)):
            return render_template("recurring_booking_form.html", clients=get_booking_clients(), form=defaults, error=str(error)), 409
        raise
    flash("تم حفظ الحجز الأسبوعي الدائم.", "success")
    return redirect(url_for("booking_screen"))


@app.route("/admin/recurring-bookings/<int:rule_id>/stop", methods=["POST"])
@login_required
def recurring_booking_stop(rule_id):
    db = get_db()
    db.execute("UPDATE recurring_booking_rules SET active = 0 WHERE id = ?", (rule_id,))
    db.commit()
    flash("تم إيقاف الحجز الأسبوعي؛ المواعيد المنشأة سابقًا محفوظة.", "success")
    return redirect(url_for("booking_screen"))


@app.route("/admin/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@login_required
def booking_edit(booking_id):
    if request.method == "GET":
        booking = get_active_booking(booking_id)
        if booking is None:
            flash("الحجز غير موجود أو تم إلغاؤه بالفعل.", "error")
            return redirect(url_for("booking_screen"))
        return render_booking_form(booking=dict(booking), booking_id=booking_id)

    try:
        values = parse_booking_form(request.form)
        save_booking(values, booking_id=booking_id)
    except ValueError as error:
        status = 409 if isinstance(error, BookingConflict) else 400
        booking = get_active_booking(booking_id)
        if booking is None:
            flash("الحجز غير موجود أو تم إلغاؤه بالفعل.", "error")
            return redirect(url_for("booking_screen"))
        return render_booking_form(
            booking=dict(booking),
            booking_id=booking_id,
            form_values=request.form.to_dict(),
            error=str(error),
            status=status,
        )
    flash("تم تعديل الحجز بنجاح.", "success")
    return redirect(url_for("booking_hours", date=values["booking_date"]))


@app.route("/admin/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def booking_cancel(booking_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        booking = db.execute(
            "SELECT booking_date FROM bookings WHERE id = ? AND status = 'active'", (booking_id,)
        ).fetchone()
        if booking is None:
            db.rollback()
            flash("الحجز غير موجود أو تم إلغاؤه بالفعل.", "error")
            return redirect(url_for("booking_screen"))
        db.execute(
            "UPDATE bookings SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'active'",
            (booking_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash("تم إلغاء الحجز وإتاحة وقته مجددًا.", "success")
    return redirect(url_for("booking_hours", date=booking["booking_date"]))


@app.route("/admin/bookings/<int:booking_id>/payments", methods=["GET", "POST"])
@login_required
def booking_payments(booking_id):
    if request.method == "GET":
        page = get_payment_page(booking_id)
        if page is None:
            flash("الحجز غير موجود.", "error")
            return redirect(url_for("booking_screen"))
        return page

    try:
        amount = int(request.form.get("amount", ""))
        if amount <= 0:
            raise PaymentError("يجب أن تكون قيمة الدفعة أكبر من صفر.")
    except ValueError as error:
        message = str(error) if isinstance(error, PaymentError) else "أدخل مبلغًا صحيحًا للدفعة."
        page = get_payment_page(booking_id, error=message, amount=request.form.get("amount", ""), status=400)
        if page is None:
            flash("الحجز غير موجود.", "error")
            return redirect(url_for("booking_screen"))
        return page

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        booking = db.execute(
            """
            SELECT b.total, b.status, COALESCE(SUM(p.amount), 0) AS paid
            FROM bookings AS b
            LEFT JOIN booking_payments AS p ON p.booking_id = b.id
            WHERE b.id = ?
            GROUP BY b.id
            """,
            (booking_id,),
        ).fetchone()
        if booking is None:
            raise PaymentError("الحجز غير موجود.")
        if booking["status"] != "active":
            raise PaymentError("لا يمكن تسجيل دفعة لحجز ملغى.")
        remaining = booking["total"] - booking["paid"]
        if amount > remaining:
            raise PaymentError("قيمة الدفعة تتجاوز المبلغ المتبقي.")
        db.execute(
            "INSERT INTO booking_payments (booking_id, amount) VALUES (?, ?)",
            (booking_id, amount),
        )
        db.commit()
    except PaymentError as error:
        db.rollback()
        page = get_payment_page(booking_id, error=str(error), amount=str(amount), status=409)
        if page is None:
            flash("الحجز غير موجود.", "error")
            return redirect(url_for("booking_screen"))
        return page
    except Exception:
        db.rollback()
        raise

    flash("تم تسجيل الدفعة بنجاح.", "success")
    return redirect(url_for("booking_payments", booking_id=booking_id))


@app.route("/admin/bookings/<int:booking_id>/deposit/refund", methods=["POST"])
@login_required
def booking_deposit_refund(booking_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT weekly_rule_id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None or row["weekly_rule_id"] is None:
            raise ValueError("لا يوجد عربون مستقل مرتبط بهذا الحجز.")
        totals = db.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN transaction_type = 'collect' THEN amount ELSE 0 END), 0) AS collected,
                   COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount ELSE 0 END), 0) AS refunded
            FROM recurring_deposit_transactions WHERE rule_id = ?
            """, (row["weekly_rule_id"],)
        ).fetchone()
        held = int(totals["collected"] or 0) - int(totals["refunded"] or 0)
        if held <= 0:
            raise ValueError("تم استرداد العربون مسبقًا.")
        method = request.form.get("payment_method", "cash")
        if method not in {"cash", "transfer"}:
            raise ValueError("اختر طريقة استرداد صحيحة.")
        db.execute(
            """
            INSERT INTO recurring_deposit_transactions
                (rule_id, booking_id, transaction_type, amount, payment_method, note)
            VALUES (?, ?, 'refund', ?, ?, ?)
            """,
            (row["weekly_rule_id"], booking_id, held, method, request.form.get("note", "").strip()),
        )
        db.commit()
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("booking_payments", booking_id=booking_id))
    except Exception:
        db.rollback()
        raise
    flash("تم استرداد العربون، ولم يُحتسب ضمن دفعات الحجز.", "success")
    return redirect(url_for("booking_payments", booking_id=booking_id))


EXPENSE_CATEGORIES = ["كهرباء", "مياه", "صيانة", "رواتب", "إيجار", "نظافة", "أدوات ومستلزمات", "أخرى"]
INCOME_CATEGORIES = ["تأجير معدات", "بيع مشروبات", "تأجير الملعب لأغراض أخرى", "رسوم أخرى", "أخرى"]


def financial_values(form, field_name):
    values = {
        "amount": form.get("amount", ""),
        "category": form.get("category", ""),
        "custom_category": form.get("custom_category", "").strip(),
        "description": form.get("description", "").strip(),
        "payment_method": form.get("payment_method", "cash"),
        field_name: form.get(field_name, stadium_business_date().isoformat()),
        "notes": form.get("notes", "").strip(),
    }
    amount = int(values["amount"])
    if amount <= 0 or values["payment_method"] not in {"cash", "transfer"}:
        raise ValueError("أدخل مبلغًا صحيحًا واختر طريقة دفع صحيحة.")
    if values["category"] == "أخرى":
        values["category"] = values["custom_category"]
    if not values["category"]:
        raise ValueError("اختر نوع العملية أو أدخل اسم النوع الآخر.")
    datetime.strptime(values[field_name], "%Y-%m-%d")
    values["amount"] = amount
    return values


def financial_list(kind):
    is_expense = kind == "expense"
    if not is_expense and kind != "income":
        abort(404)
    date_field = "expense_date" if is_expense else "income_date"
    query = request.args.get("q", "").strip()
    start = request.args.get("start_date", "").strip()
    end = request.args.get("end_date", "").strip()
    like_query = f"%{query}%"
    params = (query, like_query, like_query, like_query, start, start, end, end)
    if is_expense:
        entries = get_db().execute(
            """
            SELECT * FROM expenses
            WHERE (? = '' OR category LIKE ? OR description LIKE ? OR notes LIKE ?)
              AND (? = '' OR expense_date >= ?)
              AND (? = '' OR expense_date <= ?)
            ORDER BY expense_date DESC, id DESC
            """,
            params,
        ).fetchall()
    else:
        entries = get_db().execute(
            """
            SELECT * FROM other_income
            WHERE (? = '' OR category LIKE ? OR description LIKE ? OR notes LIKE ?)
              AND (? = '' OR income_date >= ?)
              AND (? = '' OR income_date <= ?)
            ORDER BY income_date DESC, id DESC
            """,
            params,
        ).fetchall()
    return render_template(
        "financial_list.html", entries=entries, kind=kind,
        page_title="المصروفات" if is_expense else "واردات أخرى",
        new_endpoint="expense_new" if is_expense else "income_new",
        edit_endpoint="expense_edit" if is_expense else "income_edit",
        delete_endpoint="expense_delete" if is_expense else "income_delete",
        date_field=date_field, query=query, start_date=start, end_date=end,
    )


@app.route("/admin/expenses", methods=["GET"])
@login_required
def expenses_list():
    return financial_list("expense")


@app.route("/admin/income", methods=["GET"])
@login_required
def income_list():
    return financial_list("income")


def financial_form(kind, record_id=None):
    is_expense = kind == "expense"
    if not is_expense and kind != "income":
        abort(404)
    date_field = "expense_date" if is_expense else "income_date"
    categories = EXPENSE_CATEGORIES if is_expense else INCOME_CATEGORIES
    if record_id:
        if is_expense:
            record = get_db().execute("SELECT * FROM expenses WHERE id = ?", (record_id,)).fetchone()
        else:
            record = get_db().execute("SELECT * FROM other_income WHERE id = ?", (record_id,)).fetchone()
        if record is None:
            flash("السجل غير موجود.", "error")
            return redirect(url_for("expenses_list" if is_expense else "income_list"))
    else:
        record = None
    form = dict(record) if record else {"amount": "", "category": "", "custom_category": "", "description": "", "payment_method": "cash", date_field: stadium_business_date().isoformat(), "notes": ""}
    if record and form.get("category") not in categories:
        form["custom_category"] = form["category"]
        form["category"] = "أخرى"
    if request.method == "POST":
        form.update(request.form.to_dict())
        try:
            values = financial_values(request.form, date_field)
            if record_id:
                if is_expense:
                    get_db().execute(
                        "UPDATE expenses SET amount=?, category=?, description=?, payment_method=?, expense_date=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (values["amount"], values["category"], values["description"], values["payment_method"], values[date_field], values["notes"], record_id),
                    )
                else:
                    get_db().execute(
                        "UPDATE other_income SET amount=?, category=?, description=?, payment_method=?, income_date=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (values["amount"], values["category"], values["description"], values["payment_method"], values[date_field], values["notes"], record_id),
                    )
            else:
                if is_expense:
                    get_db().execute(
                        "INSERT INTO expenses (amount, category, description, payment_method, expense_date, notes) VALUES (?, ?, ?, ?, ?, ?)",
                        (values["amount"], values["category"], values["description"], values["payment_method"], values[date_field], values["notes"]),
                    )
                else:
                    get_db().execute(
                        "INSERT INTO other_income (amount, category, description, payment_method, income_date, notes) VALUES (?, ?, ?, ?, ?, ?)",
                        (values["amount"], values["category"], values["description"], values["payment_method"], values[date_field], values["notes"]),
                    )
            get_db().commit()
            flash("تم حفظ السجل بنجاح.", "success")
            return redirect(url_for("expenses_list" if is_expense else "income_list"))
        except (TypeError, ValueError):
            error = "تحقق من البيانات وأدخل تاريخًا ومبلغًا صحيحين."
        else:
            error = None
    else:
        error = None
    return render_template("financial_form.html", form=form, categories=categories, kind=kind, page_title="تعديل مصروف" if record_id and is_expense else "إضافة مصروف" if is_expense else "تعديل وارد" if record_id else "إضافة وارد", date_field=date_field, record_id=record_id, error=error)


@app.route("/admin/expenses/new", methods=["GET", "POST"])
@login_required
def expense_new():
    return financial_form("expense")


@app.route("/admin/expenses/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def expense_edit(record_id):
    return financial_form("expense", record_id)


@app.route("/admin/expenses/<int:record_id>/delete", methods=["POST"])
@login_required
def expense_delete(record_id):
    get_db().execute("DELETE FROM expenses WHERE id = ?", (record_id,)); get_db().commit()
    flash("تم حذف المصروف.", "success")
    return redirect(url_for("expenses_list"))


@app.route("/admin/income/new", methods=["GET", "POST"])
@login_required
def income_new():
    return financial_form("income")


@app.route("/admin/income/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def income_edit(record_id):
    return financial_form("income", record_id)


@app.route("/admin/income/<int:record_id>/delete", methods=["POST"])
@login_required
def income_delete(record_id):
    get_db().execute("DELETE FROM other_income WHERE id = ?", (record_id,)); get_db().commit()
    flash("تم حذف الوارد.", "success")
    return redirect(url_for("income_list"))


@app.route("/admin/clients", methods=["GET"])
@login_required
def clients_list():
    query = request.args.get("q", "").strip()
    db = get_db()
    if query:
        clients = db.execute(
            """
            SELECT id, name, phone, notes
            FROM clients
            WHERE name LIKE ? OR phone LIKE ?
            ORDER BY name COLLATE NOCASE, id DESC
            """,
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
    else:
        clients = db.execute(
            "SELECT id, name, phone, notes FROM clients ORDER BY name COLLATE NOCASE, id DESC"
        ).fetchall()
    return render_template("clients_list.html", clients=clients, query=query)


@app.route("/admin/clients/<int:client_id>", methods=["GET"])
@login_required
def client_detail(client_id):
    db = get_db()
    client = db.execute(
        "SELECT id, name, phone, notes FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if client is None:
        flash("العميل غير موجود.", "error")
        return redirect(url_for("clients_list"))

    rows = db.execute(
        """
        SELECT b.id, b.booking_date, b.start_time, b.end_time, b.total,
               b.duration_hours, b.status,
               COALESCE(p.paid, 0) AS paid
        FROM bookings AS b
        LEFT JOIN (
            SELECT booking_id, SUM(amount) AS paid
            FROM booking_payments
            GROUP BY booking_id
        ) AS p ON p.booking_id = b.id
        WHERE b.client_id = ?
        """,
        (client_id,),
    ).fetchall()

    settings = get_app_settings()
    cycle_start_minutes, _ = work_window(settings)
    bookings = []
    for row in rows:
        booking = dict(row)
        booking["remaining"] = booking["total"] - booking["paid"]
        booking["date_label"] = datetime.strptime(
            booking["booking_date"], "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
        booking["status_label"] = "ساري" if booking["status"] == "active" else "ملغى"
        bookings.append(booking)
    bookings.sort(
        key=lambda item: (
            item["booking_date"],
            cycle_offset(item["start_time"], cycle_start_minutes),
            item["id"],
        ),
        reverse=True,
    )

    summary = {
        "booking_count": len(bookings),
        "total_amount": sum(item["total"] for item in bookings),
        "total_paid": sum(item["paid"] for item in bookings),
        "total_remaining": sum(item["remaining"] for item in bookings),
    }
    deposit_summary = db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN transaction_type = 'collect' THEN amount ELSE 0 END), 0) AS collected,
               COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount ELSE 0 END), 0) AS refunded
        FROM recurring_deposit_transactions AS t
        JOIN recurring_booking_rules AS r ON r.id = t.rule_id
        WHERE r.client_id = ?
        """, (client_id,)
    ).fetchone()
    deposit_transactions = db.execute(
        """
        SELECT t.*, b.booking_date, b.start_time
        FROM recurring_deposit_transactions AS t
        JOIN recurring_booking_rules AS r ON r.id = t.rule_id
        LEFT JOIN bookings AS b ON b.id = t.booking_id
        WHERE r.client_id = ? ORDER BY t.created_at DESC, t.id DESC
        """, (client_id,)
    ).fetchall()
    deposit_summary = dict(deposit_summary)
    deposit_summary["held"] = deposit_summary["collected"] - deposit_summary["refunded"]
    return render_template(
        "client_detail.html",
        client=client,
        bookings=bookings,
        summary=summary,
        deposit_summary=deposit_summary,
        deposit_transactions=deposit_transactions,
    )


@app.route("/admin/clients/new", methods=["GET", "POST"])
@login_required
def client_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        notes = request.form.get("notes", "").strip()
        if not name or not phone:
            flash("الاسم ورقم الهاتف مطلوبان.", "error")
            return render_template("client_form.html", client=None)
        db = get_db()
        db.execute("INSERT INTO clients (name, phone, notes) VALUES (?, ?, ?)", (name, phone, notes))
        db.commit()
        flash("تمت إضافة العميل بنجاح.", "success")
        return redirect(url_for("clients_list"))
    return render_template("client_form.html", client=None)


@app.route("/admin/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def client_edit(client_id):
    db = get_db()
    client = db.execute("SELECT id, name, phone, notes FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        flash("العميل غير موجود.", "error")
        return redirect(url_for("clients_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        notes = request.form.get("notes", "").strip()
        if not name or not phone:
            flash("الاسم ورقم الهاتف مطلوبان.", "error")
            return render_template("client_form.html", client={"id": client_id, "name": name, "phone": phone, "notes": notes})
        db.execute(
            "UPDATE clients SET name = ?, phone = ?, notes = ? WHERE id = ?",
            (name, phone, notes, client_id),
        )
        db.commit()
        flash("تم تعديل بيانات العميل.", "success")
        return redirect(url_for("clients_list"))
    return render_template("client_form.html", client=client)


@app.route("/admin/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def client_delete(client_id):
    db = get_db()
    has_booking = db.execute("SELECT 1 FROM bookings WHERE client_id = ? LIMIT 1", (client_id,)).fetchone()
    if has_booking is not None:
        flash("لا يمكن حذف عميل مرتبط بحجز مسجل.", "error")
        return redirect(url_for("clients_list"))
    db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    db.commit()
    flash("تم حذف العميل.", "success")
    return redirect(url_for("clients_list"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
