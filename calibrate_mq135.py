"""
SmartPark AI - MQ-135 calibration helper

Run this with app.py STOPPED (two programs cannot share the COM port).

  1. Warm-up (3 minutes) so the sensor settles.
  2. Clean-air baseline (60 seconds): keep the sensor away from smoke/alcohol.
  3. Trigger test (30 seconds): hold an alcohol wipe or incense near the sensor.

It then prints SUGGESTED limits for air_quality_alert.py. They are a starting
point worked out from YOUR measurements - check them against what you see.
"""

import json
import time

import serial

from serial_manager import find_arduino_port, SERIAL_PORT, BAUD_RATE

WARMUP_SECONDS = 180
BASELINE_SECONDS = 60
TRIGGER_SECONDS = 30


def read_values(ser, seconds, label):
    values = []
    end = time.time() + seconds
    while time.time() < end:
        raw = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw.startswith("{"):
            continue
        try:
            values.append(int(json.loads(raw)["raw_value"]))
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        left = int(end - time.time())
        print(f"  [{label}] raw={values[-1]:<5} ({left}s left)", end="\r")
    print()
    return values


def main():
    port = SERIAL_PORT or find_arduino_port()
    if not port:
        print("No Arduino found. Plug it in, or set SERIAL_PORT in serial_manager.py.")
        return

    print(f"Opening {port} ...")
    ser = serial.Serial(port, BAUD_RATE, timeout=1)
    time.sleep(2.5)
    ser.reset_input_buffer()

    print(f"\nSTEP 1: warm-up for {WARMUP_SECONDS} seconds (clean air).")
    read_values(ser, WARMUP_SECONDS, "warm-up")

    print(f"\nSTEP 2: clean-air baseline for {BASELINE_SECONDS} seconds.")
    base = read_values(ser, BASELINE_SECONDS, "baseline")
    if not base:
        print("No readings received. Is the merged sketch uploaded?")
        return

    input(f"\nSTEP 3: get your alcohol wipe / incense ready, then press Enter "
          f"(it records for {TRIGGER_SECONDS} seconds)...")
    trig = read_values(ser, TRIGGER_SECONDS, "trigger")
    ser.close()
    if not trig:
        print("No readings during the trigger test.")
        return

    base_min, base_max = min(base), max(base)
    base_avg = sum(base) / len(base)
    peak = max(trig)
    span = peak - base_max

    print("\n------------------------------------------")
    print(f"Clean air : min={base_min}  avg={base_avg:.0f}  max={base_max}")
    print(f"Trigger   : peak={peak}")
    print("------------------------------------------")

    if span < 30:
        print("The reading barely moved. Check wiring (AO to A0), warm-up time,")
        print("and hold the trigger closer. Do not set thresholds from this run.")
        return

    safe_limit = int(base_max + 0.15 * span)
    warning_limit = int(base_max + 0.55 * span)

    print("SUGGESTED values for air_quality_alert.py:")
    print(f"  MQ135_SAFE_LIMIT    = {safe_limit}")
    print(f"  MQ135_WARNING_LIMIT = {warning_limit}")
    print("\n(Below SAFE = SAFE, between = WARNING, at/above WARNING_LIMIT = HARMFUL.)")
    print("Write the baseline, peak and these numbers in your report.")


if __name__ == "__main__":
    main()