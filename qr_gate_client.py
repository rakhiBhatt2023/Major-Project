
"""
SmartPark AI - QR gate scanner

Reads QR codes from a camera and sends each one to the running Flask app
(/api/gate/scan).

Run it in a SECOND terminal while app.py is running:
    python qr_gate_client.py

CAMERA:
- app.py uses camera 0 for the parking tracker.
- If using a second camera, keep QR_CAMERA_INDEX = 1.
- If app.py camera is stopped, you can use QR_CAMERA_INDEX = 0.
"""

import os
import json
import time
import urllib.error
import urllib.request
import numpy as np
import cv2

API_URL = "http://127.0.0.1:5000/api/gate/scan"
FRAME_URL = "http://127.0.0.1:5000/api/frame"

# Configurable source via environment variable or default index
QR_CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
STREAM_URL = os.environ.get("STREAM_URL", "")

# Seconds before the same QR can be accepted again.
QR_COOLDOWN = 3.0

last_text = ""
last_time = 0.0
banner = "Show a registered QR"


def send_scan(qr_text):
    data = json.dumps({"qr": qr_text}).encode()

    req = urllib.request.Request(
        API_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())

    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"message": "server error"}

    except Exception as e:
        return 0, {"message": f"cannot reach app.py: {e}"}


def fetch_http_frame():
    """Fallback: fetches the latest camera frame from app.py /api/frame if camera 0 is locked."""
    try:
        req = urllib.request.Request(FRAME_URL, headers={"User-Agent": "QRClient"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            img_data = resp.read()
            nparr = np.frombuffer(img_data, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def main():
    global last_text, last_time, banner

    force_local = os.environ.get("FORCE_LOCAL_CAMERA", "0") == "1"
    cap = None
    use_http_fallback = False

    if not force_local and not STREAM_URL:
        use_http_fallback = True
        print(f"[INFO] Using shared app.py camera stream ({FRAME_URL}) — no camera device locking.")
    elif STREAM_URL:
        print(f"Connecting to QR camera stream URL: {STREAM_URL}")
        cap = cv2.VideoCapture(STREAM_URL)
    else:
        print(f"Opening local QR camera device {QR_CAMERA_INDEX}...")
        cap = cv2.VideoCapture(QR_CAMERA_INDEX, cv2.CAP_DSHOW)

    if not use_http_fallback and (not cap or not cap.isOpened()):
        print(f"[WARN] Camera {QR_CAMERA_INDEX} not openable directly. Switching to shared app.py HTTP stream ({FRAME_URL}).")
        use_http_fallback = True
    elif cap and cap.isOpened():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # OpenCV's built-in QR detector.
    qr_detector = cv2.QRCodeDetector()

    print("QR gate scanner started. Press Q in the window to quit.")

    while True:
        if use_http_fallback:
            frame = fetch_http_frame()
            if frame is None:
                time.sleep(0.1)
                continue
        else:
            ok, frame = cap.read()
            if not ok or frame is None:
                # Direct capture failed — fallback to shared app.py stream
                frame = fetch_http_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue

        # ---------------------------------------------------------
        # QR CODE DETECTION
        # ---------------------------------------------------------
        try:
            qr_text, points, _ = qr_detector.detectAndDecode(frame)

            if qr_text:
                qr_text = qr_text.strip()

                now = time.time()

                if (
                    qr_text
                    and not (
                        qr_text == last_text
                        and now - last_time < QR_COOLDOWN
                    )
                ):
                    last_text = qr_text
                    last_time = now

                    print("QR detected:", qr_text)

                    status, body = send_scan(qr_text)

                    action = (body or {}).get("action", "")
                    message = (body or {}).get("message", "")

                    banner = f"{status} {action} {message}".strip()

                    print("Scan result:", banner)

                    if status == 200 and action == "entry":
                        slot = (
                            (body or {})
                            .get("recommended_slot", {})
                            .get("slot_id")
                        )

                        print(f"  -> go to slot {slot}")

        except Exception as e:
            print("QR detection error:", e)

        # ---------------------------------------------------------
        # DISPLAY STATUS
        # ---------------------------------------------------------
        cv2.rectangle(
            frame,
            (0, 0),
            (640, 34),
            (15, 23, 42),
            -1,
        )

        cv2.putText(
            frame,
            banner[:60],
            (8, 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
        )

        cv2.imshow("SmartPark QR Gate", frame)

        # Press Q to quit.
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
