"""
===============================================================================
SMART GREEN PARKING SYSTEM - GATE ROUTES (minor project <-> major project)
===============================================================================
Connects the minor's flow (QR, wallet, gate) to the major's modules.

Entry order (matches the project objective: charge ONLY if a slot is free):
    1. Arduino online?        no  -> 503, nothing charged
    2. Any vacant slot?       no  -> 409, LCD "Parking Full", nothing charged
    3. AI picks the best slot (green_score) - nothing is saved yet
    4. payment.start_entry    unregistered / already inside / low wallet
                              -> 402, LCD "Access Denied", nothing charged
    5. ACCESS_GRANTED:<fee>   gate opens, then LCD shows "Go to Slot X"

Exit: payment.process_exit works out the fee (entry fee is credited), then
EXIT_OK opens the gate.

/api/gate/scan decides entry or exit by itself: a driver with an active
session is leaving, anyone else is arriving (same rule as the minor).
===============================================================================
"""

import uuid
import time
from datetime import datetime
from flask import Blueprint, jsonify, request

import db
import payment
from serial_manager import serial_manager

DOUBLE_SCAN_GUARD_SECONDS = 5.0  # Configurable cooldown between entry and exit to prevent accidental double-scans

gate_bp = Blueprint("gate", __name__)

_tracker = None
_get_recommended_slot = None


class GateStepper:
    """Thread-safe state tracker for the 6-step gate transaction stepper:
       1. Vehicle at gate
       2. QR scanned
       3. Driver verified
       4. Fee deducted
       5. Slot recommended
       6. Gate open
    """
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.direction = "ENTRY"
        self.is_demo = False
        self.reset()

    def reset(self, direction=None, is_demo=False):
        with self.lock:
            if direction:
                self.direction = direction.upper()
            self.action = self.direction.lower()
            self.is_demo = is_demo
            self.status = "idle"  # "idle", "in_progress", "success", "failed"
            self.error_reason = None
            self.failed_step = None
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            gate_label = "entry" if self.direction == "ENTRY" else "exit"
            step4_desc = "Entry fee check (Rs. 10.00)" if self.direction == "ENTRY" else "Exit fee calculation"
            step5_desc = "AI green score allocation" if self.direction == "ENTRY" else "Slot release & vacancy"
            self.steps = [
                {"id": 1, "title": "Vehicle at gate", "status": "idle", "detail": f"Waiting for vehicle at {gate_label} gate"},
                {"id": 2, "title": "QR scanned", "status": "idle", "detail": "Awaiting QR scan"},
                {"id": 3, "title": "Driver verified", "status": "idle", "detail": "Awaiting driver validation"},
                {"id": 4, "title": "Fee deducted", "status": "idle", "detail": step4_desc},
                {"id": 5, "title": "Slot recommended", "status": "idle", "detail": step5_desc},
                {"id": 6, "title": "Gate open", "status": "idle", "detail": "Servo barrier gate actuation"}
            ]

    def on_vehicle_at_gate(self, source="IR_ENTRY", direction=None):
        with self.lock:
            if direction:
                self.direction = direction.upper()
            elif "EXIT" in str(source).upper():
                self.direction = "EXIT"
            else:
                self.direction = "ENTRY"
            self.action = self.direction.lower()
            self.status = "in_progress"
            self.error_reason = None
            self.failed_step = None
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            self.steps[0]["status"] = "completed"
            self.steps[0]["detail"] = f"Detected at {self.direction} gate ({source}) at {self.timestamp}"
            self.steps[1]["status"] = "active"
            self.steps[1]["detail"] = f"Present QR code to camera for {self.direction}"
            for i in range(2, 6):
                self.steps[i]["status"] = "idle"

    def on_qr_scanned(self, qr_text):
        with self.lock:
            self.status = "in_progress"
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            self.steps[0]["status"] = "completed"
            if not self.steps[0]["detail"] or self.steps[0]["detail"].startswith("Waiting"):
                self.steps[0]["detail"] = f"Vehicle at {self.direction} gate ({self.timestamp})"
            self.steps[1]["status"] = "completed"
            short_qr = qr_text[:8] if len(qr_text) > 8 else qr_text
            self.steps[1]["detail"] = f"QR: {short_qr} [{self.direction}]"
            self.steps[2]["status"] = "active"
            d_display = get_driver_display(qr_text)
            self.steps[2]["detail"] = f"Driver: {d_display}"

    def on_step_failed(self, step_idx, reason):
        with self.lock:
            self.status = "failed"
            self.failed_step = step_idx
            self.error_reason = reason
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            for i in range(step_idx - 1):
                if self.steps[i]["status"] != "completed":
                    self.steps[i]["status"] = "completed"
            self.steps[step_idx - 1]["status"] = "failed"
            self.steps[step_idx - 1]["detail"] = reason
            for i in range(step_idx, 6):
                self.steps[i]["status"] = "idle"

    def on_entry_success(self, driver_name, vehicle_no, fee, slot_id, is_demo=False):
        with self.lock:
            self.direction = "ENTRY"
            self.action = "entry"
            self.is_demo = is_demo
            self.status = "success"
            self.failed_step = None
            self.error_reason = None
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            self.steps[0]["status"] = "completed"
            self.steps[1]["status"] = "completed"
            self.steps[2]["status"] = "completed"
            self.steps[2]["detail"] = f"{driver_name} ({vehicle_no})"
            self.steps[3]["status"] = "completed"
            self.steps[3]["detail"] = f"Rs. {fee:.2f} deducted"
            self.steps[4]["status"] = "completed"
            self.steps[4]["detail"] = f"Slot {slot_id} assigned (AI Best)"
            self.steps[5]["status"] = "completed"
            gate_msg = "Gate open (DEMO - no hardware)" if is_demo else "Gate open (3s) - Access Granted"
            self.steps[5]["detail"] = gate_msg

    def on_exit_success(self, driver_name, vehicle_no, fee, is_demo=False):
        with self.lock:
            self.direction = "EXIT"
            self.action = "exit"
            self.is_demo = is_demo
            self.status = "success"
            self.failed_step = None
            self.error_reason = None
            self.timestamp = datetime.now().strftime("%H:%M:%S")
            self.steps[0]["status"] = "completed"
            self.steps[0]["detail"] = "Vehicle at exit gate"
            self.steps[1]["status"] = "completed"
            self.steps[2]["status"] = "completed"
            self.steps[2]["detail"] = f"{driver_name} ({vehicle_no})"
            self.steps[3]["status"] = "completed"
            self.steps[3]["detail"] = f"Exit fee paid: Rs. {fee:.2f}"
            self.steps[4]["status"] = "completed"
            self.steps[4]["detail"] = "Slot released & marked free"
            self.steps[5]["status"] = "completed"
            gate_msg = "Exit gate open (DEMO - no hardware)" if is_demo else "Exit gate opened (3s)"
            self.steps[5]["detail"] = gate_msg

    def on_timeout(self):
        with self.lock:
            if self.status == "in_progress" and self.steps[1]["status"] == "active":
                self.status = "idle"
                self.steps[1]["status"] = "idle"
                self.steps[1]["detail"] = "Scan timed out (20s)"
                self.steps[0]["detail"] = "Vehicle cleared"

    def get_title(self):
        demo_str = " - DEMO - no hardware" if self.is_demo else ""
        return f"Gate Transaction Stepper [{self.direction}]{demo_str}"

    def get_state(self):
        with self.lock:
            return {
                "status": self.status,
                "failed_step": self.failed_step,
                "error_reason": self.error_reason,
                "action": self.action,
                "direction": self.direction,
                "title": self.get_title(),
                "is_demo": self.is_demo,
                "timestamp": self.timestamp,
                "steps": [dict(s) for s in self.steps]
            }


