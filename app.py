"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 1 + 2 + 3
REAL-TIME FLASK SERVER (app.py)
===============================================================================
Objective 1 : gate / entry flow from the minor project   (gate_routes.py)
Objective 2 : AI slot recommendation                     (green_score.py)
Objective 3 : air-quality monitoring                     (air_quality_*.py)

One Arduino, one serial port: serial_manager.py owns it for all three.
===============================================================================
"""

import cv2
import time
import threading
import numpy as np
from flask import Flask, render_template, jsonify, request, Response

import db
import payment
from realtime_tracker import RealtimeParkingTracker
from green_score import get_recommended_slot, compare_to_baseline

# Objective 3 - Air Quality
from air_quality_db import create_air_quality_table
from air_quality_routes import air_quality_bp

# Objective 1 - Gate / Arduino
from serial_manager import serial_manager
from gate_routes import gate_bp, init_gate_routes

app = Flask(__name__)
tracker = RealtimeParkingTracker()

# Objective 3 - register air-quality API routes (AFTER app is created)
app.register_blueprint(air_quality_bp)

# Objective 1 - register gate routes (needs the tracker for slot availability)
init_gate_routes(tracker, get_recommended_slot)
app.register_blueprint(gate_bp)

# ─────────────────────────────────────────────────────────────────────────────
# RAKHI'S SETUP — SQLite init + one-time slot-coordinate sync
# ─────────────────────────────────────────────────────────────────────────────
db.init_db()
db.sync_slot_coordinates(tracker.slots)

# Objective 1 - minor-project tables (drivers, sessions, parkingrecords, scanlogs)
payment.init_minor_tables()

# Objective 3 - create air_quality_readings table in smart_parking.db
create_air_quality_table()

# ─────────────────────────────────────────────────────────────────────────────
# Background Camera Thread
# Reads frames from webcam in a dedicated thread and caches the latest one.
# This is REQUIRED because cv2.VideoCapture.read() is NOT thread-safe in Flask.
# ─────────────────────────────────────────────────────────────────────────────
import queue

_latest_frame_lock = threading.Lock()
_latest_frame = None
_qr_frame_queue = queue.Queue(maxsize=2)
_qr_detector = cv2.QRCodeDetector()

_handled_qrs = {}            # {qr_text: {"handled_at": timestamp, "action": "entry"/"exit"}}
_qr_last_seen = {}           # {qr_text: timestamp}
_qr_lock = threading.Lock()
QR_ABSENT_TIMEOUT = 5.0      # Must be out of frame for 5s before re-scan allowed


def _decode_qr_frame(frame):
    """Multi-pass robust QR code decoder using cv2.QRCodeDetector with adaptive enhancements and pyzbar fallback."""
    if frame is None:
        return None, None
    try:
        # Pass 1: Direct OpenCV detection
        qr_text, points, _ = _qr_detector.detectAndDecode(frame)
        if qr_text and qr_text.strip():
            return qr_text.strip(), points

        # Pass 2: Grayscale + CLAHE contrast enhancement
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        qr_text, points, _ = _qr_detector.detectAndDecode(gray)
        if qr_text and qr_text.strip():
            return qr_text.strip(), points

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        qr_text, points, _ = _qr_detector.detectAndDecode(enhanced)
        if qr_text and qr_text.strip():
            return qr_text.strip(), points

        # Pass 3: Adaptive threshold
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 0)
        qr_text, points, _ = _qr_detector.detectAndDecode(thresh)
        if qr_text and qr_text.strip():
            return qr_text.strip(), points

        # Pass 4: Pyzbar fallback if available
        try:
            from pyzbar.pyzbar import decode as pyzbar_decode
            barcodes = pyzbar_decode(gray)
            for b in barcodes:
                txt = b.data.decode("utf-8", errors="ignore").strip()
                if txt:
                    return txt, None
        except Exception:
            pass

    except Exception:
        pass
    return None, None


def process_scanned_qr(qr_text: str):
    """Processes a detected QR string through server-side idempotency & absent-from-frame filters."""
    if not qr_text:
        return None
    qr_text = qr_text.strip()
    tracker.freeze_occupancy_for_qr(5.0)
    tracker.last_qr_detected = qr_text
    now = time.monotonic()

    with _qr_lock:
        last_handled = _handled_qrs.get(qr_text, {})
        handled_at = last_handled.get("handled_at", 0.0)

        # If QR was handled recently (< 5.0s) and is still visible in frame, ignore duplicate scan
        if now - handled_at < QR_ABSENT_TIMEOUT:
            _qr_last_seen[qr_text] = now
            return None

        # Determine explicit entry vs exit based on current gate direction
        from gate_routes import get_gate_direction, _do_entry, _do_exit
        direction = get_gate_direction()
        action = direction.lower()

        _handled_qrs[qr_text] = {"handled_at": now, "action": action}
        _qr_last_seen[qr_text] = now
        print(f"[SHARED CAM QR] Executing {direction} for QR code: '{qr_text}'")

        res = None
        with app.app_context():
            if direction == "EXIT":
                res = _do_exit(qr_text)
            else:
                res = _do_entry(qr_text)

        # Auto-revert camera mode back to SLOT_DETECTION after scan is accepted/rejected
        def _auto_return_to_slot():
            time.sleep(2.5)
            tracker.set_camera_mode("SLOT_DETECTION", reason="Scan completed, returned to Slot Detection")
        threading.Thread(target=_auto_return_to_slot, daemon=True).start()

        return res


def _on_arduino_ir_entry():
    """Callback invoked when the Arduino reports IR_ENTRY (car at entry gate)."""
    print("[AUTO-SWITCH] IR_ENTRY detected from Arduino -> switching camera to QR_SCAN mode in ENTRY direction.")
    tracker.set_camera_mode("QR_SCAN", reason="Vehicle at entry gate (IR_ENTRY)")
    try:
        from gate_routes import stepper, set_gate_direction
        set_gate_direction("ENTRY")
        stepper.on_vehicle_at_gate("IR_ENTRY")
    except Exception as e:
        print(f"[IR CALLBACK ERROR] {e}")


def _on_arduino_ir_exit():
    """Callback invoked when the Arduino reports IR_EXIT (car at exit gate)."""
    print("[AUTO-SWITCH] IR_EXIT detected from Arduino -> switching camera to QR_SCAN mode in EXIT direction.")
    tracker.set_camera_mode("QR_SCAN", reason="Vehicle at exit gate (IR_EXIT)")
    try:
        from gate_routes import stepper, set_gate_direction
        set_gate_direction("EXIT")
        stepper.on_vehicle_at_gate("IR_EXIT")
    except Exception as e:
        print(f"[IR CALLBACK ERROR] {e}")


# Hook serial manager to auto-switch camera mode on IR_ENTRY and IR_EXIT
serial_manager.on_ir_entry_callback = _on_arduino_ir_entry
serial_manager.on_ir_exit_callback = _on_arduino_ir_exit


def _camera_thread():
    """Runs forever in a daemon thread — captures frames, updates timestamps, caches latest frame.
    NEVER blocks on QR decoding or database/API calls."""
    global _latest_frame
    frame_counter = 0
    while True:
        try:
            # Check auto-timeout for QR scan mode (20 seconds)
            if tracker.camera_mode == "QR_SCAN" and tracker.qr_mode_timeout_at > 0:
                if time.time() > tracker.qr_mode_timeout_at:
                    tracker.set_camera_mode("SLOT_DETECTION", reason="QR scan mode timed out (20s)")
                    try:
                        from gate_routes import stepper
                        stepper.on_timeout()
                    except Exception:
                        pass

            frame = tracker.capture_next_frame()
            if frame is not None:
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ret:
                    with _latest_frame_lock:
                        _latest_frame = buffer.tobytes()

                # Offer frame to QR worker thread non-blockingly
                frame_counter += 1
                if frame_counter % 2 == 0:
                    try:
                        _qr_frame_queue.put_nowait(frame.copy())
                    except queue.Full:
                        pass
        except Exception as e:
            print(f'[CAM THREAD ERROR] {e}')
        time.sleep(0.05)   # ~20 FPS capture


def _qr_worker_thread():
    """Dedicated background worker thread pulling frames from queue to decode QR codes asynchronously."""
    while True:
        try:
            frame = _qr_frame_queue.get()
            if frame is not None:
                try:
                    qr_text, _ = _decode_qr_frame(frame)
                    if qr_text:
                        tracker.freeze_occupancy_for_qr(5.0)
                        process_scanned_qr(qr_text)
                except Exception as qr_err:
                    pass
            _qr_frame_queue.task_done()
        except Exception as e:
            print(f'[QR WORKER ERROR] {e}')
        time.sleep(0.01)


def _lcd_sync_thread():
    """
    Keeps the Arduino LCD in step with the software:
    sends TOTAL:n after every (re)connect and AVAILABLE:n whenever it changes.
    """
    last_connect = -1
    last_available = None
    while True:
        try:
            if serial_manager.connected:
                if serial_manager.connect_count != last_connect:
                    last_connect = serial_manager.connect_count
                    serial_manager.send(f"TOTAL:{len(tracker.slots)}")
                    last_available = None

                feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
                available = sum(1 for s in feed.values() if s["vacant"])
                if available != last_available:
                    if serial_manager.send(f"AVAILABLE:{available}"):
                        last_available = available
        except Exception as e:
            print(f'[LCD SYNC ERROR] {e}')
        time.sleep(2)


def _get_cached_frame():
    """Returns the latest JPEG bytes, or captures a frame, or returns a fallback placeholder."""
    global _latest_frame
    with _latest_frame_lock:
        if _latest_frame is not None:
            return _latest_frame

    # If background thread hasn't produced a frame yet, capture directly
    try:
        f = tracker.capture_next_frame()
        if f is not None:
            ret, buf = cv2.imencode('.jpg', f, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ret:
                with _latest_frame_lock:
                    _latest_frame = buf.tobytes()
                return buf.tobytes()
    except Exception as e:
        print(f"[CAMERA DIAGNOSTIC] Direct capture failed: {e}")

    blank = np.zeros((520, 600, 3), dtype=np.uint8)
    _, buf = cv2.imencode('.jpg', blank)
    return buf.tobytes()


def generate_video_stream():
    """MJPEG multipart generator — sends the cached frame to the browser."""
    while True:
        frame_bytes = _get_cached_frame()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.08)   # ~12 FPS to browser


# ─────────────────────────────────────────────────────────────────────────────
# Flask Routes
# ─────────────────────────────────────────────────────────────────────────────

_demo_mode = False


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/admin')
def admin():
    return render_template('admin.html')


@app.route('/api/demo/status')
def api_demo_status():
    return jsonify({"demo_mode": _demo_mode})


@app.route('/api/demo/toggle', methods=['POST'])
def api_demo_toggle():
    global _demo_mode
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        _demo_mode = bool(data["enabled"])
    else:
        _demo_mode = not _demo_mode
    return jsonify({"demo_mode": _demo_mode, "status": "ok"})


@app.route('/api/demo/reset', methods=['POST'])
def api_demo_reset():
    """Resets the demo state: backs up DB, clears active sessions & parking records,
    resets driver wallets to Rs. 500, clears stepper & transaction cards,
    resets camera mode to SLOT_DETECTION and direction to ENTRY, and optionally recalibrates baseline."""
    global _demo_mode
    _demo_mode = False
    data = request.get_json(silent=True) or {}
    recalibrate = bool(data.get("recalibrate", False))
    res = db.reset_demo_data(backup=True, recalibrate=recalibrate, tracker=tracker)
    return jsonify(res)


@app.route('/video_feed')
def video_feed():
    """MJPEG stream (kept for compatibility)."""
    headers = {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    }
    return Response(generate_video_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame',
                    headers=headers)


@app.route('/api/frame')
def api_frame():
    """Returns the latest camera frame as a single JPEG image.
    Used by the JavaScript polling loop in index.html for reliable display."""
    if not tracker.is_camera_healthy() and not _demo_mode:
        reason = tracker.get_unhealthy_reason()
        now = time.monotonic()
        if not hasattr(api_frame, "_last_warn") or (now - api_frame._last_warn > 5.0):
            api_frame._last_warn = now
            print(f"[CAMERA DIAGNOSTIC] /api/frame: Camera feed not delivering live frames ({reason})")

    frame_bytes = _get_cached_frame()
    return Response(
        frame_bytes,
        mimetype='image/jpeg',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    )


@app.route('/api/slots')
def api_slots():
    feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
    try:
        occupants = db.get_slot_occupants()
        for sid, s in feed.items():
            s["occupant"] = occupants.get(sid, None)
    except Exception as e:
        print(f"[SLOTS OCCUPANT ERROR] {e}")
    return jsonify(feed)


@app.route('/api/system/status')
def api_system_status():
    """One-stop health check for the whole integrated system."""
    feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
    ard_status = serial_manager.status()
    is_cam_healthy = tracker.is_camera_healthy()
    cam_active = is_cam_healthy or _demo_mode
    unhealthy_reason = tracker.get_unhealthy_reason() if not is_cam_healthy else None

    if not is_cam_healthy and not _demo_mode:
        now = time.monotonic()
        if not hasattr(api_system_status, "_last_log") or (now - api_system_status._last_log > 5.0):
            api_system_status._last_log = now
            print(f"[CAMERA DIAGNOSTIC] System status check: Camera not healthy -> {unhealthy_reason}")

    mode_info = tracker.get_camera_mode_info()
    kpis = db.get_kpis(feed)

    if _demo_mode:
        ard_status["connected"] = True
        ard_status["port"] = ard_status.get("port") or "COM7 (Simulated)"
        if not ard_status.get("last_air"):
            ard_status["last_air"] = {
                "sensor": "MQ135",
                "raw_value": 285,
                "alert_level": "SAFE",
                "exceedance": False,
                "message": "Simulated air reading within safe range."
            }

    from gate_routes import get_gate_direction
    return jsonify({
        'camera_active': cam_active,
        'camera_unhealthy_reason': unhealthy_reason,
        'total_slots': len(feed),
        'vacant_slots': sum(1 for s in feed.values() if s['vacant']),
        'arduino': ard_status,
        'demo_mode': _demo_mode,
        'camera_mode': tracker.camera_mode,
        'gate_direction': get_gate_direction(),
        'last_mode_change': mode_info.get("last_mode_change"),
        'last_mode_change_iso': mode_info.get("last_mode_change_iso"),
        'qr_banner': tracker.qr_banner,
        'kpis': kpis,
    })


@app.route('/api/camera/mode', methods=['GET', 'POST'])
def api_camera_mode():
    """Gets or sets the camera operational mode (SLOT_DETECTION vs QR_SCAN)."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode", "SLOT_DETECTION")).upper()
        reason = data.get("reason", "Manual user mode switch")
        tracker.set_camera_mode(mode, reason=reason)
        if mode == "QR_SCAN":
            try:
                from gate_routes import stepper
                stepper.on_vehicle_at_gate("Manual mode switch")
            except Exception:
                pass
    return jsonify(tracker.get_camera_mode_info())


