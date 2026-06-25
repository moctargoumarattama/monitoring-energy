from flask import Flask, request, jsonify, render_template, g
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import sqlite3
import time
import json
import os
from datetime import datetime

from energy_rules import DEFAULT_COMFORT_PROFILE, evaluate_energy_state
from study_audit import normalize_study_pair

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_TOKEN = os.environ.get("APP_TOKEN", "esp32_maison_2024")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "energy.db"))
COMFORT_PROFILE_ID = 1
AUTO_COMMAND_COOLDOWN_SECONDS = 30
MANUAL_OVERRIDE_WINDOW_SECONDS = 120
DEFAULT_PRICE_PER_KWH = 1.2
STUDY_EXPERIMENT_NAME = "pfa_energy_audit"
PREDICTION_SAMPLE_LIMIT = 20
ENERGY_TOTALS_RESET_AT_KEY_PREFIX = "energy_totals_reset_at"
ENERGY_TOTALS_RESET_OFFSET_KEY_PREFIX = "energy_totals_reset_offset_kwh"

TELEMETRY_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ts", "INTEGER NOT NULL"),
    ("device_id", "TEXT NOT NULL"),
    ("mode", "TEXT"),
    ("presence", "INTEGER"),
    ("window_open", "INTEGER"),
    ("temp_ok", "INTEGER"),
    ("temp_c", "REAL"),
    ("humidity", "REAL"),
    ("current_a", "REAL"),
    ("power_w", "REAL"),
    ("energy_total_kwh", "REAL"),
    ("energy_before_kwh", "REAL"),
    ("energy_after_kwh", "REAL"),
    ("cost_mad", "REAL"),
    ("lamp_on", "INTEGER"),
    ("fan_on", "INTEGER"),
    ("anomaly_dht_fail", "INTEGER"),
    ("alerts", "INTEGER"),
    ("remote_enabled", "INTEGER"),
    ("thermal_state", "TEXT"),
    ("recommended_action", "TEXT"),
    ("critical_temp_alert", "INTEGER"),
    ("energy_anomaly_json", "TEXT"),
    ("comfort_score", "INTEGER"),
    ("auto_command", "TEXT"),
    ("reasons_json", "TEXT"),
]

ALLOWED_COMMANDS = {
    "lamp_on",
    "lamp_off",
    "fan_on",
    "fan_off",
    "fan_auto",
    "window_open",
    "window_close",
    "reset_study",
    "reset_energy_totals",
    "reboot",
    "set_period",
    "set_price",
    "study_start_before",
    "study_start_after",
    "study_stop",
    "study_report",
    "daily_report_now",
    "financial_audit",
}

COALESCE_GROUPS = [
    {"lamp_on", "lamp_off"},
    {"fan_on", "fan_off", "fan_auto"},
    {"window_open", "window_close"},
    {"set_period"},
    {"set_price"},
    {"daily_report_now"},
]

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)


# ----------------------------
# DB helpers
# ----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA busy_timeout=5000;")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()
    cur = db.cursor()

    def create_telemetry_table(table_name):
        cols_sql = ",\n            ".join(f"{name} {col_type}" for name, col_type in TELEMETRY_COLUMNS)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                {cols_sql}
            )
            """
        )

    def rebuild_telemetry_table(existing_columns):
        cur.execute("DROP TABLE IF EXISTS telemetry_new")
        create_telemetry_table("telemetry_new")

        existing_set = set(existing_columns)
        insert_columns = []
        select_exprs = []
        for column_name, _ in TELEMETRY_COLUMNS:
            if column_name in existing_set:
                insert_columns.append(column_name)
                select_exprs.append(column_name)
            elif column_name == "comfort_score" and "score" in existing_set:
                insert_columns.append("comfort_score")
                select_exprs.append("score AS comfort_score")
            elif column_name == "critical_temp_alert" and "anomaly_temp" in existing_set:
                insert_columns.append("critical_temp_alert")
                select_exprs.append("anomaly_temp AS critical_temp_alert")

        if insert_columns:
            cur.execute(
                f"""
                INSERT INTO telemetry_new ({",".join(insert_columns)})
                SELECT {",".join(select_exprs)} FROM telemetry
                """
            )

        cur.execute("DROP TABLE telemetry")
        cur.execute("ALTER TABLE telemetry_new RENAME TO telemetry")

    telemetry_exists = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry'"
    ).fetchone()
    if telemetry_exists:
        existing_telemetry_columns = [
            row["name"] for row in cur.execute("PRAGMA table_info(telemetry)").fetchall()
        ]
        desired_telemetry_columns = [name for name, _ in TELEMETRY_COLUMNS]
        if set(existing_telemetry_columns) != set(desired_telemetry_columns):
            rebuild_telemetry_table(existing_telemetry_columns)
    else:
        create_telemetry_table("telemetry")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            command TEXT NOT NULL,
            payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT 'manual'
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start INTEGER NOT NULL,
            ts_end INTEGER,
            device_id TEXT NOT NULL,
            name TEXT NOT NULL,
            phase TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            date TEXT NOT NULL,
            daily_energy_kwh REAL,
            daily_cost_mad REAL,
            daily_anomalies INTEGER,
            daily_avg_score REAL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS study_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            before_kwh REAL,
            after_kwh REAL,
            savings_kwh REAL,
            savings_percent REAL,
            savings_mad REAL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            price_per_kwh REAL,
            energy_total_kwh REAL,
            cost_total_mad REAL,
            energy_before_kwh REAL,
            energy_after_kwh REAL,
            study_before_kwh REAL,
            study_after_kwh REAL,
            study_savings_kwh REAL,
            study_savings_percent REAL,
            study_savings_mad REAL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS comfort_profiles (
            id INTEGER PRIMARY KEY,
            comfort_min_c REAL NOT NULL,
            comfort_max_c REAL NOT NULL,
            critical_temp_c REAL NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    def ensure_columns(table_name, columns):
        existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for col, col_type in columns.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type}")

    ensure_columns("commands", {
        "payload": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "source": "TEXT NOT NULL DEFAULT 'manual'",
    })

    ensure_columns("daily_reports", {
        "date": "TEXT",
        "daily_energy_kwh": "REAL",
        "daily_cost_mad": "REAL",
        "daily_anomalies": "INTEGER",
        "daily_avg_score": "REAL",
    })

    ensure_columns("study_reports", {
        "before_kwh": "REAL",
        "after_kwh": "REAL",
        "savings_kwh": "REAL",
        "savings_percent": "REAL",
        "savings_mad": "REAL",
    })

    ensure_columns("financial_audits", {
        "price_per_kwh": "REAL",
        "energy_total_kwh": "REAL",
        "cost_total_mad": "REAL",
        "energy_before_kwh": "REAL",
        "energy_after_kwh": "REAL",
        "study_before_kwh": "REAL",
        "study_after_kwh": "REAL",
        "study_savings_kwh": "REAL",
        "study_savings_percent": "REAL",
        "study_savings_mad": "REAL",
    })

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_commands_device_status_ts "
        "ON commands(device_id, status, ts)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts "
        "ON telemetry(device_id, ts)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_device_name_phase "
        "ON experiments(device_id, name, phase, ts_start)"
    )

    if not cur.execute(
        "SELECT 1 FROM comfort_profiles WHERE id=?",
        (COMFORT_PROFILE_ID,),
    ).fetchone():
        cur.execute(
            """
            INSERT INTO comfort_profiles
            (id, comfort_min_c, comfort_max_c, critical_temp_c, updated_at)
            VALUES (?,?,?,?,?)
            """,
            (
                COMFORT_PROFILE_ID,
                DEFAULT_COMFORT_PROFILE["comfort_min_c"],
                DEFAULT_COMFORT_PROFILE["comfort_max_c"],
                DEFAULT_COMFORT_PROFILE["critical_temp_c"],
                int(time.time()),
            ),
        )

    if not cur.execute(
        "SELECT 1 FROM app_settings WHERE key='price_per_kwh'"
    ).fetchone():
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?,?,?)
            """,
            ("price_per_kwh", str(DEFAULT_PRICE_PER_KWH), int(time.time())),
        )

    cur.execute("DELETE FROM commands WHERE command IN ('season_hiver', 'season_ete')")
    db.commit()


