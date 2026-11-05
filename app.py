"""Система бронирования спортивных площадок — MVP на Flask."""
import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import SPORT_TYPES, close_db, get_db, init_db

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.teardown_appcontext(close_db)

BOOKING_DAYS_AHEAD = 14  # на сколько дней вперёд можно бронировать


# ---------- Вспомогательное ----------

@app.before_request
def load_current_user():
    """Перед каждым запросом достаём текущего пользователя из сессии в g.user."""
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Войдите, чтобы продолжить.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def slot_start(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def slot_label(hour):
    return f"{hour:02d}:00–{hour + 1:02d}:00"


def get_venue_or_404(venue_id):
    venue = get_db().execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
    if venue is None:
        abort(404)
    return venue


@app.template_filter("ru_date")
def ru_date(value):
    d = value if isinstance(value, date) else parse_date(value)
    return d.strftime("%d.%m.%Y") if d else value


@app.context_processor
def inject_helpers():
    return {"slot_label": slot_label, "current_user": g.get("user")}


# ---------- Каталог ----------

@app.route("/")
def index():
    sport = request.args.get("sport", "")
    sql = "SELECT * FROM venues WHERE is_active = 1"
    params = []
    if sport:
        sql += " AND sport_type = ?"
        params.append(sport)
    venues = get_db().execute(sql + " ORDER BY name", params).fetchall()
    return render_template("index.html", venues=venues, sport_types=SPORT_TYPES, selected_sport=sport)


@app.route("/venue/<int:venue_id>")
def venue_detail(venue_id):
    venue = get_venue_or_404(venue_id)
    if not venue["is_active"] and not (g.user and g.user["is_admin"]):
        abort(404)

    today = date.today()
    selected = parse_date(request.args.get("date")) or today
    if selected < today or selected > today + timedelta(days=BOOKING_DAYS_AHEAD):
        selected = today

    rows = get_db().execute(
        "SELECT hour FROM bookings WHERE venue_id = ? AND date = ? AND status = 'active'",
        (venue_id, selected.isoformat()),
    ).fetchall()
    booked = {r["hour"] for r in rows}

    now = datetime.now()
    slots = []
    for h in range(venue["open_hour"], venue["close_hour"]):
        if h in booked:
            state = "booked"
        elif slot_start(selected, h) <= now:
            state = "past"
        else:
            state = "free"
        slots.append({"hour": h, "label": slot_label(h), "state": state})

    days = [today + timedelta(days=i) for i in range(BOOKING_DAYS_AHEAD + 1)]
    return render_template("venue.html", venue=venue, slots=slots, selected=selected, days=days)


# ---------- Бронирование ----------

@app.route("/venue/<int:venue_id>/book", methods=["POST"])
@login_required
def book(venue_id):
    venue = get_venue_or_404(venue_id)
    booking_date = parse_date(request.form.get("date"))
    try:
        hour = int(request.form.get("hour", ""))
    except ValueError:
        hour = None

    back = redirect(url_for("venue_detail", venue_id=venue_id,
                            date=(booking_date or date.today()).isoformat()))

    if not venue["is_active"]:
        flash("Площадка сейчас недоступна для бронирования.", "danger")
        return back
    if booking_date is None or hour is None or not (venue["open_hour"] <= hour < venue["close_hour"]):
        flash("Некорректные дата или время.", "danger")
        return back
    if booking_date > date.today() + timedelta(days=BOOKING_DAYS_AHEAD):
        flash(f"Бронировать можно не дальше чем на {BOOKING_DAYS_AHEAD} дней вперёд.", "danger")
        return back
    if slot_start(booking_date, hour) <= datetime.now():
        flash("Нельзя забронировать время в прошлом.", "danger")
        return back

    db = get_db()
    try:
        db.execute(
            "INSERT INTO bookings (user_id, venue_id, date, hour, price) VALUES (?, ?, ?, ?, ?)",
            (g.user["id"], venue_id, booking_date.isoformat(), hour, venue["price_per_hour"]),
        )
        db.commit()
    except sqlite3.IntegrityError:  # сработал уникальный индекс — слот уже занят
        flash("Этот слот уже занят.", "danger")
        return back

    flash(f"Готово! {venue['name']}, {booking_date:%d.%m.%Y} {slot_label(hour)}.", "success")
    return back


@app.route("/my-bookings")
@login_required
def my_bookings():
    rows = get_db().execute(
        """SELECT b.*, v.name AS venue_name, v.address
           FROM bookings b JOIN venues v ON v.id = b.venue_id
           WHERE b.user_id = ? ORDER BY b.date DESC, b.hour DESC""",
        (g.user["id"],),
    ).fetchall()
    now = datetime.now()
    bookings = [
        dict(r, is_future=slot_start(parse_date(r["date"]), r["hour"]) > now) for r in rows
    ]
    return render_template("my_bookings.html", bookings=bookings)


@app.route("/booking/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        abort(404)
    is_admin = bool(g.user["is_admin"])
    if booking["user_id"] != g.user["id"] and not is_admin:
        abort(403)

    if booking["status"] != "active":
        flash("Бронь уже отменена.", "warning")
    elif slot_start(parse_date(booking["date"]), booking["hour"]) <= datetime.now() and not is_admin:
        flash("Нельзя отменить бронь, которая уже началась или прошла.", "danger")
    else:
        db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        db.commit()
        flash("Бронь отменена.", "success")
    next_url = request.form.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("my_bookings")
    return redirect(next_url)


# ---------- Регистрация и вход ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        if not name or "@" not in email or len(password) < 6:
            flash("Заполните все поля корректно, пароль — минимум 6 символов.", "danger")
        elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            flash("Пользователь с таким email уже существует.", "danger")
        else:
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (name, email, generate_password_hash(password)),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            flash("Регистрация прошла успешно!", "success")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
        flash("Неверный email или пароль.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Админ-панель ----------

@app.route("/admin/venues")
@admin_required
def admin_venues():
    venues = get_db().execute("SELECT * FROM venues ORDER BY id").fetchall()
    return render_template("admin/venues.html", venues=venues)


def venue_from_form():
    """Читает форму площадки. Возвращает (данные, ошибка)."""
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "sport_type": f.get("sport_type", ""),
        "address": f.get("address", "").strip(),
        "description": f.get("description", "").strip(),
        "price_per_hour": f.get("price_per_hour", ""),
        "open_hour": f.get("open_hour", ""),
        "close_hour": f.get("close_hour", ""),
        "is_active": 1 if f.get("is_active") == "on" else 0,
    }
    try:
        for key in ("price_per_hour", "open_hour", "close_hour"):
            data[key] = int(data[key])
    except ValueError:
        return data, "Цена и часы работы должны быть целыми числами."
    if not data["name"] or not data["address"]:
        return data, "Название и адрес обязательны."
    if data["sport_type"] not in SPORT_TYPES:
        return data, "Выберите вид спорта из списка."
    if data["price_per_hour"] <= 0:
        return data, "Цена должна быть больше нуля."
    if not (0 <= data["open_hour"] < data["close_hour"] <= 24):
        return data, "Часы работы: 0 ≤ открытие < закрытие ≤ 24."
    return data, None


@app.route("/admin/venues/new", methods=["GET", "POST"])
@admin_required
def admin_venue_new():
    venue = {"name": "", "sport_type": SPORT_TYPES[0], "address": "", "description": "",
             "price_per_hour": "", "open_hour": 8, "close_hour": 22, "is_active": 1}
    if request.method == "POST":
        venue, error = venue_from_form()
        if error:
            flash(error, "danger")
        else:
            db = get_db()
            db.execute(
                """INSERT INTO venues (name, sport_type, address, description,
                       price_per_hour, open_hour, close_hour, is_active)
                   VALUES (:name, :sport_type, :address, :description,
                       :price_per_hour, :open_hour, :close_hour, :is_active)""",
                venue,
            )
            db.commit()
            flash("Площадка добавлена.", "success")
            return redirect(url_for("admin_venues"))
    return render_template("admin/venue_form.html", venue=venue, sport_types=SPORT_TYPES, is_new=True)


@app.route("/admin/venues/<int:venue_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_venue_edit(venue_id):
    venue = dict(get_venue_or_404(venue_id))
    if request.method == "POST":
        data, error = venue_from_form()
        if error:
            flash(error, "danger")
            venue.update(data)
        else:
            data["id"] = venue_id
            db = get_db()
            db.execute(
                """UPDATE venues SET name = :name, sport_type = :sport_type, address = :address,
                       description = :description, price_per_hour = :price_per_hour,
                       open_hour = :open_hour, close_hour = :close_hour, is_active = :is_active
                   WHERE id = :id""",
                data,
            )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("admin_venues"))
    return render_template("admin/venue_form.html", venue=venue, sport_types=SPORT_TYPES, is_new=False)


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    venue_id = request.args.get("venue_id", type=int)
    status = request.args.get("status", "")
    sql = """SELECT b.*, v.name AS venue_name, u.name AS user_name, u.email AS user_email
             FROM bookings b
             JOIN venues v ON v.id = b.venue_id
             JOIN users  u ON u.id = b.user_id
             WHERE 1 = 1"""
    params = []
    if venue_id:
        sql += " AND b.venue_id = ?"
        params.append(venue_id)
    if status in ("active", "cancelled"):
        sql += " AND b.status = ?"
        params.append(status)
    db = get_db()
    bookings = db.execute(sql + " ORDER BY b.date DESC, b.hour DESC", params).fetchall()
    revenue = sum(b["price"] for b in bookings if b["status"] == "active")
    venues = db.execute("SELECT id, name FROM venues ORDER BY name").fetchall()
    return render_template("admin/bookings.html", bookings=bookings, venues=venues,
                           venue_id=venue_id, status=status, revenue=revenue)


# ---------- Ошибки ----------

@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, message="Доступ запрещён."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, message="Страница не найдена."), 404


init_db()

if __name__ == "__main__":
    app.run(debug=True)