@app.route('/api/system/kpis')
def api_system_kpis():
    """Returns Free slots, Occupied slots, Active sessions, and Collected today (Rs)."""
    feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
    return jsonify(db.get_kpis(feed))


@app.route('/api/slots/occupants')
def api_slots_occupants():
    """Returns current active occupant details per parking slot."""
    return jsonify(db.get_slot_occupants())


@app.route('/api/camera/health')
def api_camera_health():
    """Phase 1 diagnostic endpoint for camera thread & health tracking."""
    info = tracker.get_camera_health_info()
    info["thread_alive"] = bool(cam_thread and cam_thread.is_alive()) if 'cam_thread' in globals() else False
    return jsonify(info)


@app.route('/api/camera/reconnect', methods=['POST'])
def api_camera_reconnect():
    global _demo_mode
    success = tracker.init_camera()
    if success:
        _demo_mode = False
    return jsonify({'status': 'ok' if success else 'failed',
                    'camera_active': tracker.use_live_webcam})


@app.route('/api/camera/stop', methods=['POST'])
def api_camera_stop():
    global _demo_mode
    tracker.stop_camera()
    _demo_mode = True
    return jsonify({'status': 'ok', 'camera_active': False})


@app.route('/api/camera/toggle', methods=['POST'])
def api_camera_toggle():
    if tracker.use_live_webcam:
        tracker.stop_camera()
        return jsonify({'status': 'stopped', 'camera_active': False})
    else:
        success = tracker.init_camera()
        return jsonify({'status': 'started' if success else 'failed',
                        'camera_active': tracker.use_live_webcam})


