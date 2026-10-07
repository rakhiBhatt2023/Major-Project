"""
SmartPark AI - Objective 3
Demo feeder (use ONLY when Arduino is not connected).

Generates MQ-135 style readings and sends them through the real pipeline:
    reading -> air_quality_alert.py -> SQLite -> dashboard
"""

import random
import time

from air_quality_db import create_air_quality_table
from air_quality_monitor import process_sensor_reading

INTERVAL_SECONDS = 2


def main():
    create_air_quality_table()
    print("Air-quality demo feeder started. Press Ctrl+C to stop.")

    try:
        while True:
            raw_value = random.randint(250, 450)
            process_sensor_reading(raw_value)
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nDemo feeder stopped.")


if __name__ == "__main__":
    main()