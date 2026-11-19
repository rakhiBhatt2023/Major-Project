"""
===============================================================================
SMART GREEN PARKING SYSTEM - PAYMENT / SESSION MODULE (from the minor project)
===============================================================================
This is the wallet + session + fee logic of smartpark_complete.py, packaged as
functions the Flask app can call. It uses the SAME tables and the SAME
smart_parking.db as the minor project, so drivers and QR codes you already
registered keep working:

    drivers(driver_id, name, vehicle_no, phone, wallet, registered_on)
    sessions(sessionid, driver_id, entrytime, exittime, fee, status)
    parkingrecords(id, driver_id, slotid, entrytime, exittime, amountpaid, status)
    scanlogs(id, qrtext, scantime, result, message)

What changed compared with the minor:
  * The slot is chosen by the AI (green_score.py), not "first free number",
    and is stored in parkingrecords.slotid (e.g. "A1").
  * Entry fee is CREDITED at exit (CREDIT_ENTRY_FEE).
  * Fee formula: round(max(10.0, (minutes - 5.0) * 2.0), 2).
===============================================================================
"""

import math
import uuid
from datetime import datetime

import db

ENTRY_FEE = 10.0          # Base entry fee (Rs 10 covers first 5 minutes)
PER_MINUTE_RATE = 2.0     # Rs 2 per minute after 5 minutes
CREDIT_ENTRY_FEE = True   # entry fee counts towards the final fee
MAX_FEE_CAP = 200.0       # (b) Cap any single fee at Rs 200
STALE_SESSION_HOURS = 12.0 # (a) Any session older than 12 hours is treated as STALE (fee 0)
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _conn():
    return db.get_connection()


def _now():
    return datetime.now().strftime(TIME_FMT)


# ---------------------------------------------------------------------------
# TABLES
# ---------------------------------------------------------------------------
def init_minor_tables():
    """Creates the minor-project tables if missing. Never deletes anything."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            driver_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            vehicle_no TEXT UNIQUE NOT NULL,
            phone TEXT,
            wallet REAL DEFAULT 0,
            registered_on TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            sessionid INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT,
            entrytime TEXT,
            exittime TEXT,
            fee REAL,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parkingrecords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id TEXT,
            slotid INTEGER,
            entrytime TEXT,
            exittime TEXT,
            amountpaid REAL,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scanlogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qrtext TEXT,
            scantime TEXT,
            result TEXT,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_scan(qr_text, result, message):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scanlogs (qrtext, scantime, result, message)
            VALUES (?, ?, ?, ?)
        """, (qr_text, _now(), result, message))
        conn.commit()
        conn.close()
    except Exception as err:
        print("[PAYMENT] Log scan error:", err)


# ---------------------------------------------------------------------------
# LOOKUPS
# ---------------------------------------------------------------------------
def find_driver(qr_text):
    """Supports DRIVER_ID:CAR_NUMBER, DRIVER_ID|..., plain driver_id, and vehicle_no."""
    if not qr_text:
        return None
    raw = str(qr_text).strip()
    if not raw:
        return None

    conn = _conn()
    cur = conn.cursor()

    # 1. Direct match on driver_id or vehicle_no
    cur.execute("""
        SELECT driver_id, name, vehicle_no, phone, wallet,
               name AS driver, vehicle_no AS vehicle, wallet AS wallet_balance
        FROM drivers WHERE driver_id = ? OR vehicle_no = ?
    """, (raw, raw))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)

    # 2. Match candidate before ':' or '|' (e.g. DRIVER_ID:CAR_NUMBER or DRIVER_ID|...)
    candidate = raw.split(":")[0].split("|")[0].strip()
    if candidate and candidate != raw:
        cur.execute("""
            SELECT driver_id, name, vehicle_no, phone, wallet,
                   name AS driver, vehicle_no AS vehicle, wallet AS wallet_balance
            FROM drivers WHERE driver_id = ? OR vehicle_no = ?
        """, (candidate, candidate))
        row = cur.fetchone()
        if row:
            conn.close()
            return dict(row)

    # 3. If ':' was present, check if the second part matches vehicle_no
    if ":" in raw:
        car_part = raw.split(":", 1)[1].strip()
        if car_part:
            cur.execute("""
                SELECT driver_id, name, vehicle_no, phone, wallet,
                       name AS driver, vehicle_no AS vehicle, wallet AS wallet_balance
                FROM drivers WHERE vehicle_no = ?
            """, (car_part,))
            row = cur.fetchone()
            if row:
                conn.close()
                return dict(row)

    conn.close()
    return None