def bootstrap_db():
    try:
        with app.app_context():
            init_db()
            db = get_db()
            recompute_telemetry_energy(db)
            db.commit()
    except Exception:
        app.logger.exception("DB bootstrap failed")


# ----------------------------
# Helpers
# ----------------------------
def check_token(req):
    return req.headers.get("X-API-TOKEN") == APP_TOKEN


def parse_int_arg(name, default, minimum=None, maximum=None):
    try:
        value = int(request.args.get(name, str(default)))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def append_unique(items, value):
    if value and value not in items:
        items.append(value)


def dumps_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def loads_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_json_or_400():
    try:
        return request.get_json(force=True), None
    except Exception:
        return None, (jsonify({"ok": False, "error": "invalid JSON"}), 400)


def validate_comfort_profile(payload):
    profile = {
        "comfort_min_c": parse_float(payload.get("comfort_min_c"), None),
        "comfort_max_c": parse_float(payload.get("comfort_max_c"), None),
        "critical_temp_c": parse_float(payload.get("critical_temp_c"), None),
    }
    if None in profile.values():
        return None, "comfort profile incomplet"
    if profile["comfort_min_c"] >= profile["comfort_max_c"]:
        return None, "comfort_min_c doit etre inferieur a comfort_max_c"
    if profile["critical_temp_c"] < profile["comfort_max_c"]:
        return None, "critical_temp_c doit etre superieur ou egal a comfort_max_c"
    return profile, None


def get_active_comfort_profile(db=None):
    db = db or get_db()
    row = db.execute(
        """
        SELECT comfort_min_c, comfort_max_c, critical_temp_c
        FROM comfort_profiles
        WHERE id=?
        """,
        (COMFORT_PROFILE_ID,),
    ).fetchone()
    if not row:
        return dict(DEFAULT_COMFORT_PROFILE)
    return {
        "comfort_min_c": parse_float(row["comfort_min_c"], DEFAULT_COMFORT_PROFILE["comfort_min_c"]),
        "comfort_max_c": parse_float(row["comfort_max_c"], DEFAULT_COMFORT_PROFILE["comfort_max_c"]),
        "critical_temp_c": parse_float(row["critical_temp_c"], DEFAULT_COMFORT_PROFILE["critical_temp_c"]),
    }


def save_comfort_profile(db, profile):
    db.execute(
        """
        INSERT OR REPLACE INTO comfort_profiles
        (id, comfort_min_c, comfort_max_c, critical_temp_c, updated_at)
        VALUES (?,?,?,?,?)
        """,
        (
            COMFORT_PROFILE_ID,
            profile["comfort_min_c"],
            profile["comfort_max_c"],
            profile["critical_temp_c"],
            int(time.time()),
        ),
    )


def normalize_telemetry(data):
    return {
        "device_id": str(data.get("device_id", "esp32-1")),
        "mode": data.get("mode"),
        "presence": parse_bool(data.get("presence")),
        "window_open": parse_bool(data.get("window_open")),
        "temp_ok": parse_bool(data.get("temp_ok"), default=True),
        "temp_c": parse_float(data.get("temp_c"), 0.0),
        "humidity": parse_float(data.get("humidity"), 0.0),
        "current_a": parse_float(data.get("current_a"), 0.0),
        "power_w": parse_float(data.get("power_w"), 0.0),
        "energy_total_kwh": parse_float(data.get("energy_total_kwh"), 0.0),
        "energy_before_kwh": parse_float(data.get("energy_before_kwh"), 0.0),
        "energy_after_kwh": parse_float(data.get("energy_after_kwh"), 0.0),
        "cost_mad": parse_float(data.get("cost_mad"), 0.0),
        "lamp_on": parse_bool(data.get("lamp_on")),
        "fan_on": parse_bool(data.get("fan_on")),
        "anomaly_dht_fail": parse_bool(data.get("anomaly_dht_fail")),
        "remote_enabled": parse_bool(data.get("remote_enabled"), default=True),
    }


def compute_alert_count(normalized, evaluation):
    alert_count = len(evaluation.get("energy_anomaly", []))
    if parse_bool(evaluation.get("critical_temp_alert")):
        alert_count += 1
    if normalized["anomaly_dht_fail"] or not normalized["temp_ok"]:
        alert_count += 1
    return alert_count


def serialize_telemetry_row(row, db=None):
    data = dict(row)
    if db is not None:
        energy_total_kwh, cost_mad = get_display_energy_totals(db, row)
        data["energy_total_kwh"] = energy_total_kwh
        data["cost_mad"] = cost_mad
        data["price_per_kwh"] = get_price_per_kwh(db)

    data["presence"] = parse_bool(data.get("presence"))
    data["window_open"] = parse_bool(data.get("window_open"))
    data["temp_ok"] = parse_bool(data.get("temp_ok"), default=True)
    data["humidity"] = parse_float(data.get("humidity"), 0.0)
    data["lamp_on"] = parse_bool(data.get("lamp_on"))
    data["fan_on"] = parse_bool(data.get("fan_on"))
    data["anomaly_dht_fail"] = parse_bool(data.get("anomaly_dht_fail"))
    data["remote_enabled"] = parse_bool(data.get("remote_enabled"), default=True)
    data["critical_temp_alert"] = parse_bool(data.get("critical_temp_alert"))
    data["energy_anomaly"] = loads_json(data.get("energy_anomaly_json"), [])
    data["reasons"] = loads_json(data.get("reasons_json"), [])
    return data


