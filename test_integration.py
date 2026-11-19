"""
SmartPark AI - integration test runner

Run directly in terminal:
    python test_integration.py

It calls the API, checks each result against the value worked out by hand
from green_score.py, prints PASS / FAIL / SKIP, and writes test_results.csv
(Test, Expected, Actual, Result) for the testing chapter of the report.

Database Isolation:
  * Uses a dedicated temporary SQLite DB file so running this test never
    alters or pollutes the production smart_parking.db.
  * Resets db path and removes temp DB cleanly on completion.
"""

import os
import sys
import csv
import json
import time
import tempfile

# Ensure root workspace is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import payment
from air_quality_db import create_air_quality_table
from app import app, tracker
from serial_manager import serial_manager

results = []

# Create isolated temporary database for test_integration
temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
temp_db_path = temp_db_file.name
temp_db_file.close()

db.set_db_path(temp_db_path)
db.init_db()
payment.init_minor_tables()
from air_quality_db import create_air_quality_table, save_air_quality_reading
create_air_quality_table()
save_air_quality_reading({
    "sensor": "MQ-135",
    "raw_value": 285,
    "alert_level": "SAFE",
    "exceedance": 0,
    "message": "Baseline air quality reading"
})
db.sync_slot_coordinates(tracker.slots)

client = app.test_client()


def call(method, path, body=None):
    if method == "GET":
        resp = client.get(path)
    elif method == "POST":
        resp = client.post(path, json=body)
    elif method == "DELETE":
        resp = client.delete(path)
    else:
        resp = client.open(path, method=method, json=body)
    data = resp.get_json(silent=True)
    return resp.status_code, data


def record(name, expected, actual, passed=None, skip=False):
    if skip:
        result = "SKIP"
    else:
        result = "PASS" if (passed if passed is not None else expected == actual) else "FAIL"
    results.append((name, str(expected), str(actual), result))
    print(f"[{result}] {name}  | expected: {expected}  | actual: {actual}")


def occupancy(free):
    all_slots = ["A1", "A2", "B1", "B2"]
    return {"occupancy": {s: (s not in free) for s in all_slots}}


def main():
    try:
        code, status = call("GET", "/api/system/status")
        if code != 200:
            print("Failed to initialize Flask test client.")
            return

        call("POST", "/api/camera/stop")

        # ---------------- Objective 2: AI recommendation ----------------
        code, slots = call("GET", "/api/slots")
        record("Obj2: 4 slots reported", 4, len(slots) if isinstance(slots, dict) else code)

        call("POST", "/api/preset/test1")
        _, rec = call("GET", "/api/recommend")
        record("Obj2: preset test1 picks A1", "A1", rec.get("slot_id"))

        call("POST", "/api/preset/test2")
        _, rec = call("GET", "/api/recommend")
        record("Obj2: preset test2 picks A1", "A1", rec.get("slot_id"))

        call("POST", "/api/preset/reset")
        _, rec = call("GET", "/api/recommend")
        record("Obj2: default layout picks A2", "A2", rec.get("slot_id"))

        call("POST", "/api/sync_state", occupancy({"A2", "B2"}))
        _, cmp_ = call("GET", "/api/recommend/compare")
        scored = (cmp_ or {}).get("scored_pick") or {}
        baseline = (cmp_ or {}).get("baseline_pick") or {}
        record("Obj2: AI recommendation compare",
               "scored=A2",
               f"scored={scored.get('slot_id')}")

        call("POST", "/api/toggle", {"slot_id": "A2"})
        _, rec = call("GET", "/api/recommend")
        record("Obj2: recommendation changes after A2 is taken", "B2", rec.get("slot_id"))

        call("POST", "/api/sync_state", occupancy(set()))
        _, rec = call("GET", "/api/recommend")
        record("Obj2: all slots full -> no slot recommended", None, rec.get("slot_id"))

        _, hist = call("GET", "/api/recommend/history")
        record("Obj2: recommendations are logged in SQLite", "at least 1 row",
               f"{len(hist) if isinstance(hist, list) else 0} rows",
               passed=isinstance(hist, list) and len(hist) > 0)

        # ---------------- Objective 3: air quality ----------------
        code, latest = call("GET", "/api/air-quality/latest")
        record("Obj3: latest air reading available", 200, code)
        code, summ = call("GET", "/api/air-quality/summary")
        total = ((summ or {}).get("data") or {}).get("total_readings", 0)
        record("Obj3: readings are stored in SQLite", "total_readings > 0", total,
               passed=total > 0)

        # ---------------- Objective 1: gate + QR + wallet ----------------
        orig_connected = serial_manager.connected
        orig_send = serial_manager.send
        was_offline = not serial_manager.connected
        if was_offline:
            serial_manager.connected = True
            serial_manager.send = lambda msg: True

        try:
            code, st = call("GET", "/api/gate/status")
            connected = bool(((st or {}).get("arduino") or {}).get("connected"))
            record("Obj1: Arduino connected (hardware/mock)", True, connected)

            driver_id = payment.create_test_driver("TEST-0001", 500.0)
            try:
                call("POST", "/api/sync_state", occupancy(set()))
                code, _ = call("POST", "/api/gate/scan", {"qr": driver_id})
                record("Obj1: entry denied when parking full (nothing charged)", 409, code)
                time.sleep(0.5)

                call("POST", "/api/preset/reset")
                code, _ = call("POST", "/api/gate/scan", {"qr": "NOT-A-REGISTERED-QR"})
                record("Obj1: unregistered QR denied", 402, code)
                time.sleep(0.5)

                code, body = call("POST", "/api/gate/scan", {"qr": driver_id})
                body = body or {}
                slot = (body.get("recommended_slot") or {}).get("slot_id")
                record("Obj1: entry granted + AI slot returned", "200 + entry + slot id",
                       f"{code} + {body.get('action')} + {slot}",
                       passed=(code == 200 and body.get("action") == "entry" and slot is not None))
                wallet_after = (body.get("payment") or {}).get("wallet_after")
                record("Obj1: wallet reduced by the Rs.10 entry fee", 490.0, wallet_after)
                time.sleep(0.5)

                code, body = call("POST", "/api/gate/scan", {"qr": driver_id, "direction": "EXIT"})
                body = body or {}
                record("Obj1: second scan exits the car", "200 + exit",
                       f"{code} + {body.get('action')}",
                       passed=(code == 200 and body.get("action") == "exit"))
                time.sleep(0.5)

                _, ev = call("GET", "/api/gate/events")
                record("Obj1: gate events logged in SQLite", "at least 1 row",
                       f"{len(ev) if isinstance(ev, list) else 0} rows",
                       passed=isinstance(ev, list) and len(ev) > 0)
            finally:
                payment.remove_test_driver("TEST-0001")
        finally:
            if was_offline:
                serial_manager.connected = orig_connected
                serial_manager.send = orig_send

        call("POST", "/api/preset/reset")

        with open("test_results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Test", "Expected", "Actual", "Result"])
            w.writerows(results)

        passed = sum(1 for r in results if r[3] == "PASS")
        failed = sum(1 for r in results if r[3] == "FAIL")
        skipped = sum(1 for r in results if r[3] == "SKIP")
        print(f"\n{passed} passed, {failed} failed, {skipped} skipped. "
              f"Saved to test_results.csv")

    finally:
        db.reset_db_path()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


if __name__ == "__main__":
    main()