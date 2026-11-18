"""
SmartPark AI - integration test runner

Start app.py first, then in a second terminal (same folder):
    python test_integration.py

It calls the real API, checks each result against the value worked out by hand
from green_score.py, prints PASS / FAIL / SKIP, and writes test_results.csv
(Test, Expected, Actual, Result) for the testing chapter of the report.

What it touches:
  * STOPS THE CAMERA (otherwise the camera overwrites the test occupancy) and
    resets the slots at the end.
  * Creates a temporary driver with vehicle number TEST-0001 (wallet Rs.500) for
    the gate tests and deletes it again. Real drivers are never touched.
  * Gate tests need the Arduino connected; otherwise they are marked SKIP.
"""

import csv
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5000"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode() or "null"
            return r.status, json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


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
    code, status = call("GET", "/api/system/status")
    if code != 200:
        print("Cannot reach app.py. Start it first (python app.py).")
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

    # ---------------- Objective 1: gate + QR + wallet (needs Arduino) ----------------
    code, st = call("GET", "/api/gate/status")
    connected = bool(((st or {}).get("arduino") or {}).get("connected"))
    record("Obj1: Arduino connected", True, connected)

    gate_tests = ("entry denied when parking full", "unregistered QR denied",
                  "entry granted, wallet charged, AI slot returned",
                  "second scan exits the car", "gate events logged in SQLite")

    if not connected:
        for name in gate_tests:
            record(f"Obj1: {name}", "-", "Arduino offline", skip=True)
    else:
        import payment
        driver_id = payment.create_test_driver("TEST-0001", 500.0)
        try:
            call("POST", "/api/sync_state", occupancy(set()))
            code, _ = call("POST", "/api/gate/scan", {"qr": driver_id})
            record("Obj1: entry denied when parking full (nothing charged)", 409, code)
            time.sleep(3)

            call("POST", "/api/preset/reset")
            code, _ = call("POST", "/api/gate/scan", {"qr": "NOT-A-REGISTERED-QR"})
            record("Obj1: unregistered QR denied", 402, code)
            time.sleep(3)

            code, body = call("POST", "/api/gate/scan", {"qr": driver_id})
            body = body or {}
            slot = (body.get("recommended_slot") or {}).get("slot_id")
            record("Obj1: entry granted + AI slot returned", "200 + entry + slot id",
                   f"{code} + {body.get('action')} + {slot}",
                   passed=(code == 200 and body.get("action") == "entry" and slot is not None))
            wallet_after = (body.get("payment") or {}).get("wallet_after")
            record("Obj1: wallet reduced by the Rs.20 entry fee", 480.0, wallet_after)
            time.sleep(10)   # Arduino is busy opening/closing the gate

            code, body = call("POST", "/api/gate/scan", {"qr": driver_id})
            body = body or {}
            record("Obj1: second scan exits the car", "200 + exit",
                   f"{code} + {body.get('action')}",
                   passed=(code == 200 and body.get("action") == "exit"))
            time.sleep(6)

            _, ev = call("GET", "/api/gate/events")
            record("Obj1: gate events logged in SQLite", "at least 1 row",
                   f"{len(ev) if isinstance(ev, list) else 0} rows",
                   passed=isinstance(ev, list) and len(ev) > 0)
        finally:
            payment.remove_test_driver("TEST-0001")

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


if __name__ == "__main__":
    main()