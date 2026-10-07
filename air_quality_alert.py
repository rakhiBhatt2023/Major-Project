"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 3
RAMANDEEP KAUR'S MODULE: AIR QUALITY THRESHOLD + ALERT LOGIC
===============================================================================
This module handles Ramandeep's assigned work for Objective 3:
  1. Define safe vs. harmful threshold values for MQ-135 (and other MQ sensors)
  2. Design the alert logic (when/how an exceedance is flagged and reported)
  3. Prepare sustainability reporting: summarise air-quality alert logs

INPUT:  readings produced by Shubam's mq135_simulator.py / Arduino firmware
        -> {"timestamp": str, "raw_value": int, "status": str}
        NOTE: Shubam's "status" (LOW/MEDIUM/HIGH_RESPONSE) is just a raw
        sensor-response bucket, NOT a safety judgment. This module adds that
        safety layer on top of raw_value.

OUTPUT: a single alert dict per reading, ready for Rakhi's Flask trigger
        and SQLite "air_quality_readings" table + a sustainability summary
===============================================================================
"""

import csv
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIG — safety thresholds for MQ-135 (raw analog value, 0-1023 range)
# ---------------------------------------------------------------------------
# These are Ramandeep's actual safety cutoffs — independent of Shubam's
# LOW/MEDIUM/HIGH_RESPONSE buckets, which only describe sensor response
# strength, not air-quality safety. Values below are placeholders for the
# simulator's 250-450 test range; swap in calibrated MQ-135 ppm-equivalent
# cutoffs once real hardware data is available.
MQ135_SAFE_LIMIT = 300        # below this: SAFE
MQ135_WARNING_LIMIT = 400     # between SAFE_LIMIT and this: WARNING
                               # at/above this: HARMFUL

# Extendable to other MQ sensors used in the project, e.g.:
# MQ7_SAFE_LIMIT = ...
# MQ7_WARNING_LIMIT = ...
SENSOR_THRESHOLDS = {
    "MQ135": {"safe": MQ135_SAFE_LIMIT, "warning": MQ135_WARNING_LIMIT},
}


# ---------------------------------------------------------------------------
# STEP 1 & 2: ALERT LOGIC — classify a reading and decide if it's an exceedance
# ---------------------------------------------------------------------------
def classify_air_quality(raw_value: int, sensor: str = "MQ135") -> str:
    """
    Returns 'SAFE', 'WARNING', or 'HARMFUL' based on Ramandeep's defined
    safety thresholds (not Shubam's raw response buckets).
    """
    limits = SENSOR_THRESHOLDS[sensor]

    if raw_value < limits["safe"]:
        return "SAFE"
    elif raw_value < limits["warning"]:
        return "WARNING"
    else:
        return "HARMFUL"


def check_alert(reading: dict, sensor: str = "MQ135") -> dict:
    """
    Takes one reading dict (timestamp, raw_value, status) from Shubam's
    pipeline and returns an alert-annotated version of it.
    """
    raw_value = reading["raw_value"]
    level = classify_air_quality(raw_value, sensor)
    is_exceedance = level in ("WARNING", "HARMFUL")

    return {
        "timestamp": reading.get("timestamp", datetime.now().isoformat(timespec="seconds")),
        "sensor": sensor,
        "raw_value": raw_value,
        "alert_level": level,
        "exceedance": is_exceedance,
        "message": (
            f"{sensor} reading {raw_value} is within safe range."
            if level == "SAFE" else
            f"{sensor} reading {raw_value} has crossed the {level.lower()} threshold."
        ),
    }


# ---------------------------------------------------------------------------
# Process a batch of readings (e.g. the whole CSV, or a live serial buffer)
# ---------------------------------------------------------------------------
def process_readings(readings: list, sensor: str = "MQ135") -> list:
    """Applies check_alert() to a list of raw readings."""
    return [check_alert(r, sensor) for r in readings]


def load_readings_from_csv(filepath: str) -> list:
    """Reads Shubam's mq135_test_data.csv into a list of reading dicts."""
    readings = []
    with open(filepath, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            readings.append({
                "timestamp": row["timestamp"],
                "raw_value": int(row["raw_value"]),
                "status": row["status"],
            })
    return readings


# ---------------------------------------------------------------------------
# STEP 3: SUSTAINABILITY REPORTING — summarise the alert log
# ---------------------------------------------------------------------------
def summarize_alerts(alert_log: list) -> dict:
    """
    Produces a summary of an alert log for the dashboard/report:
    counts per level, exceedance rate, worst reading, etc.
    """
    total = len(alert_log)
    if total == 0:
        return {
            "total_readings": 0,
            "safe_count": 0,
            "warning_count": 0,
            "harmful_count": 0,
            "exceedance_rate": 0.0,
            "worst_reading": None,
        }

    safe_count = sum(1 for a in alert_log if a["alert_level"] == "SAFE")
    warning_count = sum(1 for a in alert_log if a["alert_level"] == "WARNING")
    harmful_count = sum(1 for a in alert_log if a["alert_level"] == "HARMFUL")
    exceedance_count = warning_count + harmful_count

    worst = max(alert_log, key=lambda a: a["raw_value"])

    return {
        "total_readings": total,
        "safe_count": safe_count,
        "warning_count": warning_count,
        "harmful_count": harmful_count,
        "exceedance_rate": round((exceedance_count / total) * 100, 1),
        "worst_reading": {
            "timestamp": worst["timestamp"],
            "raw_value": worst["raw_value"],
            "alert_level": worst["alert_level"],
        },
    }


# ---------------------------------------------------------------------------
# FINAL OUTPUT — packaged for Rakhi's API / SQLite log / LCD trigger
# ---------------------------------------------------------------------------
def get_latest_alert(reading: dict, sensor: str = "MQ135") -> dict:
    """
    This is the single function Rakhi calls for each live reading.
    Returns the exact fields her air_quality_readings table and
    dashboard/LCD alert trigger need.
    """
    return check_alert(reading, sensor)


# ---------------------------------------------------------------------------
# Debug / standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    readings = load_readings_from_csv("mq135_test_data.csv")

    print("\n------------------------------------------")
    print("PER-READING ALERT STATUS")
    print("------------------------------------------")
    alert_log = process_readings(readings)
    for a in alert_log:
        print(f"{a['timestamp']} | raw={a['raw_value']:<4} | "
              f"level={a['alert_level']:<8} | exceedance={a['exceedance']}")

    print("\n------------------------------------------")
    print("SUSTAINABILITY SUMMARY (for dashboard/report)")
    print("------------------------------------------")
    summary = summarize_alerts(alert_log)
    print(summary)

    print("\n------------------------------------------")
    print("SAMPLE LIVE OUTPUT FOR RAKHI (single reading)")
    print("------------------------------------------")
    sample = readings[0] if readings else {"timestamp": "n/a", "raw_value": 0, "status": "n/a"}
    print(get_latest_alert(sample))