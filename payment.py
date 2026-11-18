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
    and is stored in parkingrecords.slotid (e.g. "A3").
  * Entry fee is CREDITED at exit (CREDIT_ENTRY_FEE). The minor charged the
    entry fee AND then a full fee again at exit, so the minimum stay cost 40.
    Set CREDIT_ENTRY_FEE = False to get the old behaviour back.
  * No scanner, no serial port and no menu here. Register drivers / make QR
    codes with menu options 1 and 2 of smartpark_complete.py as before.
===============================================================================
"""

import math
import uuid
from datetime import datetime

import db

ENTRY_FEE = 20.0          # Rs, same as the minor
HOURLY_RATE = 20.0        # Rs per started hour, same as the minor
CREDIT_ENTRY_FEE = True   # entry fee counts towards the final fee
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
    """Supports the new QR (just the driver_id) and the old driver_id|... QR."""
    driver_id = (qr_text or "").split("|")[0].strip()
    if not driver_id:
        return None
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT driver_id, name, vehicle_no, phone, wallet,
               name AS driver, vehicle_no AS vehicle, wallet AS wallet_balance
        FROM drivers WHERE driver_id = ?
    """, (driver_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


get_user = find_driver


def _active_session(driver_id):
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
        return False, "No active session for this driver", {}

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
    hours = max(0.0, (datetime.strptime(exit_time, TIME_FMT) - entry_dt).total_seconds() / 3600.0)

    total_fee = max(ENTRY_FEE, math.ceil(hours) * HOURLY_RATE)
    already_paid = float(session["fee"] or 0) if CREDIT_ENTRY_FEE else 0.0
    charge_now = max(0.0, total_fee - already_paid)
    paid_total = already_paid + charge_now

    wallet = float(driver["wallet"] or 0)
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
        "duration_hours": round(hours, 3),
        "charged_now": charge_now,
        "total_paid": paid_total,
        "wallet_after": wallet - charge_now,
        "slot_id": record["slotid"] if record else None,
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