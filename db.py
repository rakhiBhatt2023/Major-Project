"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 2
RAKHI'S MODULE: SQLITE DATABASE LAYER (smart_parking.db)
===============================================================================
This module handles Rakhi's assigned work for Objective 2:
  1. SQLite table for slot coordinates + distance-from-gate + congestion history
  2. Logging every recommendation event (timestamp, slot chosen, score)

Tables:
  - slot_coordinates    : static per-slot bbox + distance-from-gate (Objective 1
                           schema extension, filled from Shubam's tracker.slots)
  - congestion_history  : one row per slot per poll (time-series congestion log)
  - recommendation_log  : one row per AI recommendation event (for reporting)
  - gate_events         : one row per gate/IR event (entry, exit, denied, full)
                           -> evidence that the minor (gate) works with the major

Called from app.py — Rakhi does NOT compute scores here, only stores/reads data
produced by Ramandeep's green_score.py and Shubam's realtime_tracker.py.
===============================================================================
"""

import os
import sqlite3
from datetime import datetime

# absolute path: default smart_parking.db in workspace root
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_parking.db")
DB_PATH = os.environ.get("SMART_PARKING_DB", DEFAULT_DB_PATH)


def set_db_path(path: str):
    """Overrides the database path for tests to prevent modifying smart_parking.db."""
    global DB_PATH
    DB_PATH = path
    os.environ["SMART_PARKING_DB"] = path


def reset_db_path():
    """Resets database path back to smart_parking.db."""
    global DB_PATH
    DB_PATH = DEFAULT_DB_PATH
    os.environ.pop("SMART_PARKING_DB", None)


def get_db_path() -> str:
    return DB_PATH


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db():
    """Creates all tables if they don't already exist. Safe to call every startup."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS slot_coordinates (
            slot_id             TEXT PRIMARY KEY,
            bbox_x              INTEGER,
            bbox_y              INTEGER,
            bbox_w              INTEGER,
            bbox_h              INTEGER,
            distance_from_gate  REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS congestion_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id           TEXT NOT NULL,
            congestion        INTEGER NOT NULL,
            congestion_level  TEXT NOT NULL,
            timestamp         TEXT NOT NULL,
            FOREIGN KEY (slot_id) REFERENCES slot_coordinates(slot_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recommendation_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id           TEXT,
            score             REAL,
            distance          REAL,
            congestion        INTEGER,
            congestion_level  TEXT,
            timestamp         TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gate_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event      TEXT NOT NULL,
            qr         TEXT,
            slot_id    TEXT,
            note       TEXT,
            timestamp  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS slot_reservations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id      TEXT NOT NULL,
            tx_id        TEXT NOT NULL,
            qr           TEXT NOT NULL,
            status       TEXT NOT NULL,
            reserved_at  TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS parking_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id  TEXT NOT NULL UNIQUE,
            qr              TEXT NOT NULL,
            action          TEXT NOT NULL,
            status          TEXT NOT NULL,
            fee             REAL,
            slot_id         TEXT,
            timestamp       TEXT NOT NULL,
            error_reason    TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] smart_parking.db ready — slot_coordinates, congestion_history, "
          "recommendation_log, gate_events, slot_reservations, parking_transactions")


def sync_slot_coordinates(slots_dict):
    """
    One-time sync of Shubam's static slot layout (tracker.slots) into SQLite,
    so coordinates + distance-from-gate persist outside the Python process too.
    slots_dict format: {"A1": {"bbox": (x, y, w, h), "distance": 5.0}, ...}
    """
    conn = get_connection()
    cur = conn.cursor()
    if slots_dict:
        placeholders = ",".join("?" for _ in slots_dict)
        cur.execute(f"DELETE FROM slot_coordinates WHERE slot_id NOT IN ({placeholders})", list(slots_dict.keys()))
    for slot_id, data in slots_dict.items():
        x, y, w, h = data["bbox"]
        cur.execute("""
            INSERT INTO slot_coordinates (slot_id, bbox_x, bbox_y, bbox_w, bbox_h, distance_from_gate)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET
                bbox_x = excluded.bbox_x,
                bbox_y = excluded.bbox_y,
                bbox_w = excluded.bbox_w,
                bbox_h = excluded.bbox_h,
                distance_from_gate = excluded.distance_from_gate
        """, (slot_id, x, y, w, h, data["distance"]))
    conn.commit()
    conn.close()


