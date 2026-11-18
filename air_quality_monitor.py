"""
SmartPark AI
Objective 3 - Air Quality Monitor

Responsibility:
Rakhi

Flow:

MQ-135
   ↓
Arduino
   ↓
Serial
   ↓
Python
   ↓
air_quality_alert.py
   ↓
SQLite
   ↓
Dashboard / LCD Alert
"""

import time
import json

from air_quality_alert import check_alert
from air_quality_db import (
    create_air_quality_table,
    save_air_quality_reading
)


def process_sensor_reading(raw_value, timestamp=None):
    """
    Process one raw MQ-135 sensor value.

    This function receives the raw value from Arduino
    or simulator, sends it to the threshold module,
    and stores the processed result in SQLite.
    """

    if timestamp is None:
        from datetime import datetime
        timestamp = datetime.now().isoformat(
            timespec="seconds"
        )

    reading = {
        "timestamp": timestamp,
        "raw_value": int(raw_value),
        "status": "SENSOR_READING"
    }

    # Ramandeep's threshold/alert module
    alert_data = check_alert(
        reading,
        sensor="MQ135"
    )

    # Save processed reading in database
    reading_id = save_air_quality_reading(
        alert_data
    )

    # Log to console only when threshold exceeded (keeps terminal clean)
    if alert_data.get("exceedance"):
        print(f"[AIR ALERT] {alert_data['sensor']} raw={alert_data['raw_value']} ({alert_data['alert_level']})")

    return alert_data


def process_serial_json(serial_data):
    """
    Process JSON data received from Arduino.

    Expected format:

    {
        "sensor": "MQ135",
        "raw_value": 350
    }
    """

    try:

        data = json.loads(serial_data)

        raw_value = data["raw_value"]

        timestamp = data.get("timestamp")

        return process_sensor_reading(
            raw_value,
            timestamp
        )

    except json.JSONDecodeError:

        print("Invalid JSON received:")
        print(serial_data)

        return None

    except KeyError:

        print("MQ135 raw_value missing from sensor data.")

        return None


def initialize_air_quality_system():
    """
    Initialize Objective 3 database components.
    """

    create_air_quality_table()

    print("Air-quality database initialized.")


if __name__ == "__main__":

    initialize_air_quality_system()

    # Test readings
    test_values = [
        250,
        295,
        320,
        350,
        410,
        450
    ]

    for value in test_values:

        process_sensor_reading(value)

        time.sleep(1)