def get_setting(db, key, default=None):
    row = db.execute(
        "SELECT value FROM app_settings WHERE key=?",
        (key,),
    ).fetchone()
    if not row:
        return default
    return row["value"]


def set_setting(db, key, value):
    db.execute(
        """
        INSERT OR REPLACE INTO app_settings (key, value, updated_at)
        VALUES (?,?,?)
        """,
        (key, str(value), int(time.time())),
    )


def get_price_per_kwh(db=None):
    db = db or get_db()
    return parse_float(get_setting(db, "price_per_kwh", DEFAULT_PRICE_PER_KWH), DEFAULT_PRICE_PER_KWH)


def set_price_per_kwh(db, value):
    price = parse_float(value, None)
    if price is None or price <= 0:
        raise ValueError("price_per_kwh must be positive")
    price = round(price, 4)
    set_setting(db, "price_per_kwh", price)
    return price


def energy_totals_reset_setting_key(device_id, suffix):
    return f"{suffix}:{device_id}"


def get_energy_totals_reset_state(db, device_id):
    reset_at = int(
        parse_float(
            get_setting(db, energy_totals_reset_setting_key(device_id, ENERGY_TOTALS_RESET_AT_KEY_PREFIX), 0),
            0,
        )
    )
    offset = max(
        0.0,
        parse_float(
            get_setting(db, energy_totals_reset_setting_key(device_id, ENERGY_TOTALS_RESET_OFFSET_KEY_PREFIX), 0.0),
            0.0,
        ),
    )
    return reset_at, offset


def get_display_energy_totals(db, row):
    if not row:
        return 0.0, 0.0

    raw_energy_total_kwh = parse_float(row["energy_total_kwh"], 0.0)
    row_ts = int(row["ts"] or 0)
    device_id = row["device_id"]
    reset_at, offset_kwh = get_energy_totals_reset_state(db, device_id)

    if reset_at and row_ts <= reset_at:
        display_energy_kwh = 0.0
    else:
        display_energy_kwh = max(0.0, raw_energy_total_kwh - offset_kwh)

    display_energy_kwh = round(display_energy_kwh, 4)
    display_cost_mad = round(display_energy_kwh * get_price_per_kwh(db), 4)
    return display_energy_kwh, display_cost_mad


def mark_energy_totals_reset(db, device_id):
    latest_row = get_latest_telemetry_row(db, device_id)
    offset_kwh = parse_float(latest_row["energy_total_kwh"], 0.0) if latest_row else 0.0
    reset_at = int(time.time())
    set_setting(db, energy_totals_reset_setting_key(device_id, ENERGY_TOTALS_RESET_AT_KEY_PREFIX), reset_at)
    set_setting(
        db,
        energy_totals_reset_setting_key(device_id, ENERGY_TOTALS_RESET_OFFSET_KEY_PREFIX),
        round(max(0.0, offset_kwh), 6),
    )
    return {"reset_at": reset_at, "energy_offset_kwh": round(max(0.0, offset_kwh), 6)}