def log_congestion(feed: dict):
    """
    Appends one congestion_history row per slot for this poll.
    feed = tracker.get_live_slot_feed() (Shubam's output).
    """
    conn = get_connection()
    cur = conn.cursor()
    ts = datetime.now().isoformat()
    for slot_id, s in feed.items():
        cur.execute("""
            INSERT INTO congestion_history (slot_id, congestion, congestion_level, timestamp)
            VALUES (?, ?, ?, ?)
        """, (slot_id, s["congestion"], s["congestion_level"], ts))
    conn.commit()
    conn.close()


def log_recommendation(rec: dict):
    """
    Appends one row to recommendation_log.
    rec = green_score.get_recommended_slot(feed) (Ramandeep's output).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO recommendation_log (slot_id, score, distance, congestion, congestion_level, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        rec.get("slot_id"),
        rec.get("score"),
        rec.get("distance"),
        rec.get("congestion"),
        rec.get("congestion_level"),
        rec.get("timestamp", datetime.now().isoformat()),
    ))
    conn.commit()
    conn.close()


def get_recommendation_history(limit: int = 50):
    """Most recent recommendation events first — used by /api/recommend/history."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM recommendation_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_slot_coordinates():
    """All static slot coordinate + distance rows — useful for admin/debug views."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM slot_coordinates ORDER BY slot_id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# GATE EVENTS (minor <-> major integration evidence)
# ---------------------------------------------------------------------------
def log_gate_event(event: str, qr: str = None, slot_id: str = None, note: str = None):
    """
    Appends one row to gate_events.
    event examples: IR_ENTRY, IR_EXIT, ENTRY_GRANTED, ENTRY_DENIED_FULL,
                    ENTRY_DENIED_PAYMENT, EXIT_GRANTED, MANUAL_OPEN_ENTRY
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO gate_events (event, qr, slot_id, note, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (event, qr, slot_id, note, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def get_gate_events(limit: int = 50):
    """Most recent gate events first — used by /api/gate/events with driver name & vehicle lookup."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT g.*, d.name AS driver_name, d.vehicle_no
        FROM gate_events g
        LEFT JOIN drivers d ON g.qr = d.driver_id
        ORDER BY g.id DESC LIMIT ?
    """, (limit,))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        if d.get("driver_name"):
            v_no = d.get("vehicle_no") or ""
            d["driver_display"] = f"{d['driver_name']} ({v_no})" if v_no else d['driver_name']
        elif d.get("qr"):
            short = d["qr"][:8] if len(d["qr"]) >= 8 else d["qr"]
            d["driver_display"] = f"Unregistered QR ({short})"
        else:
            d["driver_display"] = None
        rows.append(d)
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# SLOT RESERVATIONS & TRANSACTIONS
# ---------------------------------------------------------------------------
def log_slot_reservation(slot_id: str, tx_id: str, qr: str, duration: float = 60.0):
    """Appends one row to slot_reservations."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now()
    expires_at = datetime.fromtimestamp(now.timestamp() + duration)
    cur.execute("""
        INSERT INTO slot_reservations (slot_id, tx_id, qr, status, reserved_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (slot_id, tx_id, qr, 'RESERVED', now.isoformat(timespec='seconds'), expires_at.isoformat(timespec='seconds')))
    conn.commit()
    conn.close()


def update_slot_reservation_status(tx_id: str, status: str):
    """Updates the status of a slot reservation by transaction ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE slot_reservations SET status = ? WHERE tx_id = ?
    """, (status, tx_id))
    conn.commit()
    conn.close()


