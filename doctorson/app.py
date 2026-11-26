"""DoctorsOn - live doctor availability, ambulance SOS emergency finder, and pharmacy delivery.

Run:  python app.py        (then open http://127.0.0.1:5000)

Environment variables (all optional):
  DOCTORSON_DB                    path to the SQLite file            (default: ./doctorson.db)
  DOCTORSON_TZ                    IANA timezone for "now"            (default: Asia/Kolkata)
  DOCTORSON_ADMIN_PASSWORD        password for /admin                (default: admin123 - CHANGE IT)
  DOCTORSON_SECRET                Flask session secret key           (default: random per start)
  DOCTORSON_SEED                  "1" loads sample data when empty   (default: 1)
  DOCTORSON_CURRENCY              currency label                     (default: Rs.)
  DOCTORSON_DELIVERY_FEE          pharmacy delivery fee              (default: 50)
  DOCTORSON_FREE_DELIVERY_OVER    subtotal for free delivery         (default: 500)
  DOCTORSON_PHARMACY_WHATSAPP     pharmacy Rx WhatsApp number        (default: +91 90000 00000)
  OPENAI_API_KEY                  optional key for prescription extraction
  DOCTORSON_AI_MODEL              Responses API model (default: gpt-4.1-mini)
  DOCTORSON_RX_DISCOUNT_PERCENT   prescription offer cap (default: 15; max: 15)
  DOCTORSON_PRESCRIPTION_DIR      private prescription storage directory
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DOCTORSON_DB", os.path.join(BASE_DIR, "doctorson.db"))
# Windows may not include the IANA timezone database. India does not observe
# daylight-saving time, so use a fixed UTC+05:30 fallback if tzdata is absent.
_TZ_NAME = os.environ.get("DOCTORSON_TZ", "Asia/Kolkata")
try:
    TZ = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    if _TZ_NAME == "Asia/Kolkata":
        TZ = timezone(timedelta(hours=5, minutes=30), name="IST")
    else:
        raise RuntimeError(
            f"Timezone '{_TZ_NAME}' was not found. Install timezone data with: "
            "python -m pip install tzdata"
        )
ADMIN_PASSWORD = os.environ.get("DOCTORSON_ADMIN_PASSWORD", "admin123")
TIME_ZONE_LABEL = ("Tamil Nadu, India · India Standard Time (IST · UTC+05:30 · Asia/Kolkata)" if _TZ_NAME == "Asia/Kolkata" else f"Configured time zone ({_TZ_NAME})")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("DOCTORSON_AI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
PRESCRIPTION_DIR = os.environ.get(
    "DOCTORSON_PRESCRIPTION_DIR", os.path.join(BASE_DIR, "instance", "prescriptions")
)
PRESCRIPTION_MAX_BYTES = 8 * 1024 * 1024
ALLOWED_PRESCRIPTION_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
try:
    PHARMACY_RX_DISCOUNT_PERCENT = min(15.0, max(0.0, float(os.environ.get("DOCTORSON_RX_DISCOUNT_PERCENT", "15"))))
except ValueError:
    PHARMACY_RX_DISCOUNT_PERCENT = 15.0

CURRENCY = os.environ.get("DOCTORSON_CURRENCY", "Rs.")
PHARMACY_DELIVERY_FEE = float(os.environ.get("DOCTORSON_DELIVERY_FEE", "50"))
PHARMACY_FREE_DELIVERY_OVER = float(os.environ.get("DOCTORSON_FREE_DELIVERY_OVER", "500"))
PHARMACY_WHATSAPP = os.environ.get("DOCTORSON_PHARMACY_WHATSAPP", "+91 90000 00000")
AMBULANCE_NUMBER = os.environ.get("DOCTORSON_AMBULANCE_NUMBER", "108")
EMERGENCY_WHATSAPP = os.environ.get("DOCTORSON_EMERGENCY_WHATSAPP", "")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ORDER_STATUSES = ["Pending", "Confirmed", "Out for Delivery", "Delivered", "Cancelled"]
PAYMENT_METHODS = ["Cash on Delivery", "UPI / Card on Delivery"]

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
END_TIME_RE = re.compile(r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$")

app = Flask(__name__)
app.secret_key = os.environ.get("DOCTORSON_SECRET") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = PRESCRIPTION_MAX_BYTES + (512 * 1024)

# Simple in-memory brute-force protection for /admin/login: {ip: (fail_count, window_start)}
_LOGIN_FAILS = {}
_LOGIN_WINDOW_SEC = 60
_LOGIN_MAX_FAILS = 10

# --------------------------------------------------------------------------- #
# Database & Migrations
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS hospitals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    city            TEXT NOT NULL DEFAULT '',
    address         TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    emergency_phone TEXT NOT NULL DEFAULT '',
    has_er          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS doctors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    degree         TEXT NOT NULL DEFAULT '',
    speciality     TEXT NOT NULL,
    phone          TEXT NOT NULL DEFAULT '',
    whatsapp       TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    bio            TEXT NOT NULL DEFAULT '',
    is_emergency   INTEGER NOT NULL DEFAULT 0,
    emergency_note TEXT NOT NULL DEFAULT '',
    pharmacy_discount_eligible INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id   INTEGER NOT NULL REFERENCES doctors(id)   ON DELETE CASCADE,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0 = Monday
    start_time  TEXT NOT NULL,                                     -- 'HH:MM' 24h
    end_time    TEXT NOT NULL,                                     -- 'HH:MM' or '24:00'
    CHECK (end_time > start_time)
);
CREATE TABLE IF NOT EXISTS medicines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    generic_name TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'General',
    pack_size    TEXT NOT NULL DEFAULT '',
    price        REAL NOT NULL CHECK (price >= 0),
    stock        INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    requires_rx  INTEGER NOT NULL DEFAULT 0,
    description  TEXT NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pharmacy_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code     TEXT NOT NULL UNIQUE,
    customer_name  TEXT NOT NULL,
    phone          TEXT NOT NULL,
    whatsapp       TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL,
    city           TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT 'Cash on Delivery',
    has_rx         INTEGER NOT NULL DEFAULT 0,
    rx_reference   TEXT NOT NULL DEFAULT '',
    delivery_fee   REAL NOT NULL DEFAULT 0,
    subtotal       REAL NOT NULL DEFAULT 0,
    total          REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'Pending',
    created_at     TEXT NOT NULL,
    rx_filename    TEXT NOT NULL DEFAULT '',
    rx_storage_key TEXT NOT NULL DEFAULT '',
    rx_doctor_name TEXT NOT NULL DEFAULT '',
    rx_date        TEXT NOT NULL DEFAULT '',
    rx_doctor_verified INTEGER NOT NULL DEFAULT 0,
    rx_date_valid  INTEGER NOT NULL DEFAULT 0,
    rx_ai_status   TEXT NOT NULL DEFAULT '',
    rx_ai_summary  TEXT NOT NULL DEFAULT '',
    discount_percent REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pharmacy_order_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES pharmacy_orders(id) ON DELETE CASCADE,
    medicine_id   INTEGER REFERENCES medicines(id) ON DELETE SET NULL,
    medicine_name TEXT NOT NULL,
    pack_size     TEXT NOT NULL DEFAULT '',
    unit_price    REAL NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    line_total    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_doctor   ON schedules(doctor_id);
CREATE INDEX IF NOT EXISTS idx_sched_hospital ON schedules(hospital_id);
CREATE INDEX IF NOT EXISTS idx_order_code     ON pharmacy_orders(order_code);
CREATE INDEX IF NOT EXISTS idx_order_items    ON pharmacy_order_items(order_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_column(db, table, col, ddl):
    cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def seed_doctors_and_hospitals(db):
    """Sample data. Every name, number and address below is FICTIONAL."""
    hospitals = [
        ("Lakeside General Hospital", "Sample City", "12 Lake Road",
         "+94 11 000 0101", "+94 11 000 0911", 1),
        ("Greenfield Medical Centre", "Sample City", "45 Green Avenue",
         "+94 11 000 0102", "+94 11 000 0912", 1),
        ("Riverside Children's Hospital", "Sample City", "8 River Street",
         "+94 11 000 0103", "+94 11 000 0913", 1),
    ]
    db.executemany(
        "INSERT INTO hospitals(name, city, address, phone, emergency_phone, has_er) "
        "VALUES (?,?,?,?,?,?)", hospitals)

    doctors = [
        ("Dr. Anika Perera", "MBBS, MD (Cardiology)", "Cardiology",
         "+94 77 000 0001", "+94 77 000 0001", "anika@example.com",
         "Heart-health specialist focused on prevention and rehabilitation.",
         1, "Cardiac ER & Chest Pain Unit"),
        ("Dr. Rohan Fernando", "MBBS, MD (Paediatrics), DCH", "Paediatrics",
         "+94 77 000 0002", "+94 77 000 0002", "rohan@example.com",
         "Child health, vaccinations and growth monitoring.",
         1, "Paediatric Trauma & Neonatal ER"),
        ("Dr. Meera Krishnan", "MBBS, MD (Dermatology)", "Dermatology",
         "+94 77 000 0003", "+94 77 000 0003", "meera@example.com",
         "Skin, hair and nail conditions.",
         0, ""),
        ("Dr. Samuel Jayasuriya", "MBBS, MRCGP", "General Medicine",
         "+94 77 000 0004", "+94 77 000 0004", "samuel@example.com",
         "Family doctor and acute emergency triage specialist.",
         1, "24h Acute Triage & Ambulance Stabilization"),
        ("Dr. Priya Nair", "MBBS, MS (OBG)", "Gynaecology",
         "+94 77 000 0005", "+94 77 000 0005", "priya@example.com",
         "Women's health and antenatal care.",
         1, "Obstetric & Labour Emergency"),
        ("Dr. Kavindu Silva", "MBBS, MS (Orthopaedics)", "Orthopaedics",
         "+94 77 000 0006", "+94 77 000 0006", "kavindu@example.com",
         "Joint, bone and sports injuries.",
         1, "Road Trauma, Fractures & Spine ER"),
        ("Dr. Fatima Hassan", "MBBS, MS (ENT)", "ENT",
         "+94 77 000 0007", "", "fatima@example.com",
         "Ear, nose and throat care for adults and children.",
         0, ""),
        ("Dr. David Thomas", "MBBS, DM (Neurology)", "Neurology",
         "+94 77 000 0008", "+94 77 000 0008", "david@example.com",
         "Headache, epilepsy and stroke follow-up.",
         1, "Acute Stroke & Head Injury Protocol"),
    ]
    db.executemany(
        "INSERT INTO doctors(name, degree, speciality, phone, whatsapp, email, bio, "
        "is_emergency, emergency_note) VALUES (?,?,?,?,?,?,?,?,?)", doctors)

    MON_FRI, MON_SAT, ALL = range(0, 5), range(0, 6), range(0, 7)
    # (doctor_id, hospital_id, weekdays, start, end)
    plan = [
        (1, 1, MON_FRI, "09:00", "13:00"), (1, 2, [5], "10:00", "14:00"),
        (2, 3, MON_SAT, "08:00", "16:00"),
        (3, 2, [0, 2, 4], "14:00", "19:00"),
        (4, 1, ALL, "18:00", "22:00"), (4, 1, ALL, "00:00", "06:00"),
        (4, 2, MON_FRI, "08:00", "12:00"),
        (5, 1, [1, 3], "10:00", "15:00"), (5, 3, [0, 2], "09:00", "12:00"),
        (6, 2, MON_FRI, "15:00", "20:00"), (6, 2, ALL, "20:00", "24:00"),
        (7, 3, range(1, 6), "09:00", "17:00"),
        (8, 1, [0, 3], "13:00", "17:00"),
    ]
    rows = [(d, h, wd, s, e) for d, h, days, s, e in plan for wd in days]
    db.executemany(
        "INSERT INTO schedules(doctor_id, hospital_id, weekday, start_time, end_time) "
        "VALUES (?,?,?,?,?)", rows)


def seed_medicines(db):
    """Fictional sample pharmacy catalog for home delivery."""
    meds = [
        ("Paracetamol 500mg", "Paracetamol", "Pain & Fever", "Strip of 15 tablets",
         35.0, 120, 0, "Relief for fever, headache, and mild body aches."),
        ("Ibuprofen 400mg", "Ibuprofen", "Pain & Fever", "Strip of 10 tablets",
         48.0, 80, 0, "Anti-inflammatory pain relief for muscle and joint pain."),
        ("ORS Rehydration Salts", "Oral Rehydration Salts", "Gastro", "Pack of 5 sachets",
         75.0, 95, 0, "Electrolyte hydration solution for dehydration and recovery."),
        ("Cetirizine 10mg", "Cetirizine Hydrochloride", "Allergy & Cold", "Strip of 10 tablets",
         42.0, 110, 0, "Non-drowsy relief for sneezing, allergies, and hives."),
        ("Antiseptic First Aid Kit", "Povidone-Iodine + Gauze + Bandages", "First Aid", "1 compact box",
         290.0, 30, 0, "Essential wound dressing, antiseptic solution, and sterile swabs."),
        ("Digital Blood Pressure Monitor", "Automated Upper-Arm Cuff", "First Aid", "1 unit",
         1850.0, 12, 0, "Home blood pressure and pulse monitor with memory."),
        ("Amoxicillin 500mg", "Amoxicillin Trihydrate", "Antibiotics", "Strip of 10 capsules",
         145.0, 60, 1, "Broad-spectrum antibiotic. Valid doctor prescription required."),
        ("Atorvastatin 10mg", "Atorvastatin Calcium", "Cardiac & BP", "Strip of 15 tablets",
         165.0, 55, 1, "Cholesterol and heart-care medication. Prescription required."),
        ("Metformin 500mg SR", "Metformin Hydrochloride", "Diabetes", "Strip of 15 tablets",
         95.0, 70, 1, "Sustained-release blood glucose control. Prescription required."),
        ("Salbutamol Inhaler 100mcg", "Salbutamol Sulphate", "Respiratory", "200-dose inhaler",
         240.0, 25, 1, "Fast-acting bronchodilator inhaler. Prescription required."),
        ("Omeprazole 20mg", "Omeprazole", "Gastro", "Strip of 14 capsules",
         85.0, 90, 0, "Acidity, heartburn, and gastric relief capsules."),
        ("Vitamin D3 + Calcium", "Cholecalciferol & Calcium", "Vitamins", "Bottle of 30 tablets",
         220.0, 45, 0, "Daily bone and immunity support supplement."),
    ]
    db.executemany(
        "INSERT INTO medicines(name, generic_name, category, pack_size, price, stock, "
        "requires_rx, description) VALUES (?,?,?,?,?,?,?,?)", meds)


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)

    # Idempotent migrations for existing databases
    _ensure_column(db, "hospitals", "emergency_phone", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "hospitals", "has_er", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db, "doctors", "degree", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "doctors", "whatsapp", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "doctors", "is_emergency", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "doctors", "emergency_note", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "doctors", "pharmacy_discount_eligible", "INTEGER NOT NULL DEFAULT 0")

    # Prescription verification and discount audit fields are additive migrations.
    _ensure_column(db, "pharmacy_orders", "rx_filename", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "rx_storage_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "rx_doctor_name", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "rx_date", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "rx_doctor_verified", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "pharmacy_orders", "rx_date_valid", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "pharmacy_orders", "rx_ai_status", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "rx_ai_summary", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "pharmacy_orders", "discount_percent", "REAL NOT NULL DEFAULT 0")
    _ensure_column(db, "pharmacy_orders", "discount_amount", "REAL NOT NULL DEFAULT 0")

    should_seed = os.environ.get("DOCTORSON_SEED", "1") == "1"
    if should_seed:
        if db.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
            seed_doctors_and_hospitals(db)
        if db.execute("SELECT COUNT(*) FROM medicines").fetchone()[0] == 0:
            seed_medicines(db)

    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
# Template filters & globals
# --------------------------------------------------------------------------- #
def fmt_time(hhmm):
    if hhmm == "24:00":
        return "12:00 AM"
    h, m = map(int, hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


@app.template_filter("tel")
def tel_filter(phone):
    return re.sub(r"[^\d+]", "", phone or "")


@app.template_filter("wa")
def wa_filter(phone):
    return re.sub(r"\D", "", phone or "")


@app.template_filter("money")
def money_filter(amount):
    return f"{CURRENCY} {float(amount or 0):.2f}"


app.add_template_filter(fmt_time, "time12")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals.update(
    csrf_token=csrf_token,
    WEEKDAYS=WEEKDAYS,
    ORDER_STATUSES=ORDER_STATUSES,
    PAYMENT_METHODS=PAYMENT_METHODS,
    CURRENCY=CURRENCY,
    PHARMACY_WHATSAPP=PHARMACY_WHATSAPP,
    PHARMACY_DELIVERY_FEE=PHARMACY_DELIVERY_FEE,
    PHARMACY_FREE_DELIVERY_OVER=PHARMACY_FREE_DELIVERY_OVER,
    TIME_ZONE_LABEL=TIME_ZONE_LABEL,
    AI_PRESCRIPTION_ENABLED=bool(OPENAI_API_KEY),
    PHARMACY_RX_DISCOUNT_PERCENT=PHARMACY_RX_DISCOUNT_PERCENT,
)


@app.context_processor
def inject_nav_context():
    cart = session.get("cart", {})
    cart_count = 0
    if isinstance(cart, dict):
        for v in cart.values():
            try:
                q = int(v)
                if q > 0:
                    cart_count += q
            except (TypeError, ValueError):
                pass
    local_now = now_local()
    return {
        "cart_count": cart_count,
        "app_timezone": TIME_ZONE_LABEL,
        "app_timezone_location": "Tamil Nadu, India" if _TZ_NAME == "Asia/Kolkata" else _TZ_NAME,
        "app_local_clock": local_now.strftime("%d %b %Y · %I:%M %p") + " " + (local_now.tzname() or _TZ_NAME),
    }


# --------------------------------------------------------------------------- #
# Availability logic ("real time")
# --------------------------------------------------------------------------- #
def now_local():
    return datetime.now(TZ)


def _normalise_doctor_name(value):
    """Normalise a displayed doctor name for conservative exact matching."""
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = re.sub(r"\bdr\.?\s*", "", value)
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return " ".join(value.split())


def _match_doctor_record(extracted_name):
    """Return an exact name match from the app directory; never guess/fuzzy-match."""
    key = _normalise_doctor_name(extracted_name)
    if not key:
        return None
    rows = get_db().execute(
        "SELECT id, name, pharmacy_discount_eligible FROM doctors ORDER BY id"
    ).fetchall()
    matches = [row for row in rows if _normalise_doctor_name(row["name"]) == key]
    if not matches:
        return None
    return next((row for row in matches if row["pharmacy_discount_eligible"]), matches[0])


def _prescription_date_check(raw_date, order_day=None):
    """Accept only the local order date or the immediately preceding local date."""
    if not raw_date:
        return False, "unreadable"
    try:
        prescribed = datetime.strptime(str(raw_date).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False, "unreadable"
    day = order_day or now_local().date()
    if prescribed == day:
        return True, "order_date"
    if prescribed == day - timedelta(days=1):
        return True, "previous_day"
    if prescribed > day:
        return False, "future_date"
    return False, "too_old"


def _prescription_mime(ext):
    return {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


def _validate_prescription_file(filename, content):
    ext = os.path.splitext(secure_filename(filename or ""))[1].lower()
    if ext not in ALLOWED_PRESCRIPTION_EXTENSIONS:
        raise ValueError("Use a PDF, JPG, JPEG, PNG or WEBP prescription file.")
    if not content:
        raise ValueError("The selected prescription file is empty.")
    if len(content) > PRESCRIPTION_MAX_BYTES:
        raise ValueError("The prescription file must be 8 MB or smaller.")
    signatures = {
        ".pdf": content.startswith(b"%PDF-"),
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".jpeg": content.startswith(b"\xff\xd8\xff"),
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        ".webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures.get(ext, False):
        raise ValueError("The file content does not match its extension. Please upload the original image or PDF.")
    return ext


def _openai_prescription_extract(file_path, filename, ext):
    """Read prescription text fields using the configured OpenAI Responses API."""
    if not OPENAI_API_KEY:
        raise RuntimeError("AI prescription analysis is not configured. Set OPENAI_API_KEY on the server.")

    with open(file_path, "rb") as handle:
        content = handle.read()
    data_url = "data:" + _prescription_mime(ext) + ";base64," + base64.b64encode(content).decode("ascii")
    if ext == ".pdf":
        file_part = {
            "type": "input_file",
            "filename": filename,
            "file_data": data_url,
            "detail": "high",
        }
    else:
        file_part = {
            "type": "input_image",
            "image_url": data_url,
            "detail": "high",
        }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "doctor_name": {"type": ["string", "null"]},
            "prescription_date": {"type": ["string", "null"]},
            "medicines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "strength": {"type": "string"},
                        "directions_as_written": {"type": "string"},
                    },
                    "required": ["name", "strength", "directions_as_written"],
                },
            },
            "readable": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["doctor_name", "prescription_date", "medicines", "readable", "notes"],
    }
    instruction = (
        "Read this prescription image or PDF and extract only what is visibly written. "
        "Return JSON fields doctor_name, prescription_date, medicines, readable, notes. "
        "The prescription_date must be YYYY-MM-DD only when the full date is clearly legible; "
        "otherwise null. doctor_name must be null if the prescriber's name cannot be read with "
        "reasonable certainty. Do not infer, repair, invent, or guess names, dates, medicines, "
        "strengths, or directions. Transcribe medicine directions_as_written verbatim where legible; "
        "do not interpret them, recommend medicines, calculate doses, diagnose, or advise treatment. "
        "If parts are unclear, state that briefly in notes and set readable=false when the document "
        "cannot be reliably read. The pharmacist must independently review the original prescription."
    )
    payload = {
        "model": OPENAI_MODEL,
        "store": False,
        "input": [{
            "role": "user",
            "content": [file_part, {"type": "input_text", "text": instruction}],
        }],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "prescription_extraction",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 900,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + OPENAI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not expose provider response content or credentials to the customer.
        raise RuntimeError(f"AI provider returned HTTP {exc.code}. Check the API key, model and account limits.") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("The AI service could not be reached. Please retry or request pharmacist review.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("The AI service returned an unreadable response. Please retry or request pharmacist review.") from None

    output_text = result.get("output_text")
    if not output_text:
        fragments = []
        for item in result.get("output", []):
            for part in item.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    fragments.append(part["text"])
        output_text = "\n".join(fragments)
    if not output_text:
        raise RuntimeError("The AI could not read this prescription. Please upload a clearer photo or request pharmacist review.")
    try:
        data = json.loads(output_text)
    except json.JSONDecodeError:
        raise RuntimeError("The AI returned an unreadable extraction. Please retry or request pharmacist review.") from None

    medicines = []
    for medicine in data.get("medicines", [])[:25]:
        if not isinstance(medicine, dict):
            continue
        medicines.append({
            "name": str(medicine.get("name") or "")[:120],
            "strength": str(medicine.get("strength") or "")[:80],
            "directions_as_written": str(medicine.get("directions_as_written") or "")[:180],
        })
    return {
        "doctor_name": str(data.get("doctor_name") or "").strip()[:160],
        "prescription_date": str(data.get("prescription_date") or "").strip()[:10],
        "medicines": medicines,
        "readable": bool(data.get("readable")),
        "notes": str(data.get("notes") or "")[:500],
    }


def _prescription_directory():
    os.makedirs(PRESCRIPTION_DIR, mode=0o700, exist_ok=True)
    # Remove abandoned pre-order uploads after 24 hours. Final order files use DO- codes.
    cutoff = time.time() - (24 * 60 * 60)
    try:
        for name in os.listdir(PRESCRIPTION_DIR):
            if not re.fullmatch(r"[a-f0-9]{32}(?:\.(?:pdf|jpg|jpeg|png|webp|json))?", name):
                continue
            path = os.path.join(PRESCRIPTION_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
    return PRESCRIPTION_DIR


def _create_prescription_upload(file_storage, allow_ai=False):
    """Store upload privately and persist the extracted metadata outside the session cookie."""
    filename = secure_filename(getattr(file_storage, "filename", "") or "prescription")
    content = file_storage.stream.read(PRESCRIPTION_MAX_BYTES + 1)
    ext = _validate_prescription_file(filename, content)
    token = uuid.uuid4().hex
    storage_name = token + ext
    directory = _prescription_directory()
    full_path = os.path.join(directory, storage_name)
    with open(full_path, "xb") as handle:
        handle.write(content)

    extracted = None
    if not OPENAI_API_KEY:
        ai_status = "not_configured"
        ai_message = "AI is not configured; the pharmacist can review this uploaded file manually."
    elif not allow_ai:
        ai_status = "consent_not_given"
        ai_message = "AI analysis was not run because consent was not given. The pharmacist can review this file manually."
    else:
        ai_status = "analysis_failed"
        ai_message = "AI analysis could not be completed. The pharmacist can review this file manually."
    if OPENAI_API_KEY and allow_ai:
        try:
            extracted = _openai_prescription_extract(full_path, filename, ext)
            ai_status = "analysed"
            ai_message = extracted.get("notes") or "Text extraction completed. Pharmacist review is still required."
        except Exception as exc:
            ai_message = str(exc)[:300]

    doctor_name = (extracted or {}).get("doctor_name", "")
    doctor_row = _match_doctor_record(doctor_name)
    doctor_verified = bool(doctor_row and doctor_row["pharmacy_discount_eligible"])
    date_value = (extracted or {}).get("prescription_date", "")
    date_valid, date_status = _prescription_date_check(date_value)
    discount_eligible = bool(ai_status == "analysed" and (extracted or {}).get("readable") and doctor_verified and date_valid)
    if doctor_name and doctor_row is None:
        doctor_status = "not_in_doctorson"
    elif doctor_name and not doctor_verified:
        doctor_status = "listed_not_enrolled_for_offer"
    elif doctor_verified:
        doctor_status = "enrolled"
    else:
        doctor_status = "not_readable"

    metadata = {
        "token": token,
        "filename": filename[:180],
        "storage_name": storage_name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "ai_status": ai_status,
        "ai_message": ai_message,
        "extracted": extracted or {},
        "matched_doctor_name": doctor_row["name"] if doctor_row else "",
        "doctor_verified": doctor_verified,
        "doctor_status": doctor_status,
        "date_valid": date_valid,
        "date_status": date_status,
        "discount_eligible": discount_eligible,
    }
    metadata_path = os.path.join(directory, token + ".json")
    with open(metadata_path, "x", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False)
    return metadata


def _read_prescription_metadata(token):
    if not re.fullmatch(r"[a-f0-9]{32}", str(token or "")):
        return None
    path = os.path.join(_prescription_directory(), token + ".json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("token") != token:
        return None
    return data


def _prescription_response(metadata):
    extracted = metadata.get("extracted") or {}
    return {
        "ok": True,
        "token": metadata["token"],
        "ai_status": metadata.get("ai_status", ""),
        "message": metadata.get("ai_message", ""),
        "doctor_name": extracted.get("doctor_name") or "Not clearly detected",
        "matched_doctor_name": metadata.get("matched_doctor_name") or "",
        "doctor_status": metadata.get("doctor_status", "not_readable"),
        "prescription_date": extracted.get("prescription_date") or "Not clearly detected",
        "date_status": metadata.get("date_status", "unreadable"),
        "doctor_verified": bool(metadata.get("doctor_verified")),
        "date_valid": bool(metadata.get("date_valid")),
        "discount_eligible": bool(metadata.get("discount_eligible")),
        "readable": bool(extracted.get("readable")),
        "discount_percent": PHARMACY_RX_DISCOUNT_PERCENT if metadata.get("discount_eligible") else 0,
        "medicines": extracted.get("medicines", []),
    }


def _escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bounds(slot, day):
    start = datetime.combine(day, datetime.strptime(slot["start_time"], "%H:%M").time(), tzinfo=TZ)
    if slot["end_time"] == "24:00":
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    else:
        end = datetime.combine(day, datetime.strptime(slot["end_time"], "%H:%M").time(), tzinfo=TZ)
    return start, end


def find_current_and_next(slots, now):
    """Return (current_slot, current_slots, next_slot, next_start_datetime) looking 8 days ahead."""
    current_slots, nxt, nxt_start = [], None, None
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        for s in sorted((s for s in slots if s["weekday"] == day.weekday()),
                        key=lambda s: (s["start_time"], s["id"])):
            start, end = _bounds(s, day)
            if offset == 0 and start <= now < end:
                current_slots.append(s)
            elif start > now and (nxt_start is None or start < nxt_start):
                nxt, nxt_start = s, start
    current = current_slots[0] if current_slots else None
    return current, current_slots, nxt, nxt_start


def build_doctor(row, slots, now):
    """Turn a doctor row + its schedule rows into a dict with live status & highlighted hospitals."""
    row = dict(row)
    slots = [dict(s) for s in slots]
    current, current_slots, nxt, nxt_start = find_current_and_next(slots, now)
    current_ids = {s["id"] for s in current_slots}
    current_hospital_ids = {s["hospital_id"] for s in current_slots}

    current_hospital = None
    next_hospital = None

    if current:
        status = "available"
        label = "Available now"
        detail = f"until {fmt_time(current['end_time'])} at {current['hospital_name']}"
        current_hospital = {
            "id": current["hospital_id"],
            "name": current["hospital_name"],
            "city": current.get("hospital_city", ""),
            "address": current.get("hospital_address", ""),
            "phone": current.get("hospital_phone", ""),
            "emergency_phone": current.get("hospital_emergency_phone", ""),
            "end_time": current["end_time"],
        }
    elif nxt:
        next_hospital = {
            "id": nxt["hospital_id"],
            "name": nxt["hospital_name"],
            "city": nxt.get("hospital_city", ""),
            "address": nxt.get("hospital_address", ""),
            "phone": nxt.get("hospital_phone", ""),
            "emergency_phone": nxt.get("hospital_emergency_phone", ""),
            "start_time": nxt["start_time"],
        }
        if nxt_start.date() == now.date():
            status, label = "upcoming", "Starts later today"
            detail = f"{fmt_time(nxt['start_time'])} at {nxt['hospital_name']}"
        else:
            days_ahead = (nxt_start.date() - now.date()).days
            when = "tomorrow" if days_ahead == 1 else WEEKDAYS[nxt_start.weekday()]
            status, label = "off", "Not working now"
            detail = f"next: {when} {fmt_time(nxt['start_time'])} at {nxt['hospital_name']}"
    else:
        status, label, detail = "off", "No schedule listed", ""

    today = sorted((s for s in slots if s["weekday"] == now.weekday()),
                   key=lambda s: (s["start_time"], s["id"]))
    for s in today:
        s["is_current"] = s["id"] in current_ids

    # Weekly schedule grouped by hospital, then by weekday.
    by_hospital = {}
    for s in slots:
        h = by_hospital.setdefault(s["hospital_id"], {
            "id": s["hospital_id"],
            "name": s["hospital_name"],
            "city": s.get("hospital_city", ""),
            "address": s.get("hospital_address", ""),
            "phone": s.get("hospital_phone", ""),
            "emergency_phone": s.get("hospital_emergency_phone", ""),
            "has_er": bool(s.get("hospital_has_er", 1)),
            "is_current": s["hospital_id"] in current_hospital_ids,
            "days": {},
        })
        h["days"].setdefault(s["weekday"], []).append(s)

    hospitals = []
    for h in by_hospital.values():
        h["days"] = [{
            "weekday": WEEKDAYS[wd],
            "is_today": wd == now.weekday(),
            "hours": ", ".join(
                f"{fmt_time(x['start_time'])} - {fmt_time(x['end_time'])}"
                for x in sorted(v, key=lambda x: x["start_time"])
            ),
        } for wd, v in sorted(h["days"].items())]
        hospitals.append(h)

    # Put currently active hospital first, then alphabetical
    hospitals.sort(key=lambda h: (0 if h["is_current"] else 1, h["name"]))

    primary_hospital = current_hospital or next_hospital or (hospitals[0] if hospitals else None)

    return {
        "id": row["id"],
        "name": row["name"],
        "degree": row.get("degree", ""),
        "speciality": row["speciality"],
        "phone": row.get("phone", ""),
        "whatsapp": row.get("whatsapp", ""),
        "email": row.get("email", ""),
        "bio": row.get("bio", ""),
        "is_emergency": bool(row.get("is_emergency", 0)),
        "emergency_note": row.get("emergency_note", ""),
        "status": status,
        "status_label": label,
        "status_detail": detail,
        "current_hospital": current_hospital,
        "next_hospital": next_hospital,
        "primary_hospital": primary_hospital,
        "today": today,
        "hospitals": hospitals,
    }


def get_filters():
    hospital = request.args.get("hospital", "")
    return {
        "q": request.args.get("q", "").strip(),
        "speciality": request.args.get("speciality", "").strip(),
        "hospital": int(hospital) if hospital.isdigit() else None,
        "available": request.args.get("available") == "1",
        "emergency_only": request.args.get("emergency") == "1",
    }


def query_doctors(filters, now, force_emergency=False, include_all_sos=False):
    db = get_db()
    sql, params = "SELECT * FROM doctors WHERE 1=1", []
    if (force_emergency and not include_all_sos) or filters.get("emergency_only"):
        sql += " AND is_emergency = 1"

    if filters["q"]:
        like = f"%{_escape_like(filters['q'])}%"
        sql += (
            " AND (name LIKE ? ESCAPE '\\' OR degree LIKE ? ESCAPE '\\' "
            "OR speciality LIKE ? ESCAPE '\\' OR emergency_note LIKE ? ESCAPE '\\' "
            "OR id IN (SELECT s.doctor_id FROM schedules s JOIN hospitals h ON h.id = s.hospital_id "
            "WHERE h.name LIKE ? ESCAPE '\\' OR h.city LIKE ? ESCAPE '\\'))"
        )
        params += [like, like, like, like, like, like]
    if filters["speciality"]:
        sql += " AND speciality = ?"
        params.append(filters["speciality"])
    if filters["hospital"]:
        sql += " AND id IN (SELECT doctor_id FROM schedules WHERE hospital_id = ?)"
        params.append(filters["hospital"])

    rows = db.execute(sql, params).fetchall()
    if not rows:
        return []

    slot_rows = db.execute(
        "SELECT s.*, h.name AS hospital_name, h.city AS hospital_city, "
        "h.address AS hospital_address, h.phone AS hospital_phone, "
        "h.emergency_phone AS hospital_emergency_phone, h.has_er AS hospital_has_er "
        "FROM schedules s JOIN hospitals h ON h.id = s.hospital_id"
    ).fetchall()
    by_doctor = {}
    for s in slot_rows:
        by_doctor.setdefault(s["doctor_id"], []).append(s)

    doctors = [build_doctor(r, by_doctor.get(r["id"], []), now) for r in rows]

    if filters["available"]:
        if filters["hospital"]:
            # Must be working RIGHT NOW at the selected hospital specifically
            doctors = [
                d for d in doctors
                if any(s["is_current"] and s["hospital_id"] == filters["hospital"] for s in d["today"])
            ]
        else:
            doctors = [d for d in doctors if d["status"] == "available"]

    order = {"available": 0, "upcoming": 1, "off": 2}
    doctors.sort(key=lambda d: (order[d["status"]], 0 if d["is_emergency"] else 1, d["name"]))
    return doctors


def get_doctor(doctor_id, now):
    db = get_db()
    row = db.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
    if row is None:
        return None
    slots = db.execute(
        "SELECT s.*, h.name AS hospital_name, h.city AS hospital_city, "
        "h.address AS hospital_address, h.phone AS hospital_phone, "
        "h.emergency_phone AS hospital_emergency_phone, h.has_er AS hospital_has_er "
        "FROM schedules s JOIN hospitals h ON h.id = s.hospital_id WHERE s.doctor_id = ?",
        (doctor_id,)).fetchall()
    return build_doctor(row, slots, now)


def lookups(emergency_only=False):
    db = get_db()
    if emergency_only:
        specs = [r["speciality"] for r in db.execute(
            "SELECT DISTINCT speciality FROM doctors WHERE is_emergency = 1 ORDER BY speciality")]
    else:
        specs = [r["speciality"] for r in db.execute(
            "SELECT DISTINCT speciality FROM doctors ORDER BY speciality")]
    hospitals = db.execute("SELECT * FROM hospitals ORDER BY name").fetchall()
    return specs, hospitals


# --------------------------------------------------------------------------- #
# Public doctor pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    now = now_local()
    filters = get_filters()
    specialities, hospitals = lookups()
    return render_template(
        "index.html",
        doctors=query_doctors(filters, now),
        filters=filters,
        specialities=specialities,
        hospitals=hospitals,
        now=now,
        tz=str(TZ),
    )


@app.route("/partials/doctors")
def partial_doctors():
    return render_template("_cards.html", doctors=query_doctors(get_filters(), now_local()))


@app.route("/doctor/<int:doctor_id>")
def doctor_page(doctor_id):
    doctor = get_doctor(doctor_id, now_local())
    if doctor is None:
        abort(404)
    return render_template("doctor.html", d=doctor, tz=str(TZ))


# --------------------------------------------------------------------------- #
# SOS Emergency Portal (Ambulance Drivers & Rapid Response)
# --------------------------------------------------------------------------- #
def _sos_context():
    now = now_local()
    filters = get_filters()
    include_all = request.args.get("all") == "1"
    doctors = query_doctors(filters, now, force_emergency=True, include_all_sos=include_all)
    specialities, hospital_rows = lookups(emergency_only=not include_all)

    # Count how many SOS doctors are currently active inside each hospital right now
    all_er_docs = query_doctors({"q": "", "speciality": "", "hospital": None, "available": True},
                                now, force_emergency=True, include_all_sos=False)
    active_counts = {}
    for d in all_er_docs:
        if d["current_hospital"]:
            hid = d["current_hospital"]["id"]
            active_counts[hid] = active_counts.get(hid, 0) + 1

    er_hospitals = []
    for h in hospital_rows:
        hd = dict(h)
        hd["active_er_doctors"] = active_counts.get(hd["id"], 0)
        er_hospitals.append(hd)

    return {
        "doctors": doctors,
        "filters": filters,
        "include_all": include_all,
        "specialities": specialities,
        "hospitals": hospital_rows,
        "er_hospitals": er_hospitals,
        "now": now,
        "tz": str(TZ),
    }


@app.route("/sos")
def sos():
    return render_template("sos.html", ambulance_number=AMBULANCE_NUMBER, emergency_whatsapp=EMERGENCY_WHATSAPP, **_sos_context())


@app.route("/partials/sos")
def partial_sos():
    ctx = _sos_context()
    return render_template("_sos_cards.html", doctors=ctx["doctors"])


# --------------------------------------------------------------------------- #
# Pharmacy Store & Home Delivery System
# --------------------------------------------------------------------------- #
def load_cart(db):
    raw = session.get("cart", {})
    if not isinstance(raw, dict) or not raw:
        return {
            "items": [],
            "item_count": 0,
            "subtotal": 0.0,
            "delivery_fee": 0.0,
            "total": 0.0,
            "requires_rx": False,
        }

    ids = []
    qty_map = {}
    for k, v in raw.items():
        if str(k).isdigit():
            try:
                q = int(v)
                if q > 0:
                    mid = int(k)
                    ids.append(mid)
                    qty_map[mid] = q
            except (TypeError, ValueError):
                pass

    if not ids:
        return {
            "items": [],
            "item_count": 0,
            "subtotal": 0.0,
            "delivery_fee": 0.0,
            "total": 0.0,
            "requires_rx": False,
        }

    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT * FROM medicines WHERE is_active = 1 AND id IN ({placeholders})",
        ids,
    ).fetchall()

    items = []
    subtotal = 0.0
    item_count = 0
    requires_rx = False

    for r in rows:
        q = min(qty_map[r["id"]], max(r["stock"], 0))
        if q <= 0:
            continue
        line_total = round(float(r["price"]) * q, 2)
        subtotal += line_total
        item_count += q
        if r["requires_rx"]:
            requires_rx = True
        items.append({
            "id": r["id"],
            "name": r["name"],
            "generic_name": r["generic_name"],
            "pack_size": r["pack_size"],
            "unit_price": float(r["price"]),
            "stock": r["stock"],
            "requires_rx": bool(r["requires_rx"]),
            "quantity": q,
            "line_total": line_total,
        })

    items.sort(key=lambda x: x["name"])
    subtotal = round(subtotal, 2)
    if subtotal == 0:
        delivery_fee = 0.0
    elif subtotal >= PHARMACY_FREE_DELIVERY_OVER:
        delivery_fee = 0.0
    else:
        delivery_fee = round(PHARMACY_DELIVERY_FEE, 2)
    total = round(subtotal + delivery_fee, 2)

    return {
        "items": items,
        "item_count": item_count,
        "subtotal": subtotal,
        "delivery_fee": delivery_fee,
        "total": total,
        "requires_rx": requires_rx,
    }


def query_medicines(q="", category="", in_stock_only=False, otc_only=False):
    db = get_db()
    sql = "SELECT * FROM medicines WHERE is_active = 1"
    params = []
    if q:
        like = f"%{_escape_like(q)}%"
        sql += (" AND (name LIKE ? ESCAPE '\\' OR generic_name LIKE ? ESCAPE '\\' "
                "OR category LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
        params += [like, like, like, like]
    if category:
        sql += " AND category = ?"
        params.append(category)
    if in_stock_only:
        sql += " AND stock > 0"
    if otc_only:
        sql += " AND requires_rx = 0"
    sql += " ORDER BY CASE WHEN stock > 0 THEN 0 ELSE 1 END, name"
    return db.execute(sql, params).fetchall()


@app.route("/pharmacy/prescription/analyze", methods=["POST"])
def pharmacy_prescription_analyze():
    upload = request.files.get("prescription_file")
    if upload is None or not (upload.filename or "").strip():
        return jsonify({"ok": False, "message": "Choose a prescription photo or PDF first."}), 400
    try:
        metadata = _create_prescription_upload(upload, allow_ai=request.form.get("rx_ai_consent") == "1")
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except OSError:
        app.logger.exception("Unable to store a private prescription upload")
        return jsonify({"ok": False, "message": "The prescription could not be stored securely. Please retry."}), 500
    session["rx_upload_token"] = metadata["token"]
    session.modified = True
    return jsonify(_prescription_response(metadata))


@app.route("/pharmacy")
def pharmacy():
    db = get_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    in_stock_only = request.args.get("in_stock") == "1"
    otc_only = request.args.get("otc") == "1"

    medicines = query_medicines(q, category, in_stock_only, otc_only)
    categories = [r["category"] for r in db.execute(
        "SELECT DISTINCT category FROM medicines WHERE is_active = 1 ORDER BY category")]
    cart = load_cart(db)

    return render_template(
        "pharmacy.html",
        medicines=medicines,
        categories=categories,
        cart=cart,
        filters={"q": q, "category": category, "in_stock": in_stock_only, "otc": otc_only},
    )


@app.route("/pharmacy/cart/add", methods=["POST"])
def pharmacy_cart_add():
    db = get_db()
    med_id = request.form.get("medicine_id", "")
    qty_raw = request.form.get("quantity", "1")
    if not med_id.isdigit():
        flash("Invalid medicine selected.", "error")
        return redirect(url_for("pharmacy"))
    try:
        qty = max(1, int(qty_raw))
    except ValueError:
        qty = 1

    med = db.execute(
        "SELECT * FROM medicines WHERE id = ? AND is_active = 1", (int(med_id),)).fetchone()
    if med is None:
        flash("Medicine not found.", "error")
        return redirect(url_for("pharmacy"))
    if med["stock"] <= 0:
        flash(f"{med['name']} is currently out of stock.", "error")
        return redirect(url_for("pharmacy"))

    cart = dict(session.get("cart", {}))
    key = str(med["id"])
    new_qty = min(int(cart.get(key, 0)) + qty, med["stock"])
    cart[key] = new_qty
    session["cart"] = cart
    session.modified = True
    flash(f"Added {med['name']} (qty {new_qty} in cart).", "ok")
    return redirect(url_for("pharmacy"))


@app.route("/pharmacy/cart/update", methods=["POST"])
def pharmacy_cart_update():
    db = get_db()
    med_id = request.form.get("medicine_id", "")
    qty_raw = request.form.get("quantity", "0")
    if not med_id.isdigit():
        return redirect(url_for("pharmacy"))
    try:
        qty = int(qty_raw)
    except ValueError:
        qty = 0

    cart = dict(session.get("cart", {}))
    key = str(int(med_id))
    if qty <= 0:
        cart.pop(key, None)
        flash("Item removed from cart.", "ok")
    else:
        med = db.execute(
            "SELECT * FROM medicines WHERE id = ? AND is_active = 1", (int(med_id),)).fetchone()
        if med is None or med["stock"] <= 0:
            cart.pop(key, None)
        else:
            cart[key] = min(qty, med["stock"])
            flash(f"Updated {med['name']} quantity to {cart[key]}.", "ok")
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("pharmacy"))


@app.route("/pharmacy/cart/clear", methods=["POST"])
def pharmacy_cart_clear():
    session.pop("cart", None)
    flash("Cart cleared.", "ok")
    return redirect(url_for("pharmacy"))


def _generate_order_code(db):
    for _ in range(10):
        code = f"DO-{secrets.token_hex(3).upper()}"
        if not db.execute("SELECT 1 FROM pharmacy_orders WHERE order_code = ?", (code,)).fetchone():
            return code
    return f"DO-{secrets.token_hex(5).upper()}"


@app.route("/pharmacy/checkout", methods=["POST"])
def pharmacy_checkout():
    db = get_db()
    raw_cart = session.get("cart", {})
    if not isinstance(raw_cart, dict) or not raw_cart:
        flash("Your cart is empty. Add medicines before placing an order.", "error")
        return redirect(url_for("pharmacy"))

    f = request.form
    customer_name = f.get("customer_name", "").strip()
    phone = f.get("phone", "").strip()
    whatsapp = f.get("whatsapp", "").strip()
    address = f.get("address", "").strip()
    city = f.get("city", "").strip()
    notes = f.get("notes", "").strip()
    payment_method = f.get("payment_method", "Cash on Delivery").strip()
    has_rx = f.get("has_rx") == "1"
    rx_reference = f.get("rx_reference", "").strip()

    if not customer_name or not phone or not address:
        flash("Name, phone number, and delivery address are required.", "error")
        return redirect(url_for("pharmacy"))
    if payment_method not in PAYMENT_METHODS:
        payment_method = PAYMENT_METHODS[0]

    requested = []
    for k, v in raw_cart.items():
        if str(k).isdigit():
            try:
                q = int(v)
                if q > 0:
                    requested.append((int(k), q))
            except (TypeError, ValueError):
                pass
    if not requested:
        flash("Your cart is empty.", "error")
        return redirect(url_for("pharmacy"))

    # Prefer the exact server-stored file that was analysed. The browser cannot
    # replace the analysed document between verification and checkout.
    metadata = None
    original_upload_path = None
    upload_meta_path = None
    posted_token = f.get("rx_upload_token", "").strip()
    upload = request.files.get("prescription_file")
    if posted_token:
        if posted_token != session.get("rx_upload_token"):
            flash("Prescription upload session expired. Please analyse the file again.", "error")
            return redirect(url_for("pharmacy"))
        metadata = _read_prescription_metadata(posted_token)
        if metadata is None:
            flash("The analysed prescription could not be found. Please upload it again.", "error")
            return redirect(url_for("pharmacy"))
        original_upload_path = os.path.join(_prescription_directory(), metadata["storage_name"])
        upload_meta_path = os.path.join(_prescription_directory(), posted_token + ".json")
        if not os.path.isfile(original_upload_path):
            flash("The uploaded prescription is no longer available. Please upload it again.", "error")
            return redirect(url_for("pharmacy"))
        if upload and upload.filename:
            browser_bytes = upload.stream.read(PRESCRIPTION_MAX_BYTES + 1)
            if hashlib.sha256(browser_bytes).hexdigest() != metadata.get("sha256"):
                flash("The prescription file changed after analysis. Please analyse the new file again.", "error")
                return redirect(url_for("pharmacy"))
    elif upload and upload.filename:
        try:
            metadata = _create_prescription_upload(upload, allow_ai=f.get("rx_ai_consent") == "1")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("pharmacy"))
        except OSError:
            app.logger.exception("Unable to store a private prescription upload")
            flash("The prescription could not be stored securely. Please retry.", "error")
            return redirect(url_for("pharmacy"))
        original_upload_path = os.path.join(_prescription_directory(), metadata["storage_name"])
        upload_meta_path = os.path.join(_prescription_directory(), metadata["token"] + ".json")

    # Re-check the enrolment and date on the actual order day in IST.
    extracted = (metadata or {}).get("extracted") or {}
    rx_doctor_name = str(extracted.get("doctor_name") or "").strip()
    rx_date = str(extracted.get("prescription_date") or "").strip()
    doctor_row = _match_doctor_record(rx_doctor_name) if rx_doctor_name else None
    rx_doctor_verified = bool(doctor_row and doctor_row["pharmacy_discount_eligible"])
    rx_date_valid, rx_date_status = _prescription_date_check(rx_date, now_local().date())
    rx_ai_status = (metadata or {}).get("ai_status", "")
    rx_ai_summary = (metadata or {}).get("ai_message", "")
    rx_doctor_name_for_record = doctor_row["name"] if doctor_row else rx_doctor_name
    rx_attached = bool(metadata)
    has_rx = bool(has_rx or rx_reference or rx_attached)

    try:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")

        order_items = []
        subtotal = 0.0
        needs_rx = False
        for mid, qty in requested:
            med = db.execute(
                "SELECT * FROM medicines WHERE id = ? AND is_active = 1", (mid,)).fetchone()
            if med is None:
                db.rollback()
                flash("One of the medicines in your cart is no longer available.", "error")
                return redirect(url_for("pharmacy"))
            if med["stock"] < qty:
                db.rollback()
                flash(f"Insufficient stock for {med['name']}: only {med['stock']} left.", "error")
                return redirect(url_for("pharmacy"))
            if med["requires_rx"]:
                needs_rx = True
            unit_price = float(med["price"])
            line_total = round(unit_price * qty, 2)
            subtotal += line_total
            order_items.append((med["id"], med["name"], med["pack_size"], unit_price, qty, line_total))

        if needs_rx and not has_rx:
            db.rollback()
            flash("Your cart includes prescription medicines. Please upload a prescription or confirm that you will provide it to the pharmacist.", "error")
            return redirect(url_for("pharmacy"))

        subtotal = round(subtotal, 2)
        discount_percent = PHARMACY_RX_DISCOUNT_PERCENT if (
            needs_rx and rx_attached and rx_ai_status == "analysed"
            and bool(extracted.get("readable")) and rx_doctor_verified and rx_date_valid
        ) else 0.0
        discount_amount = round(subtotal * discount_percent / 100.0, 2)
        discounted_subtotal = round(subtotal - discount_amount, 2)
        delivery_fee = 0.0 if subtotal >= PHARMACY_FREE_DELIVERY_OVER else round(PHARMACY_DELIVERY_FEE, 2)
        total = round(discounted_subtotal + delivery_fee, 2)
        order_code = _generate_order_code(db)
        order_time = now_local()
        created_at = order_time.strftime("%Y-%m-%d %I:%M %p") + " " + (order_time.tzname() or _TZ_NAME)

        rx_storage_key = ""
        moved_file_path = None
        if rx_attached and original_upload_path:
            ext = os.path.splitext(metadata["storage_name"])[1]
            rx_storage_key = order_code + ext
            destination = os.path.join(_prescription_directory(), rx_storage_key)
            os.replace(original_upload_path, destination)
            moved_file_path = destination

        try:
            cur = db.execute(
                "INSERT INTO pharmacy_orders(order_code, customer_name, phone, whatsapp, address, city, "
                "notes, payment_method, has_rx, rx_reference, delivery_fee, subtotal, total, status, created_at, "
                "rx_filename, rx_storage_key, rx_doctor_name, rx_date, rx_doctor_verified, rx_date_valid, "
                "rx_ai_status, rx_ai_summary, discount_percent, discount_amount) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_code, customer_name, phone, whatsapp, address, city, notes, payment_method,
                 1 if has_rx else 0, rx_reference, delivery_fee, subtotal, total, "Pending", created_at,
                 metadata.get("filename", "") if metadata else "", rx_storage_key,
                 rx_doctor_name_for_record, rx_date, 1 if rx_doctor_verified else 0,
                 1 if rx_date_valid else 0, rx_ai_status, rx_ai_summary,
                 discount_percent, discount_amount),
            )
            order_id = cur.lastrowid
            for mid, mname, pack, uprice, qty, ltotal in order_items:
                db.execute(
                    "INSERT INTO pharmacy_order_items(order_id, medicine_id, medicine_name, pack_size, "
                    "unit_price, quantity, line_total) VALUES (?,?,?,?,?,?,?)",
                    (order_id, mid, mname, pack, uprice, qty, ltotal),
                )
                res = db.execute(
                    "UPDATE medicines SET stock = stock - ? WHERE id = ? AND stock >= ?",
                    (qty, mid, qty),
                )
                if res.rowcount != 1:
                    raise RuntimeError("Concurrent stock update failed")
            db.commit()
        except Exception:
            db.rollback()
            if moved_file_path and os.path.isfile(moved_file_path):
                os.replace(moved_file_path, original_upload_path)
            raise
    except Exception:
        db.rollback()
        app.logger.exception("Pharmacy checkout failed")
        flash("Could not complete your order due to a stock update. Please try again.", "error")
        return redirect(url_for("pharmacy"))

    if upload_meta_path and os.path.isfile(upload_meta_path):
        try:
            os.remove(upload_meta_path)
        except OSError:
            app.logger.warning("Unable to remove temporary prescription metadata")
    session.pop("rx_upload_token", None)
    session.pop("cart", None)
    if discount_amount:
        flash(f"Order {order_code} placed with a verified {discount_percent:.0f}% prescription offer.", "ok")
    else:
        flash(f"Order {order_code} placed! A pharmacist will review the prescription before dispensing Rx medicines.", "ok")
    return redirect(url_for("pharmacy_order", order_code=order_code))

@app.route("/pharmacy/order/<order_code>")
def pharmacy_order(order_code):
    db = get_db()
    order = db.execute(
        "SELECT * FROM pharmacy_orders WHERE UPPER(order_code) = UPPER(?)",
        (order_code.strip(),),
    ).fetchone()
    if order is None:
        abort(404)
    items = db.execute(
        "SELECT * FROM pharmacy_order_items WHERE order_id = ? ORDER BY id",
        (order["id"],),
    ).fetchall()
    return render_template("pharmacy_order.html", order=order, items=items)


@app.route("/pharmacy/track")
def pharmacy_track():
    db = get_db()
    code = request.args.get("code", "").strip().upper()
    phone = request.args.get("phone", "").strip()
    orders = []
    searched = bool(code or phone)

    if code:
        orders = db.execute(
            "SELECT * FROM pharmacy_orders WHERE UPPER(order_code) = ? ORDER BY id DESC",
            (code,),
        ).fetchall()
    elif phone:
        clean_q = re.sub(r"\D", "", phone)
        all_rows = db.execute("SELECT * FROM pharmacy_orders ORDER BY id DESC LIMIT 100").fetchall()
        orders = [
            r for r in all_rows
            if clean_q and clean_q in re.sub(r"\D", "", r["phone"])
        ]

    return render_template(
        "pharmacy_track.html",
        orders=orders,
        code=code,
        phone=phone,
        searched=searched,
    )


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
def serialize(d):
    return {
        "id": d["id"],
        "name": d["name"],
        "degree": d["degree"],
        "speciality": d["speciality"],
        "phone": d["phone"],
        "whatsapp": d["whatsapp"],
        "email": d["email"],
        "bio": d["bio"],
        "is_emergency": d["is_emergency"],
        "emergency_note": d["emergency_note"],
        "status": d["status"],
        "status_label": d["status_label"],
        "status_detail": d["status_detail"],
        "current_hospital": d["current_hospital"],
        "hospitals": [{
            "id": h["id"],
            "name": h["name"],
            "city": h["city"],
            "address": h["address"],
            "phone": h["phone"],
            "emergency_phone": h["emergency_phone"],
            "is_current": h["is_current"],
            "hours": [{"day": x["weekday"], "hours": x["hours"]} for x in h["days"]],
        } for h in d["hospitals"]],
    }


@app.route("/api/doctors")
def api_doctors():
    now = now_local()
    doctors = query_doctors(get_filters(), now)
    return jsonify({
        "server_time": now.isoformat(),
        "timezone": str(TZ),
        "count": len(doctors),
        "doctors": [serialize(d) for d in doctors],
    })


@app.route("/api/doctors/<int:doctor_id>")
def api_doctor(doctor_id):
    now = now_local()
    d = get_doctor(doctor_id, now)
    if d is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"server_time": now.isoformat(), "doctor": serialize(d)})


@app.route("/api/sos")
def api_sos():
    ctx = _sos_context()
    return jsonify({
        "server_time": ctx["now"].isoformat(),
        "timezone": ctx["tz"],
        "count": len(ctx["doctors"]),
        "hospitals": [{
            "id": h["id"],
            "name": h["name"],
            "city": h["city"],
            "address": h["address"],
            "phone": h["phone"],
            "emergency_phone": h["emergency_phone"],
            "has_er": bool(h["has_er"]),
            "active_er_doctors": h["active_er_doctors"],
        } for h in ctx["er_hospitals"]],
        "doctors": [serialize(d) for d in ctx["doctors"]],
    })


@app.route("/api/pharmacy/medicines")
def api_pharmacy_medicines():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    in_stock_only = request.args.get("in_stock") == "1"
    otc_only = request.args.get("otc") == "1"
    rows = query_medicines(q, category, in_stock_only, otc_only)
    return jsonify({
        "currency": CURRENCY,
        "count": len(rows),
        "medicines": [{
            "id": r["id"],
            "name": r["name"],
            "generic_name": r["generic_name"],
            "category": r["category"],
            "pack_size": r["pack_size"],
            "price": float(r["price"]),
            "stock": r["stock"],
            "requires_rx": bool(r["requires_rx"]),
            "description": r["description"],
        } for r in rows],
    })


@app.route("/api/pharmacy/orders/<order_code>")
def api_pharmacy_order(order_code):
    db = get_db()
    order = db.execute(
        "SELECT * FROM pharmacy_orders WHERE UPPER(order_code) = UPPER(?)",
        (order_code.strip(),),
    ).fetchone()
    if order is None:
        return jsonify({"error": "not found"}), 404
    items = db.execute(
        "SELECT * FROM pharmacy_order_items WHERE order_id = ? ORDER BY id",
        (order["id"],),
    ).fetchall()
    return jsonify({
        "order_code": order["order_code"],
        "status": order["status"],
        "payment_method": order["payment_method"],
        "subtotal": float(order["subtotal"]),
        "discount_percent": float(order["discount_percent"]),
        "discount_amount": float(order["discount_amount"]),
        "delivery_fee": float(order["delivery_fee"]),
        "total": float(order["total"]),
        "created_at": order["created_at"],
        "items": [{
            "medicine_name": it["medicine_name"],
            "pack_size": it["pack_size"],
            "unit_price": float(it["unit_price"]),
            "quantity": it["quantity"],
            "line_total": float(it["line_total"]),
        } for it in items],
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.errorhandler(413)
def upload_too_large(_error):
    message = "The upload is too large. Prescription files must be 8 MB or smaller."
    if request.path.startswith("/pharmacy/prescription/"):
        return jsonify({"ok": False, "message": message}), 413
    flash(message, "error")
    return redirect(url_for("pharmacy"))


# --------------------------------------------------------------------------- #
# CSRF & Admin Auth
# --------------------------------------------------------------------------- #
@app.before_request
def check_csrf():
    if request.method == "POST":
        expected = session.get("csrf")
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        # A missing token on either side must fail (two empty strings would compare equal).
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            abort(400, "Bad CSRF token")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = request.remote_addr or "local"
    if request.method == "POST":
        now_ts = time.time()
        fails, start_ts = _LOGIN_FAILS.get(ip, (0, now_ts))
        if now_ts - start_ts > _LOGIN_WINDOW_SEC:
            fails, start_ts = 0, now_ts
        if fails >= _LOGIN_MAX_FAILS:
            flash("Too many failed login attempts. Wait a minute and try again.", "error")
            return render_template("login.html"), 429

        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            _LOGIN_FAILS.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin"))

        _LOGIN_FAILS[ip] = (fails + 1, start_ts)
        flash("Wrong password.", "error")
    return render_template("login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Admin: Doctors, Hospitals & Schedules
# --------------------------------------------------------------------------- #
@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    now = now_local()
    doctors = query_doctors(
        {"q": "", "speciality": "", "hospital": None, "available": False, "emergency_only": False},
        now,
    )
    doctors.sort(key=lambda d: d["name"])

    slots = db.execute(
        "SELECT s.*, h.name AS hospital_name FROM schedules s JOIN hospitals h ON h.id = s.hospital_id "
        "ORDER BY s.doctor_id, s.weekday, s.start_time"
    ).fetchall()
    slots_by_doc = {}
    for s in slots:
        slots_by_doc.setdefault(s["doctor_id"], []).append(s)

    hospitals = db.execute("SELECT * FROM hospitals ORDER BY name").fetchall()
    pending_orders = db.execute(
        "SELECT COUNT(*) FROM pharmacy_orders WHERE status IN ('Pending', 'Confirmed', 'Out for Delivery')"
    ).fetchone()[0]

    return render_template(
        "admin.html",
        doctors=doctors,
        hospitals=hospitals,
        slots_by_doc=slots_by_doc,
        pending_orders=pending_orders,
        default_pw=ADMIN_PASSWORD == "admin123",
    )


@app.route("/admin/hospitals", methods=["POST"])
@admin_required
def add_hospital():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        flash("Hospital name is required.", "error")
    else:
        has_er = 1 if f.get("has_er") in ("1", "on", "true") or "has_er" not in f else 0
        get_db().execute(
            "INSERT INTO hospitals(name, city, address, phone, emergency_phone, has_er) "
            "VALUES (?,?,?,?,?,?)",
            (name, f.get("city", "").strip(), f.get("address", "").strip(),
             f.get("phone", "").strip(), f.get("emergency_phone", "").strip(), has_er),
        )
        get_db().commit()
        flash(f"Added hospital {name}.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/hospitals/<int:hid>/edit", methods=["GET", "POST"])
@admin_required
def edit_hospital(hid):
    db = get_db()
    hosp = db.execute("SELECT * FROM hospitals WHERE id = ?", (hid,)).fetchone()
    if hosp is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash("Hospital name is required.", "error")
        else:
            has_er = 1 if f.get("has_er") == "1" else 0
            db.execute(
                "UPDATE hospitals SET name=?, city=?, address=?, phone=?, emergency_phone=?, has_er=? "
                "WHERE id=?",
                (name, f.get("city", "").strip(), f.get("address", "").strip(),
                 f.get("phone", "").strip(), f.get("emergency_phone", "").strip(), has_er, hid),
            )
            db.commit()
            flash(f"Updated {name}.", "ok")
            return redirect(url_for("admin"))
    return render_template("admin_edit_hospital.html", h=hosp)


@app.route("/admin/hospitals/<int:hid>/delete", methods=["POST"])
@admin_required
def delete_hospital(hid):
    get_db().execute("DELETE FROM hospitals WHERE id = ?", (hid,))
    get_db().commit()
    flash("Hospital deleted (and its schedule slots).", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/doctors", methods=["POST"])
@admin_required
def add_doctor():
    f = request.form
    name, spec = f.get("name", "").strip(), f.get("speciality", "").strip()
    if not name or not spec:
        flash("Doctor name and speciality are required.", "error")
    else:
        is_er = 1 if f.get("is_emergency") == "1" else 0
        pharmacy_eligible = 1 if f.get("pharmacy_discount_eligible") == "1" else 0
        get_db().execute(
            "INSERT INTO doctors(name, degree, speciality, phone, whatsapp, email, bio, "
            "is_emergency, emergency_note, pharmacy_discount_eligible) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, f.get("degree", "").strip(), spec, f.get("phone", "").strip(),
             f.get("whatsapp", "").strip(), f.get("email", "").strip(),
             f.get("bio", "").strip(), is_er, f.get("emergency_note", "").strip(), pharmacy_eligible),
        )
        get_db().commit()
        flash(f"Added {name}.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/doctors/<int:did>/edit", methods=["GET", "POST"])
@admin_required
def edit_doctor(did):
    db = get_db()
    doc = db.execute("SELECT * FROM doctors WHERE id = ?", (did,)).fetchone()
    if doc is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        name, spec = f.get("name", "").strip(), f.get("speciality", "").strip()
        if not name or not spec:
            flash("Doctor name and speciality are required.", "error")
        else:
            is_er = 1 if f.get("is_emergency") == "1" else 0
            pharmacy_eligible = 1 if f.get("pharmacy_discount_eligible") == "1" else 0
            db.execute(
                "UPDATE doctors SET name=?, degree=?, speciality=?, phone=?, whatsapp=?, email=?, "
                "bio=?, is_emergency=?, emergency_note=?, pharmacy_discount_eligible=? WHERE id=?",
                (name, f.get("degree", "").strip(), spec, f.get("phone", "").strip(),
                 f.get("whatsapp", "").strip(), f.get("email", "").strip(),
                 f.get("bio", "").strip(), is_er, f.get("emergency_note", "").strip(), pharmacy_eligible, did),
            )
            db.commit()
            flash(f"Updated {name}.", "ok")
            return redirect(url_for("admin"))
    return render_template("admin_edit_doctor.html", d=doc)


@app.route("/admin/doctors/<int:did>/delete", methods=["POST"])
@admin_required
def delete_doctor(did):
    get_db().execute("DELETE FROM doctors WHERE id = ?", (did,))
    get_db().commit()
    flash("Doctor deleted.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/schedules", methods=["POST"])
@admin_required
def add_schedule():
    f = request.form
    days = [int(x) for x in f.getlist("weekday") if x.isdigit() and 0 <= int(x) <= 6]
    start, end = f.get("start_time", ""), f.get("end_time", "")
    overnight = f.get("overnight") == "1"
    db = get_db()

    valid_ids = (
        f.get("doctor_id", "").isdigit()
        and f.get("hospital_id", "").isdigit()
        and bool(days)
        and db.execute("SELECT 1 FROM doctors WHERE id=?", (f["doctor_id"],)).fetchone()
        and db.execute("SELECT 1 FROM hospitals WHERE id=?", (f["hospital_id"],)).fetchone()
    )

    if overnight:
        valid_times = bool(TIME_RE.match(start) and TIME_RE.match(end) and start != end)
    else:
        valid_times = bool(TIME_RE.match(start) and END_TIME_RE.match(end) and end > start)

    if not (valid_ids and valid_times):
        flash(
            "Pick a doctor, hospital, at least one day, and a valid start/end time "
            "(check 'Overnight shift' if ending after midnight).",
            "error",
        )
        return redirect(url_for("admin"))

    doc_id = int(f["doctor_id"])
    hosp_id = int(f["hospital_id"])
    candidate_slots = []
    for d in days:
        if overnight and end < start:
            candidate_slots.append((doc_id, hosp_id, d, start, "24:00"))
            if end != "00:00":
                candidate_slots.append((doc_id, hosp_id, (d + 1) % 7, "00:00", end))
        else:
            candidate_slots.append((doc_id, hosp_id, d, start, end))

    added = 0
    for row in candidate_slots:
        exists = db.execute(
            "SELECT 1 FROM schedules WHERE doctor_id=? AND hospital_id=? AND weekday=? "
            "AND start_time=? AND end_time=?",
            row,
        ).fetchone()
        if not exists:
            db.execute(
                "INSERT INTO schedules(doctor_id, hospital_id, weekday, start_time, end_time) "
                "VALUES (?,?,?,?,?)",
                row,
            )
            added += 1

    db.commit()
    if added:
        flash(f"Added {added} schedule slot(s).", "ok")
    else:
        flash("Those exact schedule slots already exist.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/schedules/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_schedule(sid):
    get_db().execute("DELETE FROM schedules WHERE id = ?", (sid,))
    get_db().commit()
    flash("Schedule slot removed.", "ok")
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------- #
# Admin: Pharmacy Inventory & Home Delivery Orders
# --------------------------------------------------------------------------- #
@app.route("/admin/pharmacy/prescriptions/<int:oid>")
@admin_required
def admin_download_prescription(oid):
    order = get_db().execute(
        "SELECT rx_filename, rx_storage_key FROM pharmacy_orders WHERE id = ?", (oid,)
    ).fetchone()
    if order is None or not order["rx_storage_key"]:
        abort(404)
    storage_key = order["rx_storage_key"]
    if os.path.basename(storage_key) != storage_key or not re.fullmatch(r"DO-[A-F0-9]+\.(pdf|jpg|jpeg|png|webp)", storage_key, re.I):
        abort(404)
    file_path = os.path.join(_prescription_directory(), storage_key)
    if not os.path.isfile(file_path):
        abort(404)
    response = send_file(
        file_path,
        as_attachment=True,
        download_name=order["rx_filename"] or "prescription",
        mimetype=_prescription_mime(os.path.splitext(storage_key)[1].lower()),
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/admin/pharmacy")
@admin_required
def admin_pharmacy():
    db = get_db()
    medicines = db.execute("SELECT * FROM medicines ORDER BY is_active DESC, name").fetchall()
    orders = db.execute("SELECT * FROM pharmacy_orders ORDER BY id DESC LIMIT 100").fetchall()
    items_rows = db.execute("SELECT * FROM pharmacy_order_items ORDER BY id").fetchall()
    items_by_order = {}
    for it in items_rows:
        items_by_order.setdefault(it["order_id"], []).append(it)
    return render_template(
        "admin_pharmacy.html",
        medicines=medicines,
        orders=orders,
        items_by_order=items_by_order,
    )


@app.route("/admin/pharmacy/medicines", methods=["POST"])
@admin_required
def admin_add_medicine():
    f = request.form
    name = f.get("name", "").strip()
    try:
        price = float(f.get("price", ""))
        stock = int(f.get("stock", "0"))
    except ValueError:
        price, stock = -1.0, -1

    if not name or price < 0 or stock < 0:
        flash("Medicine name, valid non-negative price, and stock are required.", "error")
    else:
        get_db().execute(
            "INSERT INTO medicines(name, generic_name, category, pack_size, price, stock, "
            "requires_rx, description, is_active) VALUES (?,?,?,?,?,?,?,?,1)",
            (name, f.get("generic_name", "").strip(),
             f.get("category", "General").strip() or "General",
             f.get("pack_size", "").strip(), round(price, 2), stock,
             1 if f.get("requires_rx") == "1" else 0,
             f.get("description", "").strip()),
        )
        get_db().commit()
        flash(f"Added medicine {name}.", "ok")
    return redirect(url_for("admin_pharmacy"))


@app.route("/admin/pharmacy/medicines/<int:mid>/edit", methods=["POST"])
@admin_required
def admin_edit_medicine(mid):
    db = get_db()
    f = request.form
    try:
        price = float(f.get("price", ""))
        stock = int(f.get("stock", "0"))
    except ValueError:
        price, stock = -1.0, -1

    if price < 0 or stock < 0:
        flash("Price and stock must be valid non-negative numbers.", "error")
    else:
        is_active = 1 if f.get("is_active") == "1" else 0
        requires_rx = 1 if f.get("requires_rx") == "1" else 0
        db.execute(
            "UPDATE medicines SET price=?, stock=?, requires_rx=?, is_active=? WHERE id=?",
            (round(price, 2), stock, requires_rx, is_active, mid),
        )
        db.commit()
        flash("Medicine inventory updated.", "ok")
    return redirect(url_for("admin_pharmacy"))


@app.route("/admin/pharmacy/medicines/<int:mid>/delete", methods=["POST"])
@admin_required
def admin_delete_medicine(mid):
    get_db().execute("DELETE FROM medicines WHERE id = ?", (mid,))
    get_db().commit()
    flash("Medicine deleted.", "ok")
    return redirect(url_for("admin_pharmacy"))


@app.route("/admin/pharmacy/orders/<int:oid>/status", methods=["POST"])
@admin_required
def admin_update_order(oid):
    db = get_db()
    new_status = request.form.get("status", "").strip()
    if new_status not in ORDER_STATUSES:
        flash("Invalid order status.", "error")
        return redirect(url_for("admin_pharmacy"))

    order = db.execute("SELECT * FROM pharmacy_orders WHERE id = ?", (oid,)).fetchone()
    if order is None:
        abort(404)

    old_status = order["status"]
    if old_status != new_status:
        items = db.execute(
            "SELECT * FROM pharmacy_order_items WHERE order_id = ?", (oid,)).fetchall()
        if old_status != "Cancelled" and new_status == "Cancelled":
            for it in items:
                if it["medicine_id"] is not None:
                    db.execute(
                        "UPDATE medicines SET stock = stock + ? WHERE id = ?",
                        (it["quantity"], it["medicine_id"]),
                    )
        elif old_status == "Cancelled" and new_status != "Cancelled":
            for it in items:
                if it["medicine_id"] is not None:
                    res = db.execute(
                        "UPDATE medicines SET stock = stock - ? WHERE id = ? AND stock >= ?",
                        (it["quantity"], it["medicine_id"], it["quantity"]),
                    )
                    if res.rowcount != 1:
                        db.rollback()
                        flash(
                            f"Cannot reactivate order: insufficient stock for {it['medicine_name']}.",
                            "error",
                        )
                        return redirect(url_for("admin_pharmacy"))

        db.execute("UPDATE pharmacy_orders SET status = ? WHERE id = ?", (new_status, oid))
        db.commit()
        flash(f"Order {order['order_code']} marked as {new_status}.", "ok")

    return redirect(url_for("admin_pharmacy"))


init_db()

if __name__ == "__main__":
    if ADMIN_PASSWORD == "admin123":
        print("WARNING: using the default admin password. Set DOCTORSON_ADMIN_PASSWORD.")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
