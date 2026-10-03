import os
import sqlite3
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "instance" / "stadium.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key")
WORK_START_HOUR = int(os.getenv("BOOKING_START_HOUR", "8"))
WORK_END_HOUR = int(os.getenv("BOOKING_END_HOUR", "23"))
HOURLY_RATE = int(os.getenv("HOURLY_RATE", "10000"))

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET_KEY, DATABASE=str(DATABASE_PATH))


def get_db():
    if "db" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


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


@app.route("/admin/booking", methods=["GET"])
@login_required
def booking_screen():
    today = datetime.now().date()
    days = []
    arabic_days = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    for offset in range(7):
        current = today + timedelta(days=offset)
        days.append({
            "date": current.isoformat(),
            "day_name": arabic_days[current.weekday()],
            "date_label": current.strftime("%d/%m/%Y"),
            "is_today": offset == 0,
        })

    selected_date = request.args.get("date", today.isoformat())
    valid_dates = {day["date"] for day in days}
    if selected_date not in valid_dates:
        selected_date = today.isoformat()

    slots = [
        {"start": f"{hour:02d}:00", "end": f"{hour + 1:02d}:00"}
        for hour in range(WORK_START_HOUR, WORK_END_HOUR)
    ]
    return render_template(
        "booking_screen.html",
        days=days,
        selected_date=selected_date,
        slots=slots,
        hourly_rate=HOURLY_RATE,
    )


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