stepper = GateStepper()
_current_gate_direction = "ENTRY"
_auto_gate_mode_from_ir = False  # DEFAULT OFF
_last_manual_direction_time = 0.0
_last_ir_switch_time = 0.0


def set_auto_gate_mode(enabled: bool) -> bool:
    global _auto_gate_mode_from_ir
    _auto_gate_mode_from_ir = bool(enabled)
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] [CONFIG] Auto gate mode from IR set to: {'ON' if _auto_gate_mode_from_ir else 'OFF'}")
    return _auto_gate_mode_from_ir


def get_auto_gate_mode() -> bool:
    return _auto_gate_mode_from_ir


def set_gate_direction(direction: str, manual: bool = False):
    """Sets explicit gate direction: ENTRY or EXIT.
    When manual=True, marks manual timestamp so IR events are ignored for 30s."""
    global _current_gate_direction, _last_manual_direction_time
    d = str(direction or "ENTRY").upper().strip()
    if d in ("ENTRY", "EXIT"):
        _current_gate_direction = d
        stepper.direction = d
        stepper.action = d.lower()
        if manual:
            _last_manual_direction_time = time.time()
        if _tracker:
            _tracker.set_camera_mode("QR_SCAN", reason=f"Gate direction set to {d}")
            _tracker.qr_banner = f"Vehicle at {d.lower()} gate - show your QR code"
    return _current_gate_direction


def get_gate_direction() -> str:
    return _current_gate_direction


