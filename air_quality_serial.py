"""
SmartPark AI
Objective 3 - Arduino Serial Reader

Reads MQ-135 values sent by Arduino over Serial
and passes them to the air-quality backend.
"""

import serial
import json

from air_quality_monitor import process_serial_json


# CHANGE THIS PORT according to your Arduino
SERIAL_PORT = "COM3"

BAUD_RATE = 9600


def start_serial_monitor():

    print("----------------------------------------")
    print("SmartPark AI - MQ-135 Serial Monitor")
    print("----------------------------------------")

    try:

        arduino = serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
            timeout=1
        )

        print("Connected to Arduino:", SERIAL_PORT)

        while True:

            line = arduino.readline().decode(
                "utf-8",
                errors="ignore"
            ).strip()

            if not line:
                continue

            print("Arduino:", line)

            process_serial_json(line)

    except serial.SerialException as error:

        print("Arduino serial connection failed.")
        print("Error:", error)

        print()
        print("System can continue running.")
        print("Use dashboard/database manually or")
        print("reconnect Arduino and restart monitor.")


if __name__ == "__main__":

    start_serial_monitor()