def get_latest_telemetry_row(db, device_id):
    return db.execute(
        """
        SELECT * FROM telemetry
        WHERE device_id = ?
        ORDER BY ts DESC, id DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()


def integrate_energy_total_kwh(previous_row, current_power_w, current_ts):
    if not previous_row:
        return 0.0

    previous_total = parse_float(previous_row["energy_total_kwh"], 0.0)
    previous_ts = int(previous_row["ts"] or current_ts)
    delta_seconds = max(0, current_ts - previous_ts)
    if delta_seconds <= 0:
        return previous_total

    previous_power_w = parse_float(previous_row["power_w"], 0.0)
    delta_kwh = ((previous_power_w + current_power_w) / 2.0) * delta_seconds / 3600000.0
    return round(previous_total + delta_kwh, 6)


def compute_backend_telemetry_values(db, device_id, current_power_w, current_ts):
    previous_row = get_latest_telemetry_row(db, device_id)
    energy_total_kwh = integrate_energy_total_kwh(previous_row, current_power_w, current_ts)
    price_per_kwh = get_price_per_kwh(db)
    cost_mad = round(energy_total_kwh * price_per_kwh, 6)
    return {
        "energy_total_kwh": energy_total_kwh,
        "cost_mad": cost_mad,
        "price_per_kwh": price_per_kwh,
    }


def recompute_device_telemetry_energy(db, device_id, price_per_kwh=None):
    price_per_kwh = price_per_kwh if price_per_kwh is not None else get_price_per_kwh(db)
    rows = db.execute(
        """
        SELECT id, ts, power_w
        FROM telemetry
        WHERE device_id=?
        ORDER BY ts ASC, id ASC
        """,
        (device_id,),
    ).fetchall()

    total_kwh = 0.0
    previous_ts = None
    previous_power_w = 0.0
    for row in rows:
        ts_value = int(row["ts"] or 0)
        power_w = parse_float(row["power_w"], 0.0)
        if previous_ts is not None and ts_value > previous_ts:
            total_kwh += ((previous_power_w + power_w) / 2.0) * (ts_value - previous_ts) / 3600000.0
        db.execute(
            "UPDATE telemetry SET energy_total_kwh=?, cost_mad=? WHERE id=?",
            (round(total_kwh, 6), round(total_kwh * price_per_kwh, 6), row["id"]),
        )
        previous_ts = ts_value
        previous_power_w = power_w


def recompute_telemetry_energy(db, device_id=None):
    if device_id:
        recompute_device_telemetry_energy(db, device_id)
        return

    rows = db.execute("SELECT DISTINCT device_id FROM telemetry").fetchall()
    for row in rows:
        recompute_device_telemetry_energy(db, row["device_id"])


def energy_total_at_timestamp(db, device_id, ts_value):
    before_row = db.execute(
        """
        SELECT id, ts, energy_total_kwh
        FROM telemetry
        WHERE device_id=? AND ts<=?
        ORDER BY ts DESC, id DESC
        LIMIT 1
        """,
        (device_id, ts_value),
    ).fetchone()
    after_row = db.execute(
        """
        SELECT id, ts, energy_total_kwh
        FROM telemetry
        WHERE device_id=? AND ts>=?
        ORDER BY ts ASC, id ASC
        LIMIT 1
        """,
        (device_id, ts_value),
    ).fetchone()

    if not before_row and not after_row:
        return 0.0
    if before_row and not after_row:
        return parse_float(before_row["energy_total_kwh"], 0.0)
    if not before_row:
        return 0.0

    before_total = parse_float(before_row["energy_total_kwh"], 0.0)
    if not after_row:
        return before_total

    after_total = parse_float(after_row["energy_total_kwh"], before_total)
    before_ts = int(before_row["ts"] or ts_value)
    after_ts = int(after_row["ts"] or ts_value)
    if before_row["id"] == after_row["id"] or after_ts <= before_ts or ts_value <= before_ts:
        return before_total
    if ts_value >= after_ts:
        return after_total

    ratio = (ts_value - before_ts) / float(after_ts - before_ts)
    return before_total + (after_total - before_total) * ratio


def start_study_phase(db, device_id, phase, name=STUDY_EXPERIMENT_NAME):
    ts_now = int(time.time())
    db.execute(
        "UPDATE experiments SET active=0, ts_end=? WHERE device_id=? AND name=? AND active=1",
        (ts_now, device_id, name),
    )
    db.execute(
        """
        INSERT INTO experiments (ts_start, ts_end, device_id, name, phase, active)
        VALUES (?,?,?,?,?,1)
        """,
        (ts_now, None, device_id, name, phase),
    )
    return {"started": True, "phase": phase, "ts_start": ts_now}


def stop_study_phase(db, device_id, phase=None, name=STUDY_EXPERIMENT_NAME):
    params = [device_id, name]
    phase_sql = ""
    if phase:
        phase_sql = " AND phase=?"
        params.append(phase)

    row = db.execute(
        f"""
        SELECT * FROM experiments
        WHERE device_id=? AND name=? AND active=1{phase_sql}
        ORDER BY ts_start DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if not row:
        return None

    ts_now = int(time.time())
    db.execute(
        "UPDATE experiments SET active=0, ts_end=? WHERE id=?",
        (ts_now, row["id"]),
    )
    stopped = dict(row)
    stopped["active"] = 0
    stopped["ts_end"] = ts_now
    return stopped


def get_latest_completed_study(db, device_id, phase, name=STUDY_EXPERIMENT_NAME):
    return db.execute(
        """
        SELECT * FROM experiments
        WHERE device_id=? AND name=? AND phase=? AND active=0 AND ts_end IS NOT NULL
        ORDER BY ts_end DESC, id DESC
        LIMIT 1
        """,
        (device_id, name, phase),
    ).fetchone()


def study_duration_seconds(session_row):
    if not session_row or session_row["ts_end"] is None:
        return 0
    return max(0, int(session_row["ts_end"]) - int(session_row["ts_start"]))


def study_energy_kwh(db, session_row):
    if not session_row:
        return 0.0
    ts_start = int(session_row["ts_start"])
    ts_end = int(session_row["ts_end"] or time.time())
    total = energy_total_at_timestamp(db, session_row["device_id"], ts_end) - energy_total_at_timestamp(
        db,
        session_row["device_id"],
        ts_start,
    )
    return round(max(0.0, total), 6)


def build_study_result(db, device_id, name=STUDY_EXPERIMENT_NAME):
    before_session = get_latest_completed_study(db, device_id, "before", name=name)
    after_session = get_latest_completed_study(db, device_id, "after", name=name)

    before_kwh = study_energy_kwh(db, before_session)
    after_kwh = study_energy_kwh(db, after_session)
    duration_before = study_duration_seconds(before_session)
    duration_after = study_duration_seconds(after_session)

    has_pair = before_session is not None and after_session is not None
    if has_pair:
        normalized = normalize_study_pair(before_kwh, after_kwh)
        before_kwh = normalized["before_kwh"]
        after_kwh = normalized["after_kwh"]
        gain_kwh = normalized["gain_kwh"]
        reduction_percent = normalized["reduction_percent"]
        cost_saving = gain_kwh * get_price_per_kwh(db)
    else:
        gain_kwh = 0.0
        reduction_percent = 0.0
        cost_saving = 0.0

    return {
        "before_kwh": round(before_kwh, 4),
        "after_kwh": round(after_kwh, 4),
        "gain_kwh": round(gain_kwh, 4),
        "reduction_percent": round(reduction_percent, 2),
        "cost_saving": round(cost_saving, 4),
        "duration_before": duration_before,
        "duration_after": duration_after,
        "has_before": before_session is not None,
        "has_after": after_session is not None,
    }


def persist_study_report_snapshot(db, device_id, name=STUDY_EXPERIMENT_NAME):
    result = build_study_result(db, device_id, name=name)
    db.execute(
        """
        INSERT INTO study_reports
        (ts, device_id, before_kwh, after_kwh, savings_kwh, savings_percent, savings_mad)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            int(time.time()),
            device_id,
            result["before_kwh"],
            result["after_kwh"],
            result["gain_kwh"],
            result["reduction_percent"],
            result["cost_saving"],
        ),
    )
    return result


def persist_financial_audit_snapshot(db, device_id, name=STUDY_EXPERIMENT_NAME):
    study = build_study_result(db, device_id, name=name)
    latest_row = get_latest_telemetry_row(db, device_id)
    energy_total_kwh, cost_total_mad = get_display_energy_totals(db, latest_row) if latest_row else (0.0, 0.0)
    price_per_kwh = get_price_per_kwh(db)

    db.execute(
        """
        INSERT INTO financial_audits
        (ts, device_id, price_per_kwh, energy_total_kwh, cost_total_mad,
         energy_before_kwh, energy_after_kwh, study_before_kwh, study_after_kwh,
         study_savings_kwh, study_savings_percent, study_savings_mad)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(time.time()),
            device_id,
            price_per_kwh,
            round(energy_total_kwh, 4),
            round(cost_total_mad, 4),
            study["before_kwh"],
            study["after_kwh"],
            study["before_kwh"],
            study["after_kwh"],
            study["gain_kwh"],
            study["reduction_percent"],
            study["cost_saving"],
        ),
    )
    return {
        "price_per_kwh": price_per_kwh,
        "energy_total_kwh": energy_total_kwh,
        "cost_total_mad": cost_total_mad,
        **study,
    }


