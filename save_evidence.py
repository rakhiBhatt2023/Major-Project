"""
===============================================================================
SMART GREEN PARKING SYSTEM - EVIDENCE DUMP UTILITY (MIDTERM REPORT GENERATOR)
===============================================================================
Dumps system health, database transactions, slot reservations, and gate events
into JSON and formatted text logs after each physical testing step.
===============================================================================
"""

import json
import os
import time
import requests
import db
from datetime import datetime

OUTPUT_DIR = "evidence_dumps"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def dump_evidence(step_name: str):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sanitized_name = step_name.lower().replace(" ", "_").replace("/", "_")
    filename = f"{OUTPUT_DIR}/evidence_{sanitized_name}_{timestamp}.json"
    
    # 1. Fetch camera health
    health_data = {}
    try:
        r = requests.get("http://127.0.0.1:5000/api/camera/health", timeout=3)
        if r.status_code == 200:
            health_data = r.json()
    except Exception as e:
        health_data = {"error": f"Could not reach /api/camera/health: {e}"}

    # 2. Fetch DB records
    conn = db.get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM parking_transactions ORDER BY id DESC LIMIT 20")
    transactions = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM slot_reservations ORDER BY id DESC LIMIT 20")
    reservations = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM gate_events ORDER BY id DESC LIMIT 20")
    gate_events = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM scanlogs ORDER BY id DESC LIMIT 20")
    scan_logs = [dict(r) for r in cur.fetchall()]

    conn.close()

    payload = {
        "step": step_name,
        "timestamp": datetime.now().isoformat(),
        "camera_health": health_data,
        "transactions_last_20": transactions,
        "reservations_last_20": reservations,
        "gate_events_last_20": gate_events,
        "scan_logs_last_20": scan_logs
    }

    with open(filename, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\n[EVIDENCE SAVED] Dumped state for '{step_name}' to {filename}")
    print(f"  Camera Healthy : {health_data.get('is_healthy', False)}")
    print(f"  Frame Age      : {health_data.get('last_frame_age_seconds', 'N/A')}s")
    print(f"  Recent TX count: {len(transactions)}")
    print(f"  Recent Events  : {len(gate_events)}")
    return filename

if __name__ == "__main__":
    import sys
    step = sys.argv[1] if len(sys.argv) > 1 else "manual_test_step"
    dump_evidence(step)