def handle_ir_event(source: str, from_serial: bool = False) -> tuple:
    """
    Handles IR_ENTRY or IR_EXIT events from Arduino serial or simulation.
    Rules:
    1. DEFAULT OFF: When OFF, log in activity feed and terminal with timestamp,
       must NOT change camera mode or gate direction. Direction set only by buttons.
    2. When ON: apply 10s cooldown and ignore IR events while a manual direction
       was set in the last 30s.
    """
    global _last_ir_switch_time
    now = time.time()
    stamp = datetime.now().strftime("%H:%M:%S")
    source_clean = str(source).upper().strip()
    target_dir = "EXIT" if "EXIT" in source_clean else "ENTRY"

    # If event didn't come from serial_manager (e.g. simulated), log to DB
    if not from_serial:
        db.log_gate_event(source_clean, note=f"Simulated IR event ({source_clean})")

    # Rule 1: Auto gate mode toggle (DEFAULT OFF)
    if not _auto_gate_mode_from_ir:
        print(f"[{stamp}] [IR LOGGED] {source_clean} received | Auto gate mode from IR is OFF -> Mode & Direction unchanged ({_current_gate_direction}).")
        return False, "Auto gate mode from IR is OFF"

    # Rule 2: Manual direction was set in the last 30 seconds
    time_since_manual = now - _last_manual_direction_time
    if time_since_manual < 30.0:
        remaining = round(30.0 - time_since_manual, 1)
        print(f"[{stamp}] [IR IGNORED] {source_clean} ignored | Manual direction set {round(time_since_manual, 1)}s ago (< 30s guard, {remaining}s remaining).")
        return False, f"Manual direction set recently ({remaining}s remaining)"

    # Rule 3: 10 second cooldown on IR-triggered direction switches
    time_since_last_ir = now - _last_ir_switch_time
    if time_since_last_ir < 10.0:
        remaining = round(10.0 - time_since_last_ir, 1)
        print(f"[{stamp}] [IR COOLDOWN] {source_clean} ignored | 10s cooldown active ({remaining}s remaining).")
        return False, f"IR cooldown active ({remaining}s remaining)"

    # All conditions met: perform auto-switch
    _last_ir_switch_time = now
    set_gate_direction(target_dir, manual=False)
    stepper.on_vehicle_at_gate(source_clean, direction=target_dir)
    if _tracker:
        _tracker.set_camera_mode("QR_SCAN", reason=f"Vehicle at {target_dir.lower()} gate ({source_clean})")
        _tracker.qr_banner = f"Vehicle at {target_dir.lower()} gate - show your QR code"
    print(f"[{stamp}] [IR AUTO-SWITCH] {source_clean} accepted | Switched camera to QR_SCAN and gate direction to {target_dir}.")
    return True, f"Auto-switched to {target_dir}"


def init_gate_routes(tracker, get_recommended_slot):
    """Called once from app.py so the routes can see the live slot feed."""
    global _tracker, _get_recommended_slot
    _tracker = tracker
    _get_recommended_slot = get_recommended_slot


_latest_scan_result = {
    "qr": None,
    "timestamp": None,
    "time": None,
    "action": None,
    "status": "AWAITING_SCAN",
    "message": "Awaiting scan",
    "driver": None,
    "vehicle": None,
    "slot_id": None,
    "fee": None,
    "wallet_before": None,
    "wallet_after": None,
    "duration_minutes": None,
    "payment": {},
    "recommended_slot": {},
    "success": None
}


def get_driver_display(qr: str) -> str:
    """Returns 'NAME (vehicle_no)' for registered drivers, or 'Unregistered QR (abc12345)' if not registered."""
    if not qr:
        return "Unknown Driver"
    try:
        driver = payment.find_driver(qr)
        if driver:
            v_no = driver.get("vehicle_no") or driver.get("vehicle") or ""
            return f"{driver['name']} ({v_no})" if v_no else driver['name']
    except Exception:
        pass
    short_qr = qr[:8] if len(qr) >= 8 else qr
    return f"Unregistered QR ({short_qr})"