def log_parking_transaction(transaction_id: str, qr: str, action: str, status: str, fee: float = None, slot_id: str = None, error_reason: str = None):
    """Appends one row to parking_transactions."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO parking_transactions (transaction_id, qr, action, status, fee, slot_id, timestamp, error_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (transaction_id, qr, action, status, fee, slot_id, datetime.now().isoformat(timespec='seconds'), error_reason))
    conn.commit()
    conn.close()


def get_parking_transactions(limit: int = 50):
    """Most recent transactions first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM parking_transactions ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_recent_sessions(limit: int = 50):
    """Returns most recent parking sessions joined with driver details for dashboard display."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.sessionid, s.driver_id, COALESCE(d.name, s.driver_id) AS driver,
               COALESCE(d.vehicle_no, '-') AS vehicle,
               r.slotid AS slot_id, s.entrytime, s.exittime, s.fee, s.status
        FROM sessions s
        LEFT JOIN drivers d ON s.driver_id = d.driver_id
        LEFT JOIN (
            SELECT driver_id, slotid, entrytime, status
            FROM parkingrecords
            GROUP BY driver_id, entrytime
        ) r ON s.driver_id = r.driver_id AND r.entrytime = s.entrytime
        ORDER BY s.sessionid DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_slot_occupants():
    """Returns mapping of slot_id -> current parked driver & vehicle details with elapsed duration."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT pr.slotid, pr.entrytime, pr.amountpaid, pr.status,
               d.driver_id, d.name AS driver_name, d.vehicle_no, d.wallet
        FROM parkingrecords pr
        JOIN drivers d ON pr.driver_id = d.driver_id
        WHERE pr.status = 'PARKED'
        ORDER BY pr.id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    occupants = {}
    now = datetime.now()
    for r in rows:
        sid = str(r["slotid"]).strip().upper()
        if sid not in occupants:
            entry_str = r["entrytime"]
            dur_str = "--"
            dur_mins = 0.0
            try:
                entry_dt = datetime.strptime(entry_str, "%Y-%m-%d %H:%M:%S")
                elapsed_sec = max(0.0, (now - entry_dt).total_seconds())
                mins = int(elapsed_sec // 60)
                secs = int(elapsed_sec % 60)
                dur_mins = round(elapsed_sec / 60.0, 1)
                dur_str = f"{mins}m {secs}s" if mins < 60 else f"{mins // 60}h {mins % 60}m"
            except Exception:
                pass
            occupants[sid] = {
                "driver": r["driver_name"],
                "vehicle": r["vehicle_no"],
                "entrytime": entry_str,
                "duration": dur_str,
                "duration_minutes": dur_mins,
                "paid": r["amountpaid"]
            }
    return occupants


def get_kpis(feed=None):
    """Calculates live KPI metrics: Free slots, Occupied, Active sessions, Collected today (Rs)."""
    conn = get_connection()
    cur = conn.cursor()

    # Active sessions in DB
    cur.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE' OR status != 'COMPLETED'")
    active_row = cur.fetchone()
    active_sessions = active_row[0] if active_row else 0

    # Occupied slots must agree with active sessions and camera feed:
    # A slot is occupied when camera sees a car OR an active session is assigned to it.
    if feed:
        occupied_slots = sum(1 for s in feed.values() if s.get("status") in ("OCCUPIED", "ASSIGNED", "RESERVED") or not s.get("vacant", False))
        occupied_slots = max(occupied_slots, active_sessions)
        free_slots = max(0, len(feed) - occupied_slots)
    else:
        occupied_slots = active_sessions
        free_slots = max(0, 4 - occupied_slots)

    # Collected today: must sum only today's completed session fees (capped at Rs 200) + today's active session fees
    today_str = datetime.now().strftime("%Y-%m-%d")
    cur.execute("""
        SELECT sessionid, entrytime, exittime, fee, status
        FROM sessions
        WHERE (entrytime LIKE ? OR exittime LIKE ?)
    """, (f"{today_str}%", f"{today_str}%"))
    session_rows = cur.fetchall()

    collected_today = 0.0
    for r in session_rows:
        s_fee = float(r["fee"] or 0.0)
        s_entry = str(r["entrytime"] or "")
        s_exit = str(r["exittime"] or "")
        s_status = str(r["status"] or "")

        # Cap single fee at 200
        capped_fee = min(200.0, max(0.0, s_fee))

        if s_status == 'ACTIVE':
            if s_entry.startswith(today_str):
                collected_today += capped_fee
        elif s_status == 'COMPLETED':
            if s_entry.startswith(today_str) and s_exit.startswith(today_str):
                collected_today += capped_fee
            elif s_exit.startswith(today_str):
                exit_charge = max(0.0, capped_fee - 10.0)
                collected_today += exit_charge
            elif s_entry.startswith(today_str):
                collected_today += min(10.0, capped_fee)

    conn.close()

    return {
        "free_slots": free_slots,
        "occupied_slots": occupied_slots,
        "active_sessions": active_sessions,
        "collected_today": round(float(collected_today), 2)
    }


def reset_demo_data(backup: bool = True, recalibrate: bool = False, tracker = None):
    """Executes full demo reset:
    (a) copy smart_parking.db to smart_parking_backup_<timestamp>.db
    (b) close/delete all active sessions, parking records, and slot reservations without charging anyone
    (c) reset every driver wallet to Rs 500
    (d) clear the Last Transaction card, the stepper and the live activity feed (gate_events)
    (e) reset camera mode to Slot Detection and direction to ENTRY, clear slot freeze and manual overrides
    (f) optional recalibration of empty slots if requested
    Do NOT delete drivers, QR codes, air quality history or settings.
    Do NOT send gate/servo commands.
    """
    import shutil
    from datetime import datetime

    backup_name = None
    target_db = DB_PATH
    if backup and os.path.exists(target_db):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"smart_parking_backup_{ts}.db"
        backup_path = os.path.join(os.path.dirname(os.path.abspath(target_db)), backup_name)
        try:
            shutil.copy2(target_db, backup_path)
            print(f"[RESET DEMO] Backup created: {backup_name}")
        except Exception as e:
            print(f"[RESET DEMO] Backup error: {e}")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sessions")
    sess_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM parkingrecords")
    rec_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM drivers")
    driver_cnt = cur.fetchone()[0]

    cur.execute("DELETE FROM sessions")
    cur.execute("DELETE FROM parkingrecords")
    cur.execute("DELETE FROM slot_reservations")
    cur.execute("DELETE FROM parking_transactions")
    cur.execute("DELETE FROM gate_events")

    cur.execute("UPDATE drivers SET wallet = 500.0")

    conn.commit()
    conn.close()

    try:
        from gate_routes import _latest_scan_result, stepper, set_gate_direction
        _latest_scan_result.update({
            "qr": None,
            "timestamp": None,
            "time": None,
            "action": None,
            "status": "AWAITING_SCAN",
            "message": "Awaiting scan",
            "driver": None,
            "driver_display": None,
            "vehicle": None,
            "slot_id": None,
            "fee": None,
            "wallet_before": None,
            "wallet_after": None,
            "duration_minutes": None,
            "payment": {},
            "recommended_slot": {},
            "success": None
        })
        stepper.reset(direction="ENTRY", is_demo=False)
        set_gate_direction("ENTRY")
    except Exception as e:
        print(f"[RESET DEMO] In-memory UI reset error: {e}")

    calib_text = ""
    if tracker:
        try:
            tracker.set_camera_mode("SLOT_DETECTION", reason="Reset Demo triggered")
            tracker.qr_freeze_until = 0.0
            tracker.reservations.clear()
            for sid in tracker.slot_debounce:
                tracker.slot_debounce[sid]["pending_since"] = 0.0
            if recalibrate:
                ok = tracker.calibrate_baseline()
                calib_text = " Empty slots recalibrated." if ok else " Calibration failed."
        except Exception as e:
            print(f"[RESET DEMO] Tracker reset error: {e}")

    summary = (
        f"Reset complete: {sess_cnt} sessions and {rec_cnt} parking records cleared. "
        f"{driver_cnt} driver wallets reset to Rs. 500.00. Stepper, last transaction, and activity feed cleared. "
        f"Camera mode set to Slot Detection (ENTRY). Backup: {backup_name or 'None'}.{calib_text}"
    )
    print(f"[RESET DEMO] {summary}")
    return {
        "status": "ok",
        "summary": summary,
        "backup_file": backup_name,
        "sessions_cleared": sess_cnt,
        "records_cleared": rec_cnt,
        "wallets_reset": driver_cnt,
        "recalibrated": recalibrate
    }


# ---------------------------------------------------------------------------
# Debug / standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from realtime_tracker import RealtimeParkingTracker
    from green_score import get_recommended_slot

    init_db()

    tracker = RealtimeParkingTracker()
    sync_slot_coordinates(tracker.slots)

    feed = tracker.get_live_slot_feed()
    log_congestion(feed)

    rec = get_recommended_slot(feed)
    log_recommendation(rec)

    print("\nSlot coordinates table:")
    for row in get_slot_coordinates():
        print(row)

    print("\nRecommendation history:")
    for row in get_recommendation_history():
        print(row)