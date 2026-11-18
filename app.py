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


def process_scanned_qr(qr_text: str):
    """Processes a detected QR string through server-side idempotency & absent-from-frame filters."""
    if not qr_text:
        return None
    qr_text = qr_text.strip()
    now = time.monotonic()

    with _qr_lock:
        last_handled = _handled_qrs.get(qr_text, {})
        handled_at = last_handled.get("handled_at", 0.0)

        # If QR was handled recently (< 5.0s) and is still visible in frame, ignore duplicate scan
        if now - handled_at < QR_ABSENT_TIMEOUT:
            _qr_last_seen[qr_text] = now
            return None

        # Determine entry vs exit based on active session
        has_active = payment.has_active_session(qr_text)
        action = "exit" if has_active else "entry"

        _handled_qrs[qr_text] = {"handled_at": now, "action": action}
        _qr_last_seen[qr_text] = now
        print(f"[SHARED CAM QR] Executing {action.upper()} for QR code: '{qr_text}'")

        with app.app_context():
            from gate_routes import _do_entry, _do_exit
            if has_active:
                return _do_exit(qr_text)
            else:
                return _do_entry(qr_text)


def _camera_thread():
    """Runs forever in a daemon thread — captures frames, updates timestamps, caches latest frame.
    NEVER blocks on QR decoding or database/API calls."""
    global _latest_frame
    frame_counter = 0
    while True:
        try:
            frame = tracker.capture_next_frame()
            if frame is not None:
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ret:
                    with _latest_frame_lock:
                        _latest_frame = buffer.tobytes()

                # Offer frame to QR worker thread non-blockingly every 3rd frame
                frame_counter += 1
                if frame_counter % 3 == 0:
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
                    qr_text, _, _ = _qr_detector.detectAndDecode(frame)
                    if qr_text:
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

                feed = tracker.get_live_slot_feed()
                available = sum(1 for s in feed.values() if s["vacant"])
                if available != last_available:
                    if serial_manager.send(f"AVAILABLE:{available}"):
                        last_available = available
        except Exception as e:
            print(f'[LCD SYNC ERROR] {e}')
        time.sleep(2)


def _get_cached_frame():
    """Returns the latest JPEG bytes, or a black placeholder if not ready."""
    with _latest_frame_lock:
        if _latest_frame is not None:
            return _latest_frame
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

@app.route('/')
def index():
    return render_template('index.html')


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
    return jsonify(tracker.get_live_slot_feed())


@app.route('/api/system/status')
def api_system_status():
    """One-stop health check for the whole integrated system."""
    feed = tracker.get_live_slot_feed()
    return jsonify({
        'camera_active': tracker.use_live_webcam,
        'total_slots': len(feed),
        'vacant_slots': sum(1 for s in feed.values() if s['vacant']),
        'arduino': serial_manager.status(),
    })


@app.route('/api/camera/health')
def api_camera_health():
    """Phase 1 diagnostic endpoint for camera thread & health tracking."""
    info = tracker.get_camera_health_info()
    info["thread_alive"] = bool(cam_thread and cam_thread.is_alive()) if 'cam_thread' in globals() else False
    return jsonify(info)


@app.route('/api/camera/reconnect', methods=['POST'])
def api_camera_reconnect():
    success = tracker.init_camera()
    return jsonify({'status': 'ok' if success else 'failed',
                    'camera_active': tracker.use_live_webcam})


@app.route('/api/camera/stop', methods=['POST'])
def api_camera_stop():
    tracker.stop_camera()
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
        'data': tracker.get_live_slot_feed()
    })


@app.route('/api/sync_state', methods=['POST'])
def api_sync_state():
    data = request.get_json(silent=True) or {}
    saved_occupancy = data.get('occupancy', {})
    for slot_id, state in saved_occupancy.items():
        if slot_id in tracker.occupancy_status:
            tracker.occupancy_status[slot_id] = bool(state)
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed()})


@app.route('/api/toggle', methods=['POST'])
def api_toggle():
    data = request.get_json(silent=True) or {}
    slot_id = data.get('slot_id')
    if slot_id in tracker.occupancy_status:
        tracker.occupancy_status[slot_id] = not tracker.occupancy_status[slot_id]
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed()})


@app.route('/api/recommend')
def api_recommend():
    """
    RAKHI'S ENDPOINT — Objective 2, task 1.
    Pulls Shubam's live feed -> Ramandeep's scoring -> logs to SQLite -> returns JSON.
    """
    feed = tracker.get_live_slot_feed()
    db.log_congestion(feed)                 # congestion_history row per slot
    rec = get_recommended_slot(feed)        # Ramandeep's module picks the best slot
    db.log_recommendation(rec)              # recommendation_log row (timestamp, slot, score)
    return jsonify(rec)


@app.route('/api/recommend/compare')
def api_recommend_compare():
    """Scored pick vs naive first-available-slot baseline, for report/viva."""
    feed = tracker.get_live_slot_feed()
    return jsonify(compare_to_baseline(feed))


@app.route('/api/recommend/history')
def api_recommend_history():
    """Recent recommendation events from SQLite (for dashboard/report)."""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(db.get_recommendation_history(limit))


@app.route('/api/preset/<scenario_id>', methods=['POST'])
def api_preset(scenario_id):
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
            "B1": False, "B2": False
        }
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed()})


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