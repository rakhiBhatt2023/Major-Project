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

Called from app.py — Rakhi does NOT compute scores here, only stores/reads data
produced by Ramandeep's green_score.py and Shubam's realtime_tracker.py.
===============================================================================
"""

import sqlite3
from datetime import datetime

DB_PATH = "smart_parking.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Creates all Objective-2 tables if they don't already exist. Safe to call every startup."""
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

    conn.commit()
    conn.close()
    print("[DB] smart_parking.db ready — slot_coordinates, congestion_history, recommendation_log")


def sync_slot_coordinates(slots_dict):
    """
    One-time sync of Shubam's static slot layout (tracker.slots) into SQLite,
    so coordinates + distance-from-gate persist outside the Python process too.
    slots_dict format: {"A1": {"bbox": (x, y, w, h), "distance": 5.0}, ...}
    """
    conn = get_connection()
    cur = conn.cursor()
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