def record_scan_result(qr, action, success, message, payment_info=None, rec=None):
    now_dt = datetime.now()
    _latest_scan_result["qr"] = qr
    _latest_scan_result["timestamp"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    _latest_scan_result["time"] = now_dt.strftime("%H:%M:%S")
    _latest_scan_result["action"] = action.upper() if action else "ENTRY"
    _latest_scan_result["success"] = success
    _latest_scan_result["status"] = "ACCEPTED" if success else "REJECTED"
    _latest_scan_result["message"] = message
    pay = payment_info or {}
    
    # Always look up driver display on every scan (even rejected ones)
    d_display = pay.get("driver_display") or get_driver_display(qr)
    _latest_scan_result["driver_display"] = d_display
    _latest_scan_result["driver"] = d_display
    _latest_scan_result["vehicle"] = pay.get("vehicle")
    _latest_scan_result["slot_id"] = pay.get("slot_id") or (rec.get("slot_id") if rec else None)
    _latest_scan_result["fee"] = pay.get("fee") or pay.get("charged_now") or pay.get("total_paid")
    _latest_scan_result["wallet_before"] = pay.get("wallet_before")
    _latest_scan_result["wallet_after"] = pay.get("wallet_after")
    _latest_scan_result["duration_minutes"] = pay.get("duration_minutes")
    _latest_scan_result["payment"] = pay
    _latest_scan_result["recommended_slot"] = rec or {}


def _vacant_count(feed: dict) -> int:
    return sum(1 for s in feed.values() if s.get("vacant", False))


def _fail(message, status, error_code=None, qr=None, **extra):
    if qr:
        record_scan_result(qr, extra.get("action", "scan"), False, message, payment_info={"driver_display": get_driver_display(qr)})
    payload = {"success": False, "message": message, "error_reason": message, **extra}
    if error_code:
        payload["error_code"] = error_code
    if qr:
        payload["qr"] = qr
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# core flows
# ---------------------------------------------------------------------------
def _do_entry(qr, fee=None):
    demo_active = False
    try:
        from app import _demo_mode
        demo_active = bool(_demo_mode)
    except Exception:
        pass

    stepper.reset(direction="ENTRY", is_demo=demo_active)
    stepper.on_qr_scanned(qr)
    fee = payment.ENTRY_FEE if fee is None else fee
    tx_id = f"TX-{uuid.uuid4().hex[:8].upper()}"

    # Always look up driver immediately for display across all steps
    driver = payment.find_driver(qr)
    driver_display = get_driver_display(qr)

    print(f"\n=======================================================")
    print(f"[ENTRY PIPELINE] Processing scan for QR: '{qr}'")
    print(f"  Step 1 [QR Decoded]          -> SUCCESS: {qr}")

    # 1) Direction rule for ENTRY: Driver lookup & already inside check
    if driver is None:
        print(f"  Step 2 [Driver Found]        -> FAILED: Driver not registered ({driver_display})")
        print(f"  Step 3 [Slot Available?]     -> SKIPPED (unregistered driver)")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"  Step 5 [Serial Command Sent] -> SKIPPED")
        print(f"  Step 6 [Arduino Reply]       -> None")
        print(f"=======================================================\n")
        stepper.on_step_failed(3, f"Driver not registered: {driver_display}")
        payment.log_scan(qr, "DENIED", f"Driver not registered: {driver_display}")
        return _fail(f"Driver not registered: {driver_display}", 402, error_code="INVALID_QR", qr=qr)

    driver_id = driver["driver_id"]
    active_sess = payment._active_session(driver_id)
    if active_sess:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT slotid FROM parkingrecords WHERE driver_id = ? AND status = 'PARKED' ORDER BY id DESC LIMIT 1", (driver_id,))
        rec = cur.fetchone()
        conn.close()
        slot_name = rec["slotid"] if rec and rec["slotid"] else "assigned slot"
        msg = f"{driver_display} already parked in slot {slot_name}"
        print(f"  Step 2 [Driver Found]        -> FAILED: {msg}")
        print(f"  Step 3 [Slot Available?]     -> SKIPPED")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"  Step 5 [Serial Command Sent] -> SKIPPED")
        print(f"  Step 6 [Arduino Reply]       -> None")
        print(f"=======================================================\n")
        stepper.on_step_failed(3, msg)
        payment.log_scan(qr, "DENIED", msg)
        db.log_gate_event("ENTRY_DENIED_ALREADY_PARKED", qr, slot_name, msg)
        return _fail(msg, 409, error_code="ALREADY_PARKED", qr=qr, slot_id=slot_name)

    print(f"  Step 2 [Driver Found]        -> SUCCESS: {driver_display}, Wallet: Rs {driver['wallet']:.2f}")
    stepper.steps[2]["status"] = "completed"
    stepper.steps[2]["detail"] = driver_display

    # 2) Honest gate check: Gate controller must be online BEFORE any money moves (unless in Demo Mode)
    if not serial_manager.connected and not demo_active:
        print(f"  Step 3 [Slot Available?]     -> SKIPPED (Arduino offline)")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"  Step 5 [Serial Command Sent] -> FAILED: Arduino offline on COM port")
        print(f"  Step 6 [Arduino Reply]       -> None")
        print(f"=======================================================\n")
        stepper.on_step_failed(6, "Gate command not sent: Arduino offline")
        db.log_gate_event("ENTRY_DENIED_GATE_OFFLINE", qr, None, "Arduino offline")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="GATE_OFFLINE")
        return _fail("Gate command not sent: Arduino offline. Nothing charged.", 503, error_code="GATE_OFFLINE", qr=qr)

    # 3) Camera health & freshness check (unless in Demo Mode)
    if _tracker and not _tracker.is_camera_healthy() and not demo_active:
        unhealthy_why = _tracker.get_unhealthy_reason()
        if serial_manager.connected:
            serial_manager.send("DENIED")
        print(f"  Step 3 [Slot Available?]     -> FAILED: Camera offline or detection stale ({unhealthy_why})")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"  Step 5 [Serial Command Sent] -> SENT: DENIED")
        print(f"  Step 6 [Arduino Reply]       -> ACCESS_DENIED")
        print(f"=======================================================\n")
        stepper.on_step_failed(1, f"Camera offline / detection stale ({unhealthy_why})")
        db.log_gate_event("ENTRY_DENIED_CAMERA_OFFLINE", qr, None, f"Camera offline ({unhealthy_why})")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="CAMERA_OFFLINE")
        return _fail(f"Camera offline or detection stale ({unhealthy_why}). Entry prohibited. Nothing charged.", 503, error_code="CAMERA_OFFLINE", qr=qr)

    # 4) Availability check
    feed = _tracker.get_live_slot_feed(demo_mode=demo_active) if _tracker else {}
    vacant_cnt = _vacant_count(feed)
    if vacant_cnt == 0:
        if serial_manager.connected:
            serial_manager.send("FULL")
        print(f"  Step 3 [Slot Available?]     -> FAILED: PARKING_FULL (0/4 slots vacant)")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"  Step 5 [Serial Command Sent] -> SENT: FULL")
        print(f"  Step 6 [Arduino Reply]       -> PARKING_FULL")
        print(f"=======================================================\n")
        stepper.on_step_failed(5, "Parking full (0 slots vacant)")
        payment.log_scan(qr, "DENIED", "Parking full")
        db.log_gate_event("ENTRY_DENIED_FULL", qr, None, "No vacant slots, payment not attempted")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="PARKING_FULL")
        return _fail("Parking full (0 slots vacant). Nothing charged.", 409, error_code="PARKING_FULL", qr=qr)

    # 5) AI picks the best slot
    rec = _get_recommended_slot(feed) if _get_recommended_slot else None
    if not rec or not rec.get("slot_id"):
        print(f"  Step 3 [Slot Available?]     -> FAILED: NO_SLOT (scoring could not allocate candidate)")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"=======================================================\n")
        stepper.on_step_failed(5, "No recommended slot available")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="NO_SLOT")
        return _fail("No recommended slot available. Nothing charged.", 409, error_code="NO_SLOT", qr=qr)

    slot_id = rec["slot_id"]

    # 6) Temporary Slot Reservation
    reserved = _tracker.reserve_slot(slot_id, tx_id, qr, duration=60.0, demo_mode=demo_active) if _tracker else False
    if not reserved:
        print(f"  Step 3 [Slot Available?]     -> FAILED: Slot {slot_id} contention / could not be reserved")
        print(f"  Step 4 [Fee Deducted]        -> SKIPPED (Rs 0 charged)")
        print(f"=======================================================\n")
        stepper.on_step_failed(5, f"Slot {slot_id} could not be reserved")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, slot_id=slot_id, error_reason="NO_SLOT")
        return _fail(f"Slot {slot_id} is no longer available. Nothing charged.", 409, error_code="NO_SLOT", qr=qr)

    db.log_slot_reservation(slot_id, tx_id, qr, duration=60.0)
    print(f"  Step 3 [Slot Available?]     -> SUCCESS: Slot {slot_id} allocated (Vacant slots: {vacant_cnt}/4, Score: {rec.get('score')})")

    # 7) QR + wallet + session payment execution
    ok, message, info = payment.start_entry(qr, slot_id, fee)
    if not ok:
        if _tracker:
            _tracker.release_reservation(slot_id=slot_id, tx_id=tx_id)
        db.update_slot_reservation_status(tx_id, "CANCELLED")
        if serial_manager.connected:
            serial_manager.send("DENIED")
        db.log_gate_event("ENTRY_DENIED_PAYMENT", qr, slot_id, message)

        msg_lower = message.lower()
        if "not registered" in msg_lower or "invalid" in msg_lower:
            err_code = "INVALID_QR"
            stepper.on_step_failed(3, f"Driver not registered ({message})")
        else:
            err_code = "PAYMENT_FAILED"
            stepper.on_step_failed(4, f"Payment failed ({message})")

        print(f"  Step 4 [Fee Deducted]        -> FAILED: {message}")
        print(f"  Step 5 [Serial Command Sent] -> SENT: DENIED")
        print(f"  Step 6 [Arduino Reply]       -> ACCESS_DENIED")
        print(f"=======================================================\n")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, slot_id=slot_id, error_reason=err_code)
        return _fail(message, 402, error_code=err_code, qr=qr)

    w_before = info.get("wallet_before", driver["wallet"])
    w_after = info.get("wallet_after", w_before - fee)
    print(f"  Step 4 [Fee Deducted]        -> SUCCESS: Rs {fee:.2f} deducted (Wallet: Rs {w_before:.2f} -> Rs {w_after:.2f})")

    # 8) Send serial gate open command and honestly verify
    gate_opened = False
    cmd_main = f"ACCESS_GRANTED:{fee:g}"
    cmd_goto = f"GOTO:{slot_id}"
    if serial_manager.connected:
        sent1 = serial_manager.send(cmd_main)
        sent2 = serial_manager.send(cmd_goto)
        print(f"  Step 5 [Serial Command Sent] -> SENT: {cmd_main} and {cmd_goto}")
        time.sleep(0.3)
        actual_replies = [e["line"] for e in list(serial_manager.recent_lines)[-3:] if e.get("line")]
        if actual_replies:
            print(f"  Step 6 [Arduino Reply]       -> RECEIVED: {', '.join(actual_replies)}")
        else:
            print(f"  Step 6 [Arduino Reply]       -> (No serial reply on {serial_manager.port} within timeout)")
    elif demo_active:
        gate_opened = True
        print(f"  Step 5 [Serial Command Sent] -> DEMO MODE: Simulated {cmd_main} and {cmd_goto}")
        print(f"  Step 6 [Arduino Reply]       -> DEMO MODE: Simulated SERVO_OPEN, ENTRY_GATE_OPENED")
    else:
        print(f"  Step 5 [Serial Command Sent] -> FAILED: Arduino offline")
        print(f"  Step 6 [Arduino Reply]       -> None")

    print(f"=======================================================\n")

    db.update_slot_reservation_status(tx_id, "COMPLETED")
    db.log_recommendation(rec)
    db.log_parking_transaction(tx_id, qr, "ENTRY", "SUCCESS", fee=fee, slot_id=slot_id)
    db.log_gate_event("ENTRY_GRANTED", qr, slot_id, message)
    record_scan_result(qr, "entry", True, message, info, rec)

    driver_name = info.get("driver") or "Driver"
    vehicle_no = info.get("vehicle") or "Vehicle"

    if gate_opened:
        stepper.on_entry_success(driver_name, vehicle_no, fee, slot_id, is_demo=demo_active)
    else:
        stepper.on_step_failed(6, "Gate command not sent: Arduino offline")

    return jsonify({
        "success": True,
        "action": "entry",
        "qr": qr,
        "transaction_id": tx_id,
        "message": message,
        "payment": info,
        "recommended_slot": rec,
        "gate_opened": gate_opened
    })