get_user = find_driver


def cleanup_stale_sessions():
    """(a) Any session older than 12 hours is treated as STALE: auto-close with fee 0 and log 'stale session closed', never charge it."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT sessionid, driver_id, entrytime FROM sessions
            WHERE status = 'ACTIVE' OR status != 'COMPLETED'
        """)
        rows = cur.fetchall()
        now_dt = datetime.now()
        now_str = _now()
        closed_count = 0
        for r in rows:
            entry_str = r["entrytime"]
            if not entry_str:
                continue
            try:
                entry_dt = datetime.strptime(entry_str, TIME_FMT)
                elapsed_hours = (now_dt - entry_dt).total_seconds() / 3600.0
                if elapsed_hours >= STALE_SESSION_HOURS:
                    sid = r["sessionid"]
                    did = r["driver_id"]
                    cur.execute("""
                        UPDATE sessions SET exittime = ?, fee = 0.0, status = 'COMPLETED'
                        WHERE sessionid = ?
                    """, (now_str, sid))
                    cur.execute("""
                        UPDATE parkingrecords SET exittime = ?, amountpaid = 0.0, status = 'COMPLETED'
                        WHERE driver_id = ? AND (status = 'PARKED' OR status != 'COMPLETED')
                    """, (now_str, did))
                    print(f"[PAYMENT] Stale session closed: sessionid={sid}, driver={did}, age={elapsed_hours:.1f}h")
                    log_scan(did, "INFO", "stale session closed")
                    try:
                        db.log_gate_event("STALE_SESSION_CLOSED", qr=did, note=f"stale session closed (age: {elapsed_hours:.1f}h, fee 0)")
                    except Exception:
                        pass
                    closed_count += 1
            except Exception as pe:
                pass
        if closed_count > 0:
            conn.commit()
        conn.close()
        return closed_count
    except Exception as e:
        print(f"[PAYMENT] cleanup_stale_sessions error: {e}")
        return 0


