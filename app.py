"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 2 + OBJECTIVE 3
REAL-TIME FLASK SERVER (app.py)
===============================================================================
"""

import cv2
import time
import threading
import numpy as np
from flask import Flask, render_template, jsonify, request, Response
from realtime_tracker import RealtimeParkingTracker
from green_score import get_recommended_slot, compare_to_baseline
import importlib
import db

# Objective 3 - Air Quality
from air_quality_db import create_air_quality_table
from air_quality_routes import air_quality_bp

# Load the project-local database module without requiring it to be installed
# as a third-party package.
db = importlib.import_module('db')

app = Flask(__name__)
tracker = RealtimeParkingTracker()

# Objective 3 - register air-quality API routes (AFTER app is created)
app.register_blueprint(air_quality_bp)

# ─────────────────────────────────────────────────────────────────────────────
# RAKHI'S SETUP — SQLite init + one-time slot-coordinate sync
# ─────────────────────────────────────────────────────────────────────────────
db.init_db()
db.sync_slot_coordinates(tracker.slots)

# Objective 3 - create air_quality_readings table in smart_parking.db
create_air_quality_table()

# ─────────────────────────────────────────────────────────────────────────────
# Background Camera Thread
# Reads frames from webcam in a dedicated thread and caches the latest one.
# This is REQUIRED because cv2.VideoCapture.read() is NOT thread-safe in Flask.
# ─────────────────────────────────────────────────────────────────────────────
_latest_frame_lock = threading.Lock()
_latest_frame = None


def _camera_thread():
    """Runs forever in a daemon thread — captures, processes & caches each frame."""
    global _latest_frame
    while True:
        try:
            frame = tracker.capture_next_frame()
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ret:
                with _latest_frame_lock:
                    _latest_frame = buffer.tobytes()
        except Exception as e:
            print(f'[CAM THREAD ERROR] {e}')
        time.sleep(0.05)   # ~20 FPS capture


def _air_quality_thread():
    """
    Objective 3 - reads MQ-135 values from Arduino over Serial and logs them
    to SQLite. If Arduino is not connected, this thread ends quietly and the
    Flask server keeps running normally.
    """
    try:
        from air_quality_serial import start_serial_monitor
        start_serial_monitor()
    except Exception as e:
        print(f'[AIR QUALITY THREAD ERROR] {e}')


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
        tracker.occupancy_status["B1"] = False
    elif scenario_id == 'test2':
        for s in tracker.occupancy_status:
            tracker.occupancy_status[s] = True
        tracker.occupancy_status["A1"] = False
        tracker.occupancy_status["A2"] = True
        tracker.occupancy_status["B1"] = True
        tracker.occupancy_status["C2"] = False
        tracker.occupancy_status["C3"] = False
    elif scenario_id == 'reset':
        tracker.occupancy_status = {
            "A1": True,  "A2": False, "A3": False,
            "B1": True,  "B2": False, "B3": True,
            "C1": False, "C2": False, "C3": True
        }
    return jsonify({'status': 'ok', 'data': tracker.get_live_slot_feed()})


if __name__ == '__main__':
    print("Starting Smart Green Parking System at http://127.0.0.1:5000 ...")

    # Start background camera thread BEFORE Flask begins serving requests
    cam_thread = threading.Thread(target=_camera_thread, daemon=True)
    cam_thread.start()
    print("[THREAD] Background camera capture thread started.")

    # Objective 3 - start Arduino MQ-135 serial reader thread
    aq_thread = threading.Thread(target=_air_quality_thread, daemon=True)
    aq_thread.start()
    print("[THREAD] Air-quality serial thread started.")

    # use_reloader=False prevents Flask from spawning a 2nd process that
    # would steal the camera handle from the main process.
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)