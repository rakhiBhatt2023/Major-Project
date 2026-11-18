"""
SmartPark AI
Objective 3 - Air Quality Database Module

Responsibility:
Rakhi

Stores live MQ-series sensor readings in SQLite.
"""

import sqlite3
from datetime import datetime

from db import DB_PATH, get_connection


def create_air_quality_table():
    """
    Create the air_quality_readings table if it does not already exist.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS air_quality_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sensor TEXT NOT NULL,
            raw_value INTEGER NOT NULL,
            alert_level TEXT NOT NULL,
            exceedance INTEGER NOT NULL,
            message TEXT
        )
    """)

    connection.commit()
    connection.close()


def save_air_quality_reading(alert_data):
    """
    Save one processed air-quality reading into SQLite.

    alert_data is expected to come from air_quality_alert.py.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO air_quality_readings
        (
            timestamp,
            sensor,
            raw_value,
            alert_level,
            exceedance,
            message
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        alert_data.get(
            "timestamp",
            datetime.now().isoformat(timespec="seconds")
        ),
        alert_data["sensor"],
        alert_data["raw_value"],
        alert_data["alert_level"],
        int(alert_data["exceedance"]),
        alert_data["message"]
    ))

    connection.commit()

    reading_id = cursor.lastrowid

    connection.close()

    return reading_id


def get_latest_air_quality():
    """
    Return the latest air-quality reading.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM air_quality_readings
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cursor.fetchone()

    connection.close()

    if row is None:
        return None

    return dict(row)


def get_recent_air_quality(limit=20):
    """
    Return recent air-quality readings.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM air_quality_readings
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_air_quality_summary():
    """
    Generate a simple sustainability summary.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) AS total_readings,

            SUM(
                CASE
                    WHEN alert_level = 'SAFE'
                    THEN 1 ELSE 0
                END
            ) AS safe_count,

            SUM(
                CASE
                    WHEN alert_level = 'WARNING'
                    THEN 1 ELSE 0
                END
            ) AS warning_count,

            SUM(
                CASE
                    WHEN alert_level = 'HARMFUL'
                    THEN 1 ELSE 0
                END
            ) AS harmful_count

        FROM air_quality_readings
    """)

    row = cursor.fetchone()

    connection.close()

    total = row["total_readings"] or 0
    safe = row["safe_count"] or 0
    warning = row["warning_count"] or 0
    harmful = row["harmful_count"] or 0

    exceedances = warning + harmful

    if total > 0:
        exceedance_rate = round(
            (exceedances / total) * 100,
            1
        )
    else:
        exceedance_rate = 0.0

    return {
        "total_readings": total,
        "safe_count": safe,
        "warning_count": warning,
        "harmful_count": harmful,
        "exceedance_rate": exceedance_rate
    }