def _do_exit(qr):
    demo_active = False
    try:
        from app import _demo_mode
        demo_active = bool(_demo_mode)
    except Exception:
        pass

    stepper.reset(direction="EXIT", is_demo=demo_active)
    stepper.on_qr_scanned(qr)

    # 1) Direction rule for EXIT: Driver lookup & active entry check
    driver = payment.find_driver(qr)
    driver_display = get_driver_display(qr)
    if driver is None:
        stepper.on_step_failed(3, f"Driver not registered: {driver_display}")
        payment.log_scan(qr, "DENIED", f"Driver not registered: {driver_display}")
        return _fail(f"Driver not registered: {driver_display}", 402, error_code="INVALID_QR", qr=qr)

    driver_id = driver["driver_id"]
    active_sess = payment._active_session(driver_id)
    if not active_sess:
        msg = f"{driver_display}: No active entry"
        stepper.on_step_failed(3, msg)
        payment.log_scan(qr, "DENIED", msg)
        db.log_gate_event("EXIT_DENIED_NO_ENTRY", qr, None, msg)
        return _fail(msg, 402, error_code="NO_ACTIVE_SESSION", qr=qr)

    stepper.steps[2]["status"] = "completed"
    stepper.steps[2]["detail"] = driver_display

    # 2) Honest gate check: Gate controller must be online BEFORE any money moves (unless in Demo Mode)
    if not serial_manager.connected and not demo_active:
        stepper.on_step_failed(6, "Gate command not sent: Arduino offline")
        return _fail("Gate command not sent: Arduino offline. Nothing charged.", 503, error_code="GATE_OFFLINE", qr=qr)

    # 3) Process exit payment
    ok, message, info = payment.process_exit(qr)
    if not ok:
        if serial_manager.connected:
            serial_manager.send("DENIED")
        db.log_gate_event("EXIT_DENIED", qr, None, message)
        stepper.on_step_failed(4, f"Exit denied: {message}")
        return _fail(message, 402, qr=qr)

    # 4) Send serial gate open command and honestly verify
    gate_opened = False
    if serial_manager.connected:
        gate_opened = serial_manager.send("EXIT_OK")
    elif demo_active:
        gate_opened = True

    # Immediate slot release in tracker
    slot_id = info.get("slot_id")
    if slot_id and _tracker:
        _tracker.occupancy_status[slot_id] = False
        _tracker.release_reservation(slot_id=slot_id)

    exit_fee = info.get("charged_now", 0.0)
    tx_id = f"TX-{uuid.uuid4().hex[:8].upper()}"
    db.log_parking_transaction(tx_id, qr, "EXIT", "SUCCESS", fee=exit_fee, slot_id=slot_id)
    db.log_gate_event("EXIT_GRANTED", qr, slot_id, message)
    record_scan_result(qr, "exit", True, message, info, None)

    driver_name = info.get("driver") or "Driver"
    vehicle_no = info.get("vehicle") or "Vehicle"
    paid_total = info.get("total_paid") or info.get("charged_now") or 0.0

    if gate_opened:
        stepper.on_exit_success(driver_name, vehicle_no, paid_total, is_demo=demo_active)
    else:
        stepper.on_step_failed(6, "Gate command not sent: Arduino offline")

    return jsonify({
        "success": True,
        "action": "exit",
        "qr": qr,
        "transaction_id": tx_id,
        "message": message,
        "payment": info,
        "gate_opened": gate_opened
    })