def _active_session(driver_id):
    cleanup_stale_sessions()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT sessionid, entrytime, fee FROM sessions
        WHERE driver_id = ? AND status = 'ACTIVE'
        ORDER BY sessionid DESC LIMIT 1
    """, (driver_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def has_active_session(qr_text):
    """True if this QR belongs to a driver who is currently inside."""
    driver = find_driver(qr_text)
    return bool(driver and _active_session(driver["driver_id"]))


# ---------------------------------------------------------------------------
# ENTRY: deduct the fee, open a session, remember the AI-chosen slot
# ---------------------------------------------------------------------------
def start_entry(qr_text, slot_id, fee=ENTRY_FEE):
    """Returns (ok, message, info)."""
    driver = find_driver(qr_text)
    if driver is None:
        log_scan(qr_text, "DENIED", "Vehicle not registered")
        return False, "Vehicle not registered", {}

    driver_id = driver["driver_id"]
    wallet = float(driver["wallet"] or 0)

    if _active_session(driver_id):
        log_scan(qr_text, "DENIED", "Driver already has active session")
        return False, "Driver is already inside", {}

    if wallet < fee:
        log_scan(qr_text, "DENIED", "Low wallet balance")
        return False, f"Low wallet balance (Rs.{wallet:.2f})", {}

    now = _now()
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE drivers SET wallet = wallet - ?
            WHERE driver_id = ? AND wallet >= ?
        """, (fee, driver_id, fee))
        if cur.rowcount != 1:
            conn.rollback()
            log_scan(qr_text, "DENIED", "Low wallet balance")
            return False, "Low wallet balance", {}

        cur.execute("""
            INSERT INTO sessions (driver_id, entrytime, exittime, fee, status)
            VALUES (?, ?, NULL, ?, 'ACTIVE')
        """, (driver_id, now, fee))
        cur.execute("""
            INSERT INTO parkingrecords
            (driver_id, slotid, entrytime, exittime, amountpaid, status)
            VALUES (?, ?, ?, NULL, ?, 'PARKED')
        """, (driver_id, slot_id, now, fee))
        conn.commit()
    except Exception as err:
        conn.rollback()
        log_scan(qr_text, "ERROR", str(err))
        return False, f"Database error: {err}", {}
    finally:
        conn.close()

    log_scan(qr_text, "SUCCESS", f"Entry granted, slot {slot_id}")
    return True, "Entry granted", {
        "driver": driver["name"],
        "vehicle": driver["vehicle_no"],
        "fee": fee,
        "wallet_before": wallet,
        "wallet_after": wallet - fee,
        "slot_id": slot_id,
    }


# ---------------------------------------------------------------------------
# EXIT: work out the final fee, charge the difference, close the session
# ---------------------------------------------------------------------------
def process_exit(qr_text):
    """Returns (ok, message, info)."""
    driver = find_driver(qr_text)
    if driver is None:
        log_scan(qr_text, "DENIED", "Vehicle not registered")
        return False, "Vehicle not registered", {}

    driver_id = driver["driver_id"]
    session = _active_session(driver_id)
    if session is None:
        log_scan(qr_text, "DENIED", "No active session")
        return False, "No active entry for this driver", {}

    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, slotid FROM parkingrecords
        WHERE driver_id = ? AND status = 'PARKED'
        ORDER BY id DESC LIMIT 1
    """, (driver_id,))
    record = cur.fetchone()
    conn.close()

    exit_time = _now()
    entry_dt = datetime.strptime(session["entrytime"], TIME_FMT)
    elapsed_seconds = max(0.0, (datetime.strptime(exit_time, TIME_FMT) - entry_dt).total_seconds())
    minutes = max(0.0, elapsed_seconds / 60.0)
    hours = minutes / 60.0
    wallet = float(driver["wallet"] or 0)

    # (a) Check if session is older than 12 hours: treated as STALE -> auto-close with fee 0, never charge
    if hours >= STALE_SESSION_HOURS:
        conn = _conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE sessions SET exittime = ?, fee = 0.0, status = 'COMPLETED'
                WHERE sessionid = ?
            """, (exit_time, session["sessionid"]))
            if record is not None:
                cur.execute("""
                    UPDATE parkingrecords
                    SET exittime = ?, amountpaid = 0.0, status = 'COMPLETED'
                    WHERE id = ?
                """, (exit_time, record["id"]))
            conn.commit()
        finally:
            conn.close()
        print(f"[PAYMENT] Stale session closed: sessionid={session['sessionid']}, driver={driver_id}, age={hours:.1f}h")
        log_scan(qr_text, "INFO", "stale session closed")
        try:
            db.log_gate_event("STALE_SESSION_CLOSED", qr=qr_text, note=f"stale session closed (age: {hours:.1f}h, fee 0)")
        except Exception:
            pass
        return True, "Stale session closed (fee 0)", {
            "driver": driver["name"],
            "vehicle": driver["vehicle_no"],
            "duration_minutes": round(minutes, 2),
            "duration_hours": round(hours, 3),
            "charged_now": 0.0,
            "total_paid": 0.0,
            "wallet_after": wallet,
            "slot_id": record["slotid"] if record else None,
            "stale": True
        }

    # (b) Cap any single fee at Rs 200, formula: max(10, (minutes - 5) * 2)
    uncapped_fee = round(max(10.0, (minutes - 5.0) * 2.0), 2)
    total_fee = min(MAX_FEE_CAP, uncapped_fee)
    already_paid = float(session["fee"] or 0) if CREDIT_ENTRY_FEE else 0.0
    charge_now = max(0.0, round(total_fee - already_paid, 2))
    paid_total = round(already_paid + charge_now, 2)

    if wallet < charge_now:
        log_scan(qr_text, "DENIED", "Insufficient wallet at exit")
        return False, f"Insufficient wallet (Rs.{wallet:.2f} < Rs.{charge_now:.2f})", {}

    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE drivers SET wallet = wallet - ? WHERE driver_id = ?",
                    (charge_now, driver_id))
        cur.execute("""
            UPDATE sessions SET exittime = ?, fee = ?, status = 'COMPLETED'
            WHERE sessionid = ?
        """, (exit_time, paid_total, session["sessionid"]))
        if record is not None:
            cur.execute("""
                UPDATE parkingrecords
                SET exittime = ?, amountpaid = ?, status = 'COMPLETED'
                WHERE id = ?
            """, (exit_time, paid_total, record["id"]))
        conn.commit()
    except Exception as err:
        conn.rollback()
        log_scan(qr_text, "ERROR", str(err))
        return False, f"Database error: {err}", {}
    finally:
        conn.close()

    log_scan(qr_text, "SUCCESS", f"Exit successful, total Rs.{paid_total:.2f}")
    return True, "Exit approved", {
        "driver": driver["name"],
        "vehicle": driver["vehicle_no"],
        "duration_minutes": round(minutes, 2),
        "duration_hours": round(hours, 3),
        "charged_now": charge_now,
        "total_paid": paid_total,
        "wallet_before": wallet,
        "wallet_after": wallet - charge_now,
        "slot_id": record["slotid"] if record else None,
        "fee_formula": "max(10, (minutes - 5) * 2)"
    }