def reset_study_state(db, device_id, name=STUDY_EXPERIMENT_NAME):
    ts_now = int(time.time())
    db.execute(
        "DELETE FROM experiments WHERE device_id=? AND name=?",
        (device_id, name),
    )
    db.execute(
        """
        INSERT INTO study_reports
        (ts, device_id, before_kwh, after_kwh, savings_kwh, savings_percent, savings_mad)
        VALUES (?,?,?,?,?,?,?)
        """,
        (ts_now, device_id, 0.0, 0.0, 0.0, 0.0, 0.0),
    )
    return {"reset": True}


def classify_risk_level(avg_power_w):
    if avg_power_w >= 1000:
        return "critical"
    if avg_power_w >= 500:
        return "warning"
    return "normal"


def build_prediction(db, device_id, sample_limit=PREDICTION_SAMPLE_LIMIT):
    rows = db.execute(
        """
        SELECT power_w
        FROM telemetry
        WHERE device_id=?
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (device_id, sample_limit),
    ).fetchall()
    values = [parse_float(row["power_w"], 0.0) for row in rows if row["power_w"] is not None]
    avg_power_w = (sum(values) / len(values)) if values else 0.0
    predicted_kwh_1h = avg_power_w / 1000.0
    predicted_kwh_24h = predicted_kwh_1h * 24.0
    predicted_cost_24h = predicted_kwh_24h * get_price_per_kwh(db)
    return {
        "avg_power_w": round(avg_power_w, 2),
        "predicted_kwh_1h": round(predicted_kwh_1h, 4),
        "predicted_kwh_24h": round(predicted_kwh_24h, 4),
        "predicted_cost_24h": round(predicted_cost_24h, 4),
        "risk_level": classify_risk_level(avg_power_w),
        "sample_count": len(values),
    }


def apply_backend_command_side_effects(db, device_id, command, payload):
    payload = payload if isinstance(payload, dict) else {}

    if command == "set_price":
        price_per_kwh = set_price_per_kwh(db, payload.get("price"))
        recompute_telemetry_energy(db, device_id=device_id)
        return {"price_per_kwh": price_per_kwh}
    if command == "reset_energy_totals":
        return mark_energy_totals_reset(db, device_id)
    if command == "study_start_before":
        return start_study_phase(db, device_id, "before")
    if command == "study_start_after":
        return start_study_phase(db, device_id, "after")
    if command == "study_stop":
        stop_study_phase(db, device_id)
        return persist_study_report_snapshot(db, device_id)
    if command == "study_report":
        return persist_study_report_snapshot(db, device_id)
    if command == "financial_audit":
        return persist_financial_audit_snapshot(db, device_id)
    if command == "reset_study":
        return reset_study_state(db, device_id)
    return None


def queue_command(db, device_id, command, payload=None, source="manual", dedupe_window_seconds=2, commit=True):
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"command '{command}' not allowed")

    payload = payload if isinstance(payload, dict) else {}
    payload_text = dumps_json(payload)
    ts_now = int(time.time())

    for group in COALESCE_GROUPS:
        if command in group:
            marks = ",".join("?" for _ in group)
            db.execute(
                f"""
                DELETE FROM commands
                WHERE device_id=? AND status='pending' AND command IN ({marks})
                """,
                (device_id, *group),
            )
            break
    else:
        recent_same = db.execute(
            """
            SELECT id FROM commands
            WHERE device_id=? AND status='pending' AND command=? AND payload=? AND source=? AND ts>=?
            ORDER BY ts DESC LIMIT 1
            """,
            (device_id, command, payload_text, source, ts_now - dedupe_window_seconds),
        ).fetchone()
        if recent_same:
            return {"queued": False, "deduped": True, "id": recent_same["id"], "pending": None}

    cur = db.execute(
        """
        INSERT INTO commands (ts, device_id, command, payload, status, source)
        VALUES (?,?,?,?, 'pending', ?)
        """,
        (ts_now, device_id, command, payload_text, source),
    )
    pending = db.execute(
        "SELECT COUNT(*) AS n FROM commands WHERE device_id=? AND status='pending'",
        (device_id,),
    ).fetchone()["n"]
    if commit:
        db.commit()
    return {"queued": True, "deduped": False, "id": cur.lastrowid, "pending": int(pending)}


def resolve_auto_command(db, device_id, requested_command, normalized):
    if not requested_command:
        return None, []

    reasons = []
    now_ts = int(time.time())

    if requested_command == "fan_on" and normalized["fan_on"]:
        append_unique(reasons, "Commande auto ignoree: ventilateur deja actif.")
        return None, reasons

    recent_manual = db.execute(
        """
        SELECT command FROM commands
        WHERE device_id=? AND source='manual' AND command IN ('fan_on', 'fan_off', 'fan_auto') AND ts>=?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (device_id, now_ts - MANUAL_OVERRIDE_WINDOW_SECONDS),
    ).fetchone()
    if recent_manual:
        append_unique(reasons, "Commande auto bloquee: priorite temporaire au controle manuel.")
        return None, reasons

    recent_auto = db.execute(
        """
        SELECT id FROM commands
        WHERE device_id=? AND source='auto' AND command=? AND status IN ('pending', 'sent', 'acked') AND ts>=?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (device_id, requested_command, now_ts - AUTO_COMMAND_COOLDOWN_SECONDS),
    ).fetchone()
    if recent_auto:
        append_unique(reasons, "Commande auto bloquee par l'anti-spam backend.")
        return None, reasons

    return requested_command, reasons


@app.errorhandler(Exception)
def handle_api_errors(e):
    if request.path.startswith("/api/"):
        if isinstance(e, HTTPException):
            return jsonify({"ok": False, "error": e.description}), e.code
        return jsonify({"ok": False, "error": str(e)}), 500

    if isinstance(e, HTTPException):
        return e
    return "Internal Server Error", 500


# ----------------------------
# Pages
# ----------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------
# API: comfort profile
# ----------------------------
@app.route("/api/get_comfort_profile")
def get_comfort_profile():
    return jsonify({"ok": True, "profile": get_active_comfort_profile()})


@app.route("/api/set_comfort_profile", methods=["POST"])
def set_comfort_profile():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    profile, profile_error = validate_comfort_profile(data)
    if profile_error:
        return jsonify({"ok": False, "error": profile_error}), 400

    db = get_db()
    save_comfort_profile(db, profile)
    db.commit()
    return jsonify({"ok": True, "profile": profile})


# ----------------------------
# API: ingest telemetry
# ----------------------------
@app.route("/api/ingest", methods=["POST"])
def ingest():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    normalized = normalize_telemetry(data)
    comfort_profile = get_active_comfort_profile(db)
    evaluation = evaluate_energy_state(normalized, comfort_profile)
    alerts = compute_alert_count(normalized, evaluation)
    ts_now = int(time.time())
    backend_values = compute_backend_telemetry_values(
        db,
        normalized["device_id"],
        normalized["power_w"],
        ts_now,
    )
    normalized["energy_total_kwh"] = backend_values["energy_total_kwh"]
    normalized["cost_mad"] = backend_values["cost_mad"]

    queued_auto_command, auto_reasons = resolve_auto_command(
        db,
        normalized["device_id"],
        evaluation.get("auto_command"),
        normalized,
    )
    reasons = list(evaluation.get("reasons", []))
    for reason in auto_reasons:
        append_unique(reasons, reason)

    db.execute(
        """
        INSERT INTO telemetry (
            ts, device_id, mode, presence, window_open, temp_ok, temp_c,
            humidity, current_a, power_w, energy_total_kwh, energy_before_kwh, energy_after_kwh,
            cost_mad, lamp_on, fan_on, anomaly_dht_fail, alerts, remote_enabled,
            thermal_state, recommended_action, critical_temp_alert, energy_anomaly_json,
            comfort_score, auto_command, reasons_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ts_now,
            normalized["device_id"],
            normalized["mode"],
            1 if normalized["presence"] else 0,
            1 if normalized["window_open"] else 0,
            1 if normalized["temp_ok"] else 0,
            normalized["temp_c"],
            normalized["humidity"],
            normalized["current_a"],
            normalized["power_w"],
            normalized["energy_total_kwh"],
            normalized["energy_before_kwh"],
            normalized["energy_after_kwh"],
            normalized["cost_mad"],
            1 if normalized["lamp_on"] else 0,
            1 if normalized["fan_on"] else 0,
            1 if normalized["anomaly_dht_fail"] else 0,
            alerts,
            1 if normalized["remote_enabled"] else 0,
            evaluation["thermal_state"],
            evaluation["recommended_action"],
            1 if evaluation["critical_temp_alert"] else 0,
            dumps_json(evaluation["energy_anomaly"]),
            int(evaluation["comfort_score"]),
            evaluation["auto_command"],
            dumps_json(reasons),
        ),
    )

    auto_queued = False
    if queued_auto_command:
        queue_command(
            db,
            normalized["device_id"],
            queued_auto_command,
            payload={},
            source="auto",
            dedupe_window_seconds=AUTO_COMMAND_COOLDOWN_SECONDS,
            commit=False,
        )
        auto_queued = True

    db.commit()
    return jsonify({"ok": True, "saved": True, "auto_queued": auto_queued})