def _qr_from_request():
    data = request.get_json(silent=True) or {}
    return data, str(data.get("qr") or "").strip()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@gate_bp.route("/api/gate/direction", methods=["GET", "POST"])
def api_gate_direction():
    """Gets or sets explicit gate direction (ENTRY vs EXIT)."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        new_dir = data.get("direction", "ENTRY")
        is_manual = bool(data.get("manual", True))
        set_gate_direction(new_dir, manual=is_manual)
    return jsonify({
        "direction": get_gate_direction(),
        "auto_gate_mode_from_ir": get_auto_gate_mode(),
        "stepper_title": stepper.get_title(),
        "is_demo": stepper.is_demo
    })


@gate_bp.route("/api/gate/auto_ir", methods=["GET", "POST"])
def api_gate_auto_ir():
    """Gets or sets the 'Auto gate mode from IR' toggle state."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("enabled", False))
        set_auto_gate_mode(enabled)
    return jsonify({
        "auto_gate_mode_from_ir": get_auto_gate_mode()
    })


@gate_bp.route("/api/gate/scan", methods=["POST"])
def gate_scan():
    """One QR scan: respects explicit gate direction (ENTRY mode or EXIT mode)."""
    data, qr = _qr_from_request()
    if not qr:
        return _fail("qr is required", 400)

    # Use explicit direction from request or current gate direction
    direction = str(data.get("direction") or get_gate_direction()).upper().strip()
    if direction == "EXIT":
        return _do_exit(qr)
    else:
        return _do_entry(qr)