# ---------------------------------------------------------------------------
# TEST HELPERS (used by test_integration.py; only touch vehicles named TEST-*)
# ---------------------------------------------------------------------------
def create_test_driver(vehicle_no="TEST-0001", wallet=500.0):
    """Creates (or tops up) a test driver and returns its driver_id (= QR text)."""
    if not vehicle_no.startswith("TEST-"):
        raise ValueError("test vehicles must start with TEST-")
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT driver_id FROM drivers WHERE vehicle_no = ?", (vehicle_no,))
    row = cur.fetchone()
    if row:
        driver_id = row["driver_id"]
        cur.execute("UPDATE drivers SET wallet = ? WHERE driver_id = ?", (wallet, driver_id))
    else:
        driver_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO drivers (driver_id, name, vehicle_no, phone, wallet)
            VALUES (?, 'Test Driver', ?, '0000000000', ?)
        """, (driver_id, vehicle_no, wallet))
    conn.commit()
    conn.close()
    return driver_id


def remove_test_driver(vehicle_no="TEST-0001"):
    """Deletes a TEST-* driver and everything tied to it. Real drivers are never touched."""
    if not vehicle_no.startswith("TEST-"):
        raise ValueError("only TEST-* vehicles can be removed")
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT driver_id FROM drivers WHERE vehicle_no = ?", (vehicle_no,))
    row = cur.fetchone()
    if row:
        driver_id = row["driver_id"]
        cur.execute("DELETE FROM sessions WHERE driver_id = ?", (driver_id,))
        cur.execute("DELETE FROM parkingrecords WHERE driver_id = ?", (driver_id,))
        cur.execute("DELETE FROM scanlogs WHERE qrtext = ?", (driver_id,))
        cur.execute("DELETE FROM drivers WHERE driver_id = ?", (driver_id,))
    conn.commit()
    conn.close()