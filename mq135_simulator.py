import random
import time
import csv
from datetime import datetime

NUMBER_OF_READINGS = 20
OUTPUT_FILE = "mq135_test_data.csv"


def generate_sensor_reading():
    # Fake MQ-135 sensor reading generate karta hai
    return random.randint(250, 450)


def classify_reading(raw_value):
    # Sirf demonstration ke liye categories
    if raw_value < 300:
        return "LOW_RESPONSE"
    elif raw_value < 400:
        return "MEDIUM_RESPONSE"
    else:
        return "HIGH_RESPONSE"


def main():
    print("=" * 45)
    print("SmartPark - MQ-135 Sensor Simulator")
    print("Developer: Shubam Sarmal")
    print("=" * 45)

    with open(OUTPUT_FILE, "w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            "timestamp",
            "raw_value",
            "status"
        ])

        for i in range(NUMBER_OF_READINGS):
            raw_value = generate_sensor_reading()
            status = classify_reading(raw_value)

            timestamp = datetime.now().isoformat(
                timespec="seconds"
            )

            writer.writerow([
                timestamp,
                raw_value,
                status
            ])

            print(
                f"Reading {i + 1}: "
                f"Raw Value = {raw_value}, "
                f"Status = {status}"
            )

            time.sleep(1)

    print("\nSimulation completed!")
    print(f"Data saved in: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()