@gate_bp.route("/api/gate/entry", methods=["POST"])
def gate_entry():
    data, qr = _qr_from_request()
    if not qr:
        return _fail("qr is required", 400)
    try:
        fee = float(data.get("amount", payment.ENTRY_FEE))
    except (TypeError, ValueError):
        return _fail("amount must be a number", 400)
    return _do_entry(qr, fee)


@gate_bp.route("/api/gate/exit", methods=["POST"])
def gate_exit():
    _, qr = _qr_from_request()
    if not qr:
        return _fail("qr is required", 400)
    return _do_exit(qr)


@gate_bp.route("/api/gate/manual/<action>", methods=["POST"])
def gate_manual(action):
    """Open/close the gate without any payment (hardware testing only)."""
    commands = {"open_entry": "OPEN_ENTRY",
                "open_exit": "OPEN_EXIT",
                "close": "CLOSE_GATE"}
    if action not in commands:
        return _fail("Unknown action", 400)
    if not serial_manager.connected:
        return _fail("Arduino offline", 503)
    serial_manager.send(commands[action])
    db.log_gate_event("MANUAL_" + action.upper(), None, None, "Manual test command")
    return jsonify({"success": True, "message": f"Sent {commands[action]}"})


@gate_bp.route("/api/gate/test_open_entry", methods=["POST"])
def gate_test_open_entry():
    """Admin diagnostic test endpoint: sends OPEN_ENTRY and returns exact command and Arduino reply."""
    import time
    demo_active = False
    try:
        from app import _demo_mode
        demo_active = bool(_demo_mode)
    except Exception:
        pass

    exact_cmd = "OPEN_ENTRY"
    if not serial_manager.connected and not demo_active:
        return jsonify({
            "success": False,
            "connected": False,
            "port": serial_manager.port or "None",
            "command": exact_cmd,
            "angle": 90,
            "duration": "Indefinite (stays open until CLOSE_GATE)",
            "message": "Arduino offline. Connect USB to COM port or enable Demo Mode.",
            "replies": []
        }), 503

    replies = []
    if serial_manager.connected:
        start_len = len(serial_manager.recent_lines)
        serial_manager.send(exact_cmd)
        t0 = time.time()
        while time.time() - t0 < 1.5:
            time.sleep(0.08)
            current_lines = serial_manager.recent_lines[start_len:]
            if any("ENTRY_GATE_OPENED" in l.get("line", "") for l in current_lines):
                break
        replies = [l.get("line") for l in serial_manager.recent_lines[start_len:]]
        if not replies:
            replies = ["(No reply received within 1.5s - check baud rate 9600)"]
    else:
        replies = [
            "Received: OPEN_ENTRY",
            "SERVO_OPEN",
            "ENTRY_GATE_OPENED"
        ]

    db.log_gate_event("TEST_OPEN_ENTRY", note=f"Command: {exact_cmd}, Replies: {', '.join(replies)}")
    return jsonify({
        "success": True,
        "connected": serial_manager.connected,
        "port": serial_manager.port or ("COM7 (Demo)" if demo_active else "None"),
        "command": exact_cmd,
        "angle": 90,
        "duration": "Indefinite (stays open until CLOSE_GATE command is sent)",
        "message": f"Command '{exact_cmd}' executed successfully.",
        "replies": replies
    })


@gate_bp.route("/api/gate/status")
def gate_status():
    demo_active = False
    try:
        from app import _demo_mode
        demo_active = bool(_demo_mode)
    except Exception:
        pass
    feed = _tracker.get_live_slot_feed(demo_mode=demo_active) if _tracker else {}
    return jsonify({
        "arduino": serial_manager.status(),
        "total_slots": len(feed),
        "vacant_slots": _vacant_count(feed),
        "last_scan": _latest_scan_result,
        "direction": get_gate_direction(),
        "auto_gate_mode_from_ir": get_auto_gate_mode(),
        "stepper": stepper.get_state(),
    })


@gate_bp.route("/api/gate/stepper")
def gate_stepper():
    return jsonify(stepper.get_state())


@gate_bp.route("/api/gate/trigger_ir", methods=["POST"])
def gate_trigger_ir():
    data = request.get_json(silent=True) or {}
    event = str(data.get("event", "IR_ENTRY")).upper()
    if event in ("IR_ENTRY", "IR_EXIT"):
        switched, reason = handle_ir_event(event, from_serial=False)
        return jsonify({
            "success": True,
            "event": event,
            "switched": switched,
            "reason": reason,
            "direction": get_gate_direction(),
            "stepper": stepper.get_state()
        })
    return jsonify({"success": False, "message": "Unknown event"}), 400


@gate_bp.route("/api/gate/last_scan")
def gate_last_scan():
    return jsonify(_latest_scan_result)


@gate_bp.route("/api/gate/sessions")
def gate_sessions():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_recent_sessions(limit))


@gate_bp.route("/api/gate/events")
def gate_events():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_gate_events(limit))