# ----------------------------
# API: daily report
# ----------------------------
@app.route("/api/daily_report", methods=["POST"])
def daily_report():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    db.execute(
        """
        INSERT INTO daily_reports
        (ts, device_id, date, daily_energy_kwh, daily_cost_mad, daily_anomalies, daily_avg_score)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            int(time.time()),
            data.get("device_id", "esp32-1"),
            data.get("date", str(datetime.now().date())),
            parse_float(data.get("daily_energy_kwh"), 0.0),
            parse_float(data.get("daily_cost_mad"), 0.0),
            int(data.get("daily_anomalies", 0)),
            parse_float(data.get("daily_avg_score"), 0.0),
        ),
    )
    db.commit()
    return jsonify({"ok": True, "saved": True})


# ----------------------------
# API: study report
# ----------------------------
@app.route("/api/study_report", methods=["POST"])
def study_report():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    report = persist_study_report_snapshot(db, data.get("device_id", "esp32-1"))
    db.commit()
    return jsonify({"ok": True, "saved": True, "report": report})


# ----------------------------
# API: financial audit
# ----------------------------
@app.route("/api/financial_audit", methods=["POST"])
def financial_audit():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    if data.get("price_per_kwh") is not None:
        set_price_per_kwh(db, data.get("price_per_kwh"))
        recompute_telemetry_energy(db, device_id=data.get("device_id", "esp32-1"))
    audit = persist_financial_audit_snapshot(db, data.get("device_id", "esp32-1"))
    db.commit()
    return jsonify({"ok": True, "saved": True, "audit": audit})


# ----------------------------
# API: get reports
# ----------------------------
def _daily_rows_from_telemetry(device_id, limit):
    db = get_db()
    rows = db.execute(
        """
        SELECT
            CAST(strftime('%s', date(ts, 'unixepoch', 'localtime')) AS INTEGER) AS ts,
            MIN(device_id) AS device_id,
            date(ts, 'unixepoch', 'localtime') AS date,
            ROUND(MAX(energy_total_kwh) - MIN(energy_total_kwh), 4) AS daily_energy_kwh,
            ROUND(MAX(cost_mad) - MIN(cost_mad), 2) AS daily_cost_mad,
            SUM(
                CASE
                    WHEN critical_temp_alert = 1
                      OR anomaly_dht_fail = 1
                      OR (energy_anomaly_json IS NOT NULL AND energy_anomaly_json <> '[]')
                    THEN 1 ELSE 0
                END
            ) AS daily_anomalies,
            ROUND(AVG(comfort_score), 1) AS daily_avg_score
        FROM telemetry
        WHERE device_id = ?
        GROUP BY date(ts, 'unixepoch', 'localtime')
        ORDER BY date DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/api/daily_reports")
def get_daily_reports():
    device_id = request.args.get("device_id", "esp32-1")
    limit = parse_int_arg("limit", 7, minimum=1, maximum=100)

    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM daily_reports
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()

    reports = [dict(r) for r in rows]
    if not reports:
        reports = _daily_rows_from_telemetry(device_id, limit)

    return jsonify({"ok": True, "reports": reports})


@app.route("/api/study_reports")
def get_study_reports():
    device_id = request.args.get("device_id", "esp32-1")
    limit = parse_int_arg("limit", 5, minimum=1, maximum=100)

    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM study_reports
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()

    reports = [dict(r) for r in rows]
    if not reports:
        computed = build_study_result(db, device_id)
        if computed["has_before"] or computed["has_after"]:
            reports = [
                {
                    "ts": int(time.time()),
                    "device_id": device_id,
                    "before_kwh": computed["before_kwh"],
                    "after_kwh": computed["after_kwh"],
                    "savings_kwh": computed["gain_kwh"],
                    "savings_percent": computed["reduction_percent"],
                    "savings_mad": computed["cost_saving"],
                }
            ]

    return jsonify({"ok": True, "reports": reports})


@app.route("/api/latest_study")
def latest_study():
    device_id = request.args.get("device_id", "esp32-1")

    db = get_db()
    computed = build_study_result(db, device_id)
    if not computed["has_before"] and not computed["has_after"]:
        return jsonify({"ok": True, "data": None})

    return jsonify(
        {
            "ok": True,
            "data": {
                "ts": int(time.time()),
                "device_id": device_id,
                "before_kwh": computed["before_kwh"],
                "after_kwh": computed["after_kwh"],
                "savings_kwh": computed["gain_kwh"],
                "savings_percent": computed["reduction_percent"],
                "savings_mad": computed["cost_saving"],
            },
        }
    )


@app.route("/api/financial_audits")
def get_financial_audits():
    device_id = request.args.get("device_id", "esp32-1")
    limit = parse_int_arg("limit", 5, minimum=1, maximum=100)

    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM financial_audits
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()

    return jsonify({"ok": True, "audits": [dict(r) for r in rows]})


@app.route("/api/daily_scores")
def get_daily_scores():
    device_id = request.args.get("device_id", "esp32-1")
    days = parse_int_arg("days", 7, minimum=1, maximum=365)

    db = get_db()
    rows = db.execute(
        """
        SELECT date, daily_avg_score, daily_energy_kwh, daily_anomalies
        FROM daily_reports
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, days),
    ).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        rows = _daily_rows_from_telemetry(device_id, days)

    rows = list(reversed(rows))
    return jsonify(
        {
            "ok": True,
            "data": {
                "labels": [r.get("date") for r in rows],
                "scores": [parse_float(r.get("daily_avg_score"), 0.0) for r in rows],
                "energy": [parse_float(r.get("daily_energy_kwh"), 0.0) for r in rows],
                "anomalies": [int(r.get("daily_anomalies") or 0) for r in rows],
            },
        }
    )


# ----------------------------
# API: telemetry read
# ----------------------------
@app.route("/api/latest")
def latest():
    device_id = request.args.get("device_id", "esp32-1")

    db = get_db()
    row = db.execute(
        """
        SELECT * FROM telemetry
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()

    if not row:
        return jsonify({"ok": True, "data": None})

    return jsonify({"ok": True, "data": serialize_telemetry_row(row, db)})


@app.route("/api/series")
def series():
    device_id = request.args.get("device_id", "esp32-1")
    metric = request.args.get("metric", "power_w")
    minutes = parse_int_arg("minutes", 60, minimum=1, maximum=24 * 60)

    allowed = {
        "power_w",
        "temp_c",
        "current_a",
        "energy_total_kwh",
        "cost_mad",
        "comfort_score",
        "alerts",
    }
    if metric not in allowed:
        return jsonify({"ok": False, "error": "metric not allowed"}), 400

    ts_min = int(time.time()) - minutes * 60
    db = get_db()
    rows = db.execute(
        f"""
        SELECT ts, {metric} AS v
        FROM telemetry
        WHERE device_id = ? AND ts >= ?
        ORDER BY ts ASC
        """,
        (device_id, ts_min),
    ).fetchall()

    return jsonify({"ok": True, "metric": metric, "points": [{"ts": r["ts"], "v": r["v"]} for r in rows]})


# ----------------------------
# API: study workflow
# ----------------------------
@app.route("/api/study/start_before", methods=["POST"])
def study_start_before():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    result = start_study_phase(db, data.get("device_id", "esp32-1"), "before")
    db.commit()
    return jsonify({"ok": True, **result})


@app.route("/api/study/stop_before", methods=["POST"])
def study_stop_before():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    stopped = stop_study_phase(db, data.get("device_id", "esp32-1"), phase="before")
    report = persist_study_report_snapshot(db, data.get("device_id", "esp32-1"))
    db.commit()
    return jsonify({"ok": True, "stopped": stopped is not None, "report": report})


@app.route("/api/study/start_after", methods=["POST"])
def study_start_after():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    result = start_study_phase(db, data.get("device_id", "esp32-1"), "after")
    db.commit()
    return jsonify({"ok": True, **result})


@app.route("/api/study/stop_after", methods=["POST"])
def study_stop_after():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    db = get_db()
    stopped = stop_study_phase(db, data.get("device_id", "esp32-1"), phase="after")
    report = persist_study_report_snapshot(db, data.get("device_id", "esp32-1"))
    db.commit()
    return jsonify({"ok": True, "stopped": stopped is not None, "report": report})


@app.route("/api/study/result")
def study_result():
    device_id = request.args.get("device_id", "esp32-1")
    result = build_study_result(get_db(), device_id)
    return jsonify(
        {
            "before_kwh": result["before_kwh"],
            "after_kwh": result["after_kwh"],
            "gain_kwh": result["gain_kwh"],
            "reduction_percent": result["reduction_percent"],
            "cost_saving": result["cost_saving"],
            "duration_before": result["duration_before"],
            "duration_after": result["duration_after"],
        }
    )


# ----------------------------
# API: prediction and history
# ----------------------------
@app.route("/api/prediction")
def prediction():
    device_id = request.args.get("device_id", "esp32-1")
    result = build_prediction(get_db(), device_id)
    return jsonify(
        {
            "avg_power_w": result["avg_power_w"],
            "predicted_kwh_1h": result["predicted_kwh_1h"],
            "predicted_kwh_24h": result["predicted_kwh_24h"],
            "predicted_cost_24h": result["predicted_cost_24h"],
            "risk_level": result["risk_level"],
        }
    )


@app.route("/api/history")
def history():
    device_id = request.args.get("device_id", "esp32-1")
    limit = parse_int_arg("limit", 100, minimum=1, maximum=1000)
    rows = get_db().execute(
        """
        SELECT
            ts,
            temp_c,
            humidity,
            power_w,
            energy_total_kwh,
            cost_mad,
            thermal_state,
            comfort_score,
            energy_anomaly_json
        FROM telemetry
        WHERE device_id=?
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()
    return jsonify({"items": [dict(row) for row in rows]})


# ----------------------------
# API: commands
# ----------------------------
@app.route("/api/cmd", methods=["POST"])
def create_cmd():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    device_id = data.get("device_id", "esp32-1")
    command = data.get("command")
    payload = data.get("payload", {})

    if command not in ALLOWED_COMMANDS:
        return jsonify({"ok": False, "error": f"command '{command}' not allowed"}), 400

    db = get_db()
    try:
        result = queue_command(
            db,
            device_id,
            command,
            payload=payload,
            source="manual",
            dedupe_window_seconds=2,
            commit=False,
        )
        backend_result = apply_backend_command_side_effects(db, device_id, command, payload)
        db.commit()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    response = {"ok": True, **result}
    if backend_result is not None:
        response["backend"] = backend_result
    return jsonify(response)


@app.route("/api/pull_cmd")
def pull_cmd():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    device_id = request.args.get("device_id", "esp32-1")
    db = get_db()
    now_ts = int(time.time())

    db.execute(
        "DELETE FROM commands WHERE device_id=? AND status='pending' AND ts < ?",
        (device_id, now_ts - 300),
    )

    compress_groups = [
        ("lamp_on", "lamp_off"),
        ("fan_on", "fan_off", "fan_auto"),
        ("window_open", "window_close"),
        ("set_period",),
        ("set_price",),
        ("daily_report_now",),
    ]
    for group in compress_groups:
        marks = ",".join("?" for _ in group)
        rows = db.execute(
            f"""
            SELECT id FROM commands
            WHERE device_id=? AND status='pending' AND command IN ({marks})
            ORDER BY ts DESC
            """,
            (device_id, *group),
        ).fetchall()
        if len(rows) > 1:
            delete_ids = [str(r["id"]) for r in rows[1:]]
            db.execute(f"DELETE FROM commands WHERE id IN ({','.join(delete_ids)})")

    pending_rows = db.execute(
        """
        SELECT id FROM commands
        WHERE device_id=? AND status='pending'
        ORDER BY ts DESC
        """,
        (device_id,),
    ).fetchall()
    if len(pending_rows) > 30:
        delete_ids = [str(r["id"]) for r in pending_rows[30:]]
        db.execute(f"DELETE FROM commands WHERE id IN ({','.join(delete_ids)})")
    db.commit()

    row = db.execute(
        """
        SELECT * FROM commands
        WHERE device_id = ? AND status = 'pending'
        ORDER BY ts DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()

    if not row:
        return jsonify({"ok": True, "cmd": None})

    db.execute("UPDATE commands SET status='sent' WHERE id=?", (row["id"],))
    db.commit()

    return jsonify(
        {
            "ok": True,
            "cmd": {
                "id": row["id"],
                "command": row["command"],
                "payload": loads_json(row["payload"], {}),
            },
        }
    )


@app.route("/api/cmd_ack", methods=["POST"])
def cmd_ack():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    cmd_id = int(data.get("id", 0))
    status = data.get("status", "acked")

    db = get_db()
    db.execute("UPDATE commands SET status=? WHERE id=?", (status, cmd_id))
    db.commit()

    return jsonify({"ok": True})


# ----------------------------
# API: experiments (compat)
# ----------------------------
@app.route("/api/experiment/start", methods=["POST"])
def exp_start():
    if not check_token(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data, err = get_json_or_400()
    if err:
        return err

    device_id = data.get("device_id", "esp32-1")
    name = data.get("name", "etude")
    phase = data.get("phase", "before")

    if phase not in {"before", "after"}:
        return jsonify({"ok": False, "error": "phase invalid"}), 400

    db = get_db()
    result = start_study_phase(db, device_id, phase, name=name)
    db.commit()

    return jsonify({"ok": True, **result})


@app.route("/api/experiment/report")
def exp_report():
    device_id = request.args.get("device_id", "esp32-1")
    name = request.args.get("name", "etude")

    db = get_db()
    study = build_study_result(db, device_id, name=name)
    reduction = study["reduction_percent"] if study["has_before"] and study["has_after"] else None

    return jsonify(
        {
            "ok": True,
            "device_id": device_id,
            "name": name,
            "before_kwh_est": study["before_kwh"],
            "after_kwh_est": study["after_kwh"],
            "reduction_percent": reduction,
        }
    )


# ----------------------------
# API: AI
# ----------------------------
@app.route("/api/ai/predict")
def ai_predict():
    device_id = request.args.get("device_id", "esp32-1")
    horizon_min = parse_int_arg("horizon_min", 10, minimum=1, maximum=24 * 60)

    prediction_data = build_prediction(get_db(), device_id)
    if prediction_data["sample_count"] == 0:
        return jsonify({"ok": True, "pred_power_w": None, "note": "not enough data"})

    pred_kwh = (prediction_data["avg_power_w"] / 1000.0) * (horizon_min / 60.0)
    return jsonify(
        {
            "ok": True,
            "pred_power_w": prediction_data["avg_power_w"],
            "pred_energy_kwh": round(pred_kwh, 4),
            "horizon_min": horizon_min,
        }
    )


@app.route("/api/ai/anomaly_summary")
def ai_anomaly_summary():
    device_id = request.args.get("device_id", "esp32-1")
    minutes = parse_int_arg("minutes", 60, minimum=1, maximum=24 * 60)
    ts_min = int(time.time()) - minutes * 60

    db = get_db()
    latest = db.execute(
        """
        SELECT temp_ok, anomaly_dht_fail, critical_temp_alert, energy_anomaly_json
        FROM telemetry
        WHERE device_id=?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()

    row = db.execute(
        """
        SELECT
          SUM(
              CASE
                  WHEN critical_temp_alert = 1
                    OR anomaly_dht_fail = 1
                    OR temp_ok = 0
                    OR (energy_anomaly_json IS NOT NULL AND energy_anomaly_json <> '[]')
                  THEN 1 ELSE 0
              END
          ) AS total_anom,
          SUM(CASE WHEN energy_anomaly_json LIKE '%"overpower"%' THEN 1 ELSE 0 END) AS overpower,
          SUM(CASE WHEN critical_temp_alert = 1 THEN 1 ELSE 0 END) AS temp,
          SUM(CASE WHEN anomaly_dht_fail = 1 OR temp_ok = 0 THEN 1 ELSE 0 END) AS dht
        FROM telemetry
        WHERE device_id=? AND ts>=?
        """,
        (device_id, ts_min),
    ).fetchone()

    latest_anomalies = loads_json(latest["energy_anomaly_json"], []) if latest else []
    active_overpower = 1 if "overpower" in latest_anomalies else 0
    active_temp = 1 if latest and parse_bool(latest["critical_temp_alert"]) else 0
    active_dht = 1 if latest and (parse_bool(latest["anomaly_dht_fail"]) or not parse_bool(latest["temp_ok"], default=True)) else 0
    active_total = active_overpower + active_temp + active_dht

    return jsonify(
        {
            "ok": True,
            "minutes": minutes,
            "anomalies": {
                "total": int(active_total),
                "overpower": int(active_overpower),
                "temp": int(active_temp),
                "dht_fail": int(active_dht),
                "window_total": int(row["total_anom"] or 0),
                "window_overpower": int(row["overpower"] or 0),
                "window_temp": int(row["temp"] or 0),
                "window_dht_fail": int(row["dht"] or 0),
            },
        }
    )


# ----------------------------
# main
# ----------------------------
bootstrap_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
