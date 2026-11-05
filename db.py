"""Работа с базой данных SQLite: подключение, создание таблиц, тестовые данные."""
import os
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "booking.db")

SPORT_TYPES = ["Футбол", "Баскетбол", "Волейбол", "Теннис", "Бадминтон", "Хоккей", "Другое"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS venues (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    sport_type     TEXT    NOT NULL,
    address        TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    price_per_hour INTEGER NOT NULL CHECK (price_per_hour > 0),
    open_hour      INTEGER NOT NULL DEFAULT 8,
    close_hour     INTEGER NOT NULL DEFAULT 22,
    is_active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    venue_id   INTEGER NOT NULL REFERENCES venues(id),
    date       TEXT    NOT NULL,              -- 'YYYY-MM-DD'
    hour       INTEGER NOT NULL,              -- слот hour:00–hour+1:00
    price      INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active',  -- active / cancelled
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Защита от двойной брони на уровне БД: один активный слот на площадку
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_slot
    ON bookings (venue_id, date, hour) WHERE status = 'active';
"""

SEED_VENUES = [
    ("Арена «Север»", "Футбол", "Мурино, ул. Шоссе в Лаврики, 10",
     "Мини-футбольное поле с искусственным газоном, раздевалки, душ.", 3500, 8, 23),
    ("Спортзал «Атлант»", "Баскетбол", "СПб, пр. Просвещения, 45",
     "Крытый зал, паркет, электронное табло.", 2500, 8, 22),
    ("Корт «Эйс»", "Теннис", "СПб, Крестовский остров, 3",
     "Хардовый корт, аренда ракеток.", 1800, 7, 22),
    ("Зал «Песок»", "Волейбол", "СПб, ул. Савушкина, 112",
     "Крытый зал для пляжного волейбола.", 2200, 10, 22),
]


def get_db():
    """Одно подключение на запрос; хранится в flask.g."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row  # строки как словари: row["name"]
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Создаёт таблицы и наполняет тестовыми данными при первом запуске."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users WHERE email = ?", ("admin@example.com",)).fetchone():
        conn.execute(
            "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?, ?, ?, 1)",
            ("Администратор", "admin@example.com", generate_password_hash("admin123")),
        )
    if conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO venues (name, sport_type, address, description, price_per_hour, open_hour, close_hour)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            SEED_VENUES,
        )
    conn.commit()
    conn.close()