@gate_bp.route("/api/gate/transactions")
def gate_transactions():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_parking_transactions(limit))


# ---------------------------------------------------------------------------
# STEP-BY-STEP ENTRY API ENDPOINTS
# ---------------------------------------------------------------------------
@gate_bp.route("/api/entry/check", methods=["POST"])
def entry_check():
    """Step 1: Check camera health, QR validity, parking availability, candidate slot."""
    data, qr = _qr_from_request()
    if not qr:
        return _fail("qr is required", 400, error_code="INVALID_QR", qr=qr)

    user = payment.get_user(qr)
    if not user:
        return _fail("QR code is not registered", 402, error_code="INVALID_QR", qr=qr)

    if payment.has_active_session(qr):
        return jsonify({"success": True, "action": "exit", "qr": qr, "message": "User currently inside parking. Ready for exit."})

    demo_active = False
    try:
        from app import _demo_mode
        demo_active = bool(_demo_mode)
    except Exception:
        pass

    if not serial_manager.connected and not demo_active:
        return _fail("Gate controller (Arduino) is offline.", 503, error_code="GATE_OFFLINE", qr=qr)

    if _tracker and not _tracker.is_camera_healthy() and not demo_active:
        return _fail("Camera offline or detection stale.", 503, error_code="CAMERA_OFFLINE", qr=qr)

    feed = _tracker.get_live_slot_feed(demo_mode=demo_active) if _tracker else {}
    if _vacant_count(feed) == 0:
        return _fail("Parking full.", 409, error_code="PARKING_FULL", qr=qr)

    rec = _get_recommended_slot(feed) if _get_recommended_slot else None
    if not rec or not rec.get("slot_id"):
        return _fail("No recommended slot available.", 409, error_code="NO_SLOT", qr=qr)

    return jsonify({
        "success": True,
        "action": "entry_check",
        "qr": qr,
        "driver": user["driver"],
        "vehicle": user["vehicle"],
        "wallet_balance": user["wallet_balance"],
        "recommended_slot": rec,
        "fee": payment.ENTRY_FEE,
    })


@gate_bp.route("/api/entry/reserve", methods=["POST"])
def entry_reserve():
    """Step 2: Reserve candidate slot for a transaction."""
    data, qr = _qr_from_request()
    slot_id = data.get("slot_id")
    if not qr or not slot_id:
        return _fail("qr and slot_id are required", 400, error_code="INVALID_PARAM", qr=qr)

    tx_id = f"TX-{uuid.uuid4().hex[:8].upper()}"
    reserved = _tracker.reserve_slot(slot_id, tx_id, qr, duration=60.0) if _tracker else False
    if not reserved:
        return _fail(f"Slot {slot_id} could not be reserved.", 409, error_code="NO_SLOT", qr=qr)

    db.log_slot_reservation(slot_id, tx_id, qr, duration=60.0)
    return jsonify({
        "success": True,
        "qr": qr,
        "transaction_id": tx_id,
        "slot_id": slot_id,
        "reserved_until_seconds": 60,
    })


@gate_bp.route("/api/entry/pay", methods=["POST"])
def entry_pay():
    """Step 3: Execute payment for reserved transaction ID and open gate."""
    data, qr = _qr_from_request()
    tx_id = data.get("transaction_id")
    slot_id = data.get("slot_id")
    fee = float(data.get("amount", payment.ENTRY_FEE))

    if not qr or not tx_id or not slot_id:
        return _fail("qr, transaction_id, and slot_id are required", 400, error_code="INVALID_PARAM", qr=qr)

    ok, message, info = payment.start_entry(qr, slot_id, fee)
    if not ok:
        if _tracker:
            _tracker.release_reservation(slot_id=slot_id, tx_id=tx_id)
        db.update_slot_reservation_status(tx_id, "CANCELLED")
        serial_manager.send("DENIED")
        db.log_gate_event("ENTRY_DENIED_PAYMENT", qr, slot_id, message)
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, slot_id=slot_id, error_reason="PAYMENT_FAILED")
        return _fail(message, 402, error_code="PAYMENT_FAILED", qr=qr)

    db.update_slot_reservation_status(tx_id, "COMPLETED")
    db.log_parking_transaction(tx_id, qr, "ENTRY", "SUCCESS", fee=fee, slot_id=slot_id)
    serial_manager.send(f"ACCESS_GRANTED:{fee:g}")
    serial_manager.send(f"GOTO:{slot_id}")
    db.log_gate_event("ENTRY_GRANTED", qr, slot_id, message)

    return jsonify({
        "success": True,
        "action": "entry",
        "qr": qr,
        "transaction_id": tx_id,
        "message": message,
        "payment": info,
        "slot_id": slot_id,
    })


@gate_bp.route("/api/entry/cancel", methods=["POST"])
def entry_cancel():
    """Cancel a pending slot reservation."""
    data, qr = _qr_from_request()
    tx_id = data.get("transaction_id")
    slot_id = data.get("slot_id")

    if _tracker:
        _tracker.release_reservation(slot_id=slot_id, tx_id=tx_id)
    if tx_id:
        db.update_slot_reservation_status(tx_id, "CANCELLED")

    return jsonify({"success": True, "message": "Reservation cancelled cleanly."})