import calendar
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "instance" / "stadium.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key")
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
    "background_color": "#f3f6fb",
}

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET_KEY, DATABASE=str(DATABASE_PATH))


class BookingConflict(ValueError):
    """Raised when a booking overlaps another active booking."""


class PaymentError(ValueError):
    """Raised when a payment cannot be recorded for a booking."""


def get_db():
    if "db" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(app.config["DATABASE"])
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
    moment = moment or datetime.now()
    settings = settings or get_app_settings()
    business_date = moment.date()
    if moment.hour * 60 + moment.minute < clock_minutes(settings["work_start"]):
        business_date -= timedelta(days=1)
    return business_date


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    admin = db.execute("SELECT id FROM admins WHERE username = ?", ("admin",)).fetchone()
    if admin is None:
        db.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            ("admin", generate_password_hash("admin123")),
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
          AND b.booking_date >= ?
          AND b.booking_date <= ?
        """,
        (start_date, end_date),
    ).fetchone()
    return dict(row)


def render_booking_form(booking=None, booking_id=None, form_values=None, error=None, status=200):
    settings = get_app_settings()
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

    return render_template(
        "booking_form.html",
        booking_id=booking_id,
        form=values,
        end_time=end_time,
        total=total,
        already_paid=already_paid,
        remaining_preview=remaining_preview,
        payment_status_preview=payment_status_preview,
        start_time_options=start_time_options,
        window_hours=window_hours,
        cycle_start=settings["work_start"],
        clients=get_booking_clients(),
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
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = get_db().execute(
            "SELECT id, username, password_hash FROM admins WHERE username = ?",
            (username,),
        ).fetchone()
        if admin is not None and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            return redirect(url_for("admin_home"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@app.route("/admin", methods=["GET"])
@login_required
def admin_home():
    return render_template("admin_home.html", username=session.get("admin_username"))


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
        "background_color": request.form.get("background_color", "").strip(),
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
               for key in ("primary_color", "secondary_color", "background_color")):
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
        f"  --bg: {safe_theme_color(settings, 'background_color')};\n"
        "}\n"
    )
    response = Response(css, mimetype="text/css")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/reports", methods=["GET"])
@login_required
def admin_reports():
    settings = get_app_settings()
    stadium_today = stadium_business_date(datetime.now(), settings)
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
        custom_summary=custom_summary,
        custom_label=custom_label,
        custom_start=custom_start or month_start.isoformat(),
        custom_end=custom_end or stadium_today.isoformat(),
        error=error,
    ), 400 if error else 200


@app.route("/admin/booking", methods=["GET"])
@login_required
def booking_screen():
    settings = get_app_settings()
    today = stadium_business_date(settings=settings)
    try:
        selected_day = datetime.strptime(
            request.args.get("date", today.isoformat()), "%Y-%m-%d"
        ).date()
    except ValueError:
        selected_day = today

    month_start = selected_day.replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_names = [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
    ]
    weekday_names = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]
    calendar_weeks = []
    for week in calendar.Calendar(firstweekday=6).monthdayscalendar(selected_day.year, selected_day.month):
        calendar_week = []
        for day_number in week:
            if day_number == 0:
                calendar_week.append(None)
                continue
            calendar_day = selected_day.replace(day=day_number)
            calendar_week.append({
                "date": calendar_day.isoformat(),
                "day_number": day_number,
                "is_selected": calendar_day == selected_day,
                "is_today": calendar_day == today,
            })
        calendar_weeks.append(calendar_week)

    active_bookings = get_db().execute(
        """
        SELECT b.*, c.name AS client_name,
               COALESCE((SELECT SUM(p.amount) FROM booking_payments AS p WHERE p.booking_id = b.id), 0) AS paid
        FROM bookings AS b
        JOIN clients AS c ON c.id = b.client_id
        WHERE b.booking_date = ? AND b.status = 'active'
        ORDER BY b.start_time
        """,
        (selected_day.isoformat(),),
    ).fetchall()
    cycle_start_minutes, window_minutes = work_window(settings)
    window_hours = window_minutes // 60
    booking_summaries = []
    for row in active_bookings:
        booking = dict(row)
        booking["cycle_start_offset"] = cycle_offset(booking["start_time"], cycle_start_minutes) // 60
        booking["remaining"] = booking["total"] - booking["paid"]
        booking["payment_status"] = (
            "مدفوع بالكامل" if booking["paid"] == booking["total"] else "عربون"
        )
        booking_summaries.append(booking)
    booking_summaries.sort(key=lambda item: item["cycle_start_offset"])
    active_bookings = booking_summaries
    slots = []
    for slot_offset in range(window_hours):
        slot_start = format_clock(cycle_start_minutes + slot_offset * 60)
        slot_end = format_clock(cycle_start_minutes + (slot_offset + 1) * 60)
        booking = next(
            (
                item for item in active_bookings
                if item["cycle_start_offset"] < slot_offset + 1
                and item["cycle_start_offset"] + item["duration_hours"] > slot_offset
            ),
            None,
        )
        slots.append({"start": slot_start, "end": slot_end, "booking": booking})

    cycle_end_minutes = cycle_start_minutes + window_minutes
    work_end_label = format_clock(cycle_end_minutes)
    work_end_day = "اليوم التالي" if cycle_end_minutes >= 24 * 60 else "اليوم نفسه"

    return render_template(
        "booking_screen.html",
        calendar_weeks=calendar_weeks,
        weekday_names=weekday_names,
        month_label=f"{month_names[selected_day.month - 1]} {selected_day.year}",
        previous_month=previous_month.isoformat(),
        next_month=next_month.isoformat(),
        selected_date=selected_day.isoformat(),
        selected_date_label=selected_day.strftime("%d/%m/%Y"),
        slots=slots,
        booking_summaries=booking_summaries,
        hourly_rate=int(settings["hourly_rate"]),
        work_start=settings["work_start"],
        work_end_label=work_end_label,
        work_end_day=work_end_day,
        work_window_hours=window_hours,
    )


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
    return redirect(url_for("booking_screen", date=values["booking_date"]))


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
    return redirect(url_for("booking_screen", date=values["booking_date"]))


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
    return redirect(url_for("booking_screen", date=booking["booking_date"]))


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
    return render_template(
        "client_detail.html",
        client=client,
        bookings=bookings,
        summary=summary,
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
