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
from flask import Blueprint, jsonify, request

import db
import payment
from serial_manager import serial_manager

gate_bp = Blueprint("gate", __name__)

_tracker = None
_get_recommended_slot = None


def init_gate_routes(tracker, get_recommended_slot):
    """Called once from app.py so the routes can see the live slot feed."""
    global _tracker, _get_recommended_slot
    _tracker = tracker
    _get_recommended_slot = get_recommended_slot


def _vacant_count(feed: dict) -> int:
    return sum(1 for s in feed.values() if s.get("vacant", False))


def _fail(message, status, error_code=None, qr=None, **extra):
    payload = {"success": False, "message": message, **extra}
    if error_code:
        payload["error_code"] = error_code
    if qr:
        payload["qr"] = qr
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# core flows
# ---------------------------------------------------------------------------
def _do_entry(qr, fee=None):
    fee = payment.ENTRY_FEE if fee is None else fee
    tx_id = f"TX-{uuid.uuid4().hex[:8].upper()}"

    # 1) Gate controller must be online BEFORE any money moves
    if not serial_manager.connected:
        db.log_gate_event("ENTRY_DENIED_GATE_OFFLINE", qr, None, "Arduino offline")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="GATE_OFFLINE")
        return _fail("Gate controller (Arduino) is offline. Nothing charged.", 503, error_code="GATE_OFFLINE", qr=qr)

    # 2) Camera health & freshness check
    if _tracker and not _tracker.is_camera_healthy():
        serial_manager.send("DENIED")
        db.log_gate_event("ENTRY_DENIED_CAMERA_OFFLINE", qr, None, "Camera offline / detection stale")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="CAMERA_OFFLINE")
        return _fail("Camera offline or detection stale. Entry prohibited to prevent double booking. Nothing charged.", 503, error_code="CAMERA_OFFLINE", qr=qr)

    # 3) Availability check
    feed = _tracker.get_live_slot_feed() if _tracker else {}
    if _vacant_count(feed) == 0:
        serial_manager.send("FULL")
        payment.log_scan(qr, "DENIED", "Parking full")
        db.log_gate_event("ENTRY_DENIED_FULL", qr, None, "No vacant slots, payment not attempted")
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="PARKING_FULL")
        return _fail("Parking full. Nothing charged.", 409, error_code="PARKING_FULL", qr=qr)

    # 4) AI picks the best slot
    rec = _get_recommended_slot(feed) if _get_recommended_slot else None
    if not rec or not rec.get("slot_id"):
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, error_reason="NO_SLOT")
        return _fail("No recommended slot available. Nothing charged.", 409, error_code="NO_SLOT", qr=qr)

    slot_id = rec["slot_id"]

    # 5) Temporary Slot Reservation
    reserved = _tracker.reserve_slot(slot_id, tx_id, qr, duration=60.0) if _tracker else False
    if not reserved:
        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, slot_id=slot_id, error_reason="NO_SLOT")
        return _fail(f"Slot {slot_id} is no longer available. Nothing charged.", 409, error_code="NO_SLOT", qr=qr)

    db.log_slot_reservation(slot_id, tx_id, qr, duration=60.0)

    # 6) QR + wallet + session payment execution
    ok, message, info = payment.start_entry(qr, slot_id, fee)
    if not ok:
        if _tracker:
            _tracker.release_reservation(slot_id=slot_id, tx_id=tx_id)
        db.update_slot_reservation_status(tx_id, "CANCELLED")
        serial_manager.send("DENIED")
        db.log_gate_event("ENTRY_DENIED_PAYMENT", qr, slot_id, message)

        msg_lower = message.lower()
        if "not registered" in msg_lower or "invalid" in msg_lower:
            err_code = "INVALID_QR"
        else:
            err_code = "PAYMENT_FAILED"

        db.log_parking_transaction(tx_id, qr, "ENTRY", "FAILED", fee=fee, slot_id=slot_id, error_reason=err_code)
        return _fail(message, 402, error_code=err_code, qr=qr)

    # 7) Record recommendation, finalize transaction, open gate
    db.update_slot_reservation_status(tx_id, "COMPLETED")
    db.log_recommendation(rec)
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
        "recommended_slot": rec,
    })


def _do_exit(qr):
    if not serial_manager.connected:
        return _fail("Gate controller (Arduino) is offline.", 503, qr=qr)

    ok, message, info = payment.process_exit(qr)
    if not ok:
        serial_manager.send("DENIED")
        db.log_gate_event("EXIT_DENIED", qr, None, message)
        return _fail(message, 402, qr=qr)

    serial_manager.send("EXIT_OK")
    db.log_gate_event("EXIT_GRANTED", qr, info.get("slot_id"), message)
    return jsonify({
        "success": True,
        "action": "exit",
        "qr": qr,
        "message": message,
        "payment": info
    })


def _qr_from_request():
    data = request.get_json(silent=True) or {}
    return data, str(data.get("qr") or "").strip()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@gate_bp.route("/api/gate/scan", methods=["POST"])
def gate_scan():
    """One QR scan: exit if the driver is inside, entry otherwise."""
    _, qr = _qr_from_request()
    if not qr:
        return _fail("qr is required", 400)
    if payment.has_active_session(qr):
        return _do_exit(qr)
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


@gate_bp.route("/api/gate/status")
def gate_status():
    feed = _tracker.get_live_slot_feed()
    return jsonify({
        "arduino": serial_manager.status(),
        "total_slots": len(feed),
        "vacant_slots": _vacant_count(feed),
    })


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

    if not serial_manager.connected:
        return _fail("Gate controller (Arduino) is offline.", 503, error_code="GATE_OFFLINE", qr=qr)

    if _tracker and not _tracker.is_camera_healthy():
        return _fail("Camera offline or detection stale.", 503, error_code="CAMERA_OFFLINE", qr=qr)

    feed = _tracker.get_live_slot_feed() if _tracker else {}
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