@app.route('/api/camera/calibrate', methods=['POST'])
def api_camera_calibrate():
    success = tracker.calibrate_baseline()
    return jsonify({
        'status': 'ok' if success else 'failed',
        'message': 'Baseline calibrated for empty slots' if success else 'Calibration failed',
        'calibration': tracker.get_calibration_info(),
        'data': tracker.get_live_slot_feed(demo_mode=_demo_mode)
    })


@app.route('/api/camera/diagnostics')
def api_camera_diagnostics():
    """Returns live per-slot detection metrics vs thresholds and baseline calibration prompt status."""
    return jsonify(tracker.get_slot_diagnostics())


@app.route('/api/camera/thresholds', methods=['POST'])
def api_camera_thresholds():
    """Updates per-slot free and occupied thresholds from /admin on the spot."""
    data = request.get_json(silent=True) or {}
    slot_id = data.get("slot_id")
    free_th = data.get("free_threshold")
    occ_th = data.get("occupied_threshold")

    if not slot_id or slot_id not in tracker.slots:
        return jsonify({"status": "error", "message": "Valid slot_id is required"}), 400

    ok = tracker.set_slot_thresholds(slot_id, free_th=free_th, occ_th=occ_th)
    return jsonify({
        "status": "ok" if ok else "failed",
        "slot_id": slot_id,
        "thresholds": tracker.thresholds.get(slot_id, {})
    })


@app.route('/api/camera/hardware', methods=['GET', 'POST'])
def api_camera_hardware():
    """Gets or sets camera brightness, exposure, and auto-exposure hardware controls."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        brightness = data.get('brightness')
        exposure = data.get('exposure')
        auto_exposure = data.get('auto_exposure')
        ok, res = tracker.set_camera_hardware(brightness=brightness, exposure=exposure, auto_exposure=auto_exposure)
        return jsonify({
            'status': 'ok' if ok else 'failed',
            'result': res,
            'mean_brightness': round(getattr(tracker, 'last_mean_brightness', 0.0), 1),
            'is_too_dark': getattr(tracker, 'last_mean_brightness', 0.0) < 50.0
        }), (200 if ok else 400)
    else:
        return jsonify(tracker.get_camera_hardware_info())


@app.route('/api/sync_state', methods=['POST'])
def api_sync_state():
    global _demo_mode
    _demo_mode = True
    data = request.get_json(silent=True) or {}
    saved_occupancy = data.get('occupancy', {})
    for slot_id, state in saved_occupancy.items():
        if slot_id in tracker.occupancy_status:
            tracker.occupancy_status[slot_id] = bool(state)
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed(demo_mode=_demo_mode)})


@app.route('/api/toggle', methods=['POST'])
def api_toggle():
    global _demo_mode
    _demo_mode = True
    data = request.get_json(silent=True) or {}
    slot_id = data.get('slot_id')
    if slot_id in tracker.occupancy_status:
        tracker.occupancy_status[slot_id] = not tracker.occupancy_status[slot_id]
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed(demo_mode=_demo_mode)})


@app.route('/api/recommend')
def api_recommend():
    """
    RAKHI'S ENDPOINT — Objective 2, task 1.
    Pulls Shubam's live feed -> Ramandeep's scoring -> logs to SQLite -> returns JSON.
    """
    feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
    db.log_congestion(feed)                 # congestion_history row per slot
    rec = get_recommended_slot(feed)        # Ramandeep's module picks the best slot
    db.log_recommendation(rec)              # recommendation_log row (timestamp, slot, score)
    return jsonify(rec)


@app.route('/api/recommend/compare')
def api_recommend_compare():
    """Scored pick vs naive first-available-slot baseline, for report/viva."""
    feed = tracker.get_live_slot_feed(demo_mode=_demo_mode)
    return jsonify(compare_to_baseline(feed))


@app.route('/api/recommend/history')
def api_recommend_history():
    """Recent recommendation events from SQLite (for dashboard/report)."""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(db.get_recommendation_history(limit))


@app.route('/api/preset/<scenario_id>', methods=['POST'])
def api_preset(scenario_id):
    global _demo_mode
    _demo_mode = True
    if scenario_id == 'test1':
        for s in tracker.occupancy_status:
            tracker.occupancy_status[s] = True
        tracker.occupancy_status["A1"] = False
        tracker.occupancy_status["A2"] = False
    elif scenario_id == 'test2':
        for s in tracker.occupancy_status:
            tracker.occupancy_status[s] = True
        tracker.occupancy_status["A1"] = False
        tracker.occupancy_status["B2"] = False
    elif scenario_id == 'reset':
        tracker.occupancy_status = {
            "A1": True,  "A2": False,
            "B1": True,  "B2": False
        }
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed(demo_mode=_demo_mode)})


if __name__ == '__main__':
    print("Starting Smart Green Parking System at http://127.0.0.1:5000 ...")

    # Start background camera thread BEFORE Flask begins serving requests
    cam_thread = threading.Thread(target=_camera_thread, daemon=True)
    cam_thread.start()
    print("[THREAD] Background camera capture thread started.")

    # Start decoupled QR decoding worker thread
    qr_thread = threading.Thread(target=_qr_worker_thread, daemon=True)
    qr_thread.start()
    print("[THREAD] Decoupled QR worker thread started.")

    # Objectives 1 + 3 - ONE serial manager for gate commands + MQ-135 readings
    serial_manager.start()
    print("[THREAD] Arduino serial manager started.")

    # Keep the Arduino LCD slot count in step with the software
    lcd_thread = threading.Thread(target=_lcd_sync_thread, daemon=True)
    lcd_thread.start()
    print("[THREAD] LCD sync thread started.")

    # use_reloader=False prevents Flask from spawning a 2nd process that
    # would steal the camera handle (and the serial port) from the main process.
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)