"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 2
SHUBAM SARMAL'S MODULE: REAL-TIME DISTANCE & CONGESTION TRACKER
===============================================================================
This module handles Shubam's assigned work for Objective 2:
  1. Fixed distance-from-entry-gate mapping for every labelled slot
  2. Live camera ROI occupancy detection (OpenCV)
  3. Local congestion calculation per slot (nearby occupied slot count)
  4. Real-time update of occupancy + congestion from camera feed
  5. Structured JSON data feed served for Ramandeep's scoring module

NOTE: This module does NOT do recommendation, scoring, or slot selection.
      That is Ramandeep's work. This module provides the raw input data.
===============================================================================
"""

import cv2
import time
import numpy as np

class RealtimeParkingTracker:
    def __init__(self):
        # PART 1: Configurable Parking Slot Distance Mapping (meters from entry gate)
        self.slots = {
            "A1": {"bbox": (60,  100, 130, 95), "distance": 5.0},
            "A2": {"bbox": (220, 100, 130, 95), "distance": 7.0},
            "B1": {"bbox": (60,  240, 130, 95), "distance": 8.0},
            "B2": {"bbox": (220, 240, 130, 95), "distance": 10.0},
        }

        # PART 3: Configurable Spatial Adjacency Network for Local Congestion
        self.adjacent_network = {
            "A1": ["A2", "B1"],
            "A2": ["A1", "B2"],
            "B1": ["A1", "B2"],
            "B2": ["A2", "B1"],
        }

        # PART 2 & PART 9: Initial Slot Occupancy Status (Default: All Vacant)
        self.occupancy_status = {
            "A1": False, "A2": False,
            "B1": False, "B2": False,
        }
        self.roi_pixel_counts = {k: 0 for k in self.slots}
        self.baseline_rois = {}

        # Camera Health & Reservation State
        self.last_frame_timestamp = 0.0      # Monotonic timestamp of last successful cap.read()
        self.last_detection_timestamp = 0.0  # Monotonic timestamp of last frame ROI inference
        self.STALE_TIMEOUT = 10.0            # seconds (strict safety threshold)
        self.consecutive_failures = 0
        self.reconnect_attempts = 0
        self.last_reconnect_time = 0.0
        self.active_backend_name = "CAP_DSHOW"
        self.reservations = {}               # { slot_id: { tx_id, qr, timestamp, expire_at } }

        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)

        self.cap = None
        self.use_live_webcam = False
        self.active_camera_index = -1
        self.scan_line_y = 0

        self.init_camera()

    def is_camera_healthy(self) -> bool:
        """Returns True if webcam is active, fresh frames are captured, AND fresh detection is processed."""
        if not self.use_live_webcam or not self.cap or not self.cap.isOpened():
            return False
        now = time.monotonic()
        frame_fresh = (now - self.last_frame_timestamp) < self.STALE_TIMEOUT
        detection_fresh = (now - self.last_detection_timestamp) < self.STALE_TIMEOUT
        return frame_fresh and detection_fresh

    def get_unhealthy_reason(self) -> str:
        """Returns an explicit human-readable string explaining why the camera/detection is unhealthy."""
        if not self.use_live_webcam:
            if getattr(self, 'reconnect_attempts', 0) > 0:
                return f"reconnecting (attempt {self.reconnect_attempts})"
            return "camera stopped"
        if not self.cap or not self.cap.isOpened():
            return "camera device unopened"
        now = time.monotonic()
        frame_age = round(now - self.last_frame_timestamp, 1) if self.last_frame_timestamp > 0 else 9999.0
        detection_age = round(now - self.last_detection_timestamp, 1) if self.last_detection_timestamp > 0 else 9999.0
        if frame_age >= self.STALE_TIMEOUT:
            return f"no frame for {frame_age}s"
        if detection_age >= self.STALE_TIMEOUT:
            return f"detection stale for {detection_age}s"
        return "healthy"

    def get_camera_health_info(self) -> dict:
        """Returns detailed camera diagnostic metrics for debugging and API health status."""
        import os
        now = time.monotonic()
        frame_age = round(now - self.last_frame_timestamp, 2) if self.last_frame_timestamp > 0 else 9999.0
        detection_age = round(now - self.last_detection_timestamp, 2) if self.last_detection_timestamp > 0 else 9999.0
        opened = bool(self.cap and self.cap.isOpened())
        return {
            "camera_opened": opened,
            "use_live_webcam": self.use_live_webcam,
            "camera_index": self.active_camera_index,
            "last_frame_age_seconds": frame_age,
            "last_detection_age_seconds": detection_age,
            "unhealthy_reason": self.get_unhealthy_reason(),
            "consecutive_read_failures": getattr(self, "consecutive_failures", 0),
            "reconnect_attempts": getattr(self, "reconnect_attempts", 0),
            "backend_used": getattr(self, "active_backend_name", "CAP_DSHOW"),
            "process_id": os.getpid(),
            "is_healthy": self.is_camera_healthy()
        }

    def cleanup_expired_reservations(self):
        """Releases slot reservations older than their expiration duration."""
        now = time.time()
        expired = [s for s, r in self.reservations.items() if now > r.get("expire_at", 0)]
        for s in expired:
            del self.reservations[s]
            print(f"[RESERVATION] Slot {s} reservation expired and released.")

    def reserve_slot(self, slot_id: str, tx_id: str, qr: str, duration: float = 60.0) -> bool:
        """Temporarily reserves a vacant slot for an active transaction."""
        self.cleanup_expired_reservations()
        if not self.is_camera_healthy():
            return False
        if self.occupancy_status.get(slot_id, True):
            return False  # Slot physically occupied
        if slot_id in self.reservations:
            if self.reservations[slot_id].get("tx_id") == tx_id:
                return True  # Idempotent re-reservation
            return False  # Reserved by another transaction

        now = time.time()
        self.reservations[slot_id] = {
            "tx_id": tx_id,
            "qr": qr,
            "timestamp": now,
            "expire_at": now + duration
        }
        print(f"[RESERVATION] Slot {slot_id} reserved for TX {tx_id} ({duration}s).")
        return True

    def release_reservation(self, slot_id: str = None, tx_id: str = None):
        """Releases a slot reservation by slot_id or tx_id."""
        if slot_id and slot_id in self.reservations:
            del self.reservations[slot_id]
        if tx_id:
            to_del = [s for s, r in self.reservations.items() if r.get("tx_id") == tx_id]
            for s in to_del:
                del self.reservations[s]

    def calibrate_baseline(self, frame=None):
        """Captures the baseline appearance of empty parking slots."""
        if frame is None and self.use_live_webcam and self.cap and self.cap.isOpened():
            ret, cap_frame = self.cap.read()
            if ret and cap_frame is not None:
                frame = cap_frame

        if frame is None:
            frame = np.zeros((520, 600, 3), dtype=np.uint8)
            frame[:] = (15, 23, 42)

        frame = cv2.resize(frame, (600, 520))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)

        for slot_id, data in self.slots.items():
            x, y, w, h = data["bbox"]
            if y + h <= blur.shape[0] and x + w <= blur.shape[1]:
                self.baseline_rois[slot_id] = blur[y:y+h, x:x+w].copy()
                self.occupancy_status[slot_id] = False

        print(f"[CALIBRATION] Baseline empty slot reference saved for slots: {list(self.slots.keys())}")
        return True

    def auto_reconnect(self) -> bool:
        """Attempts background reconnect with exponential backoff on repeated read failures."""
        now = time.monotonic()
        backoff = min(10.0, 1.0 * (2 ** min(self.reconnect_attempts, 4)))
        if now - self.last_reconnect_time < backoff:
            return False
        self.last_reconnect_time = now
        self.reconnect_attempts += 1
        print(f"[RECONNECT] Background camera reconnect attempt {self.reconnect_attempts} (backoff={backoff}s)...")
        return self.init_camera()

    def init_camera(self):
        """Try to open the webcam. Configurable via CAMERA_INDEX env var."""
        import os
        if self.cap and self.cap.isOpened():
            self.cap.release()

        target_idx = int(os.environ.get("CAMERA_INDEX", 0))
        attempts = [
            (target_idx, cv2.CAP_DSHOW),
            (target_idx, cv2.CAP_MSMF),
            (target_idx, cv2.CAP_ANY),
        ]

        for idx, backend in attempts:
            try:
                cap = cv2.VideoCapture(idx, backend)
                if cap and cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        self.cap = cap
                        self.use_live_webcam = True
                        self.active_camera_index = idx
                        now = time.monotonic()
                        self.last_frame_timestamp = now
                        self.last_detection_timestamp = now
                        self.consecutive_failures = 0
                        self.reconnect_attempts = 0
                        self.active_backend_name = "CAP_DSHOW" if backend == cv2.CAP_DSHOW else ("CAP_MSMF" if backend == cv2.CAP_MSMF else "CAP_ANY")
                        print(f'[SUCCESS] Live Camera Connected on Device {idx} ({self.active_backend_name}).')
                        return True
                if cap:
                    cap.release()
            except Exception:
                pass

        self.use_live_webcam = False
        print('[INFO] No live camera found — using synthetic frame view.')
        return False

    def stop_camera(self):
        if self.cap and self.cap.isOpened():
            self.cap.release()
            self.cap = None
        self.use_live_webcam = False
        self.active_camera_index = -1
        print("[INFO] Camera capture stopped cleanly.")
        return True

    # PART 2 & PART 4: Process Camera Frame & Compute ROI Occupancy
    def process_camera_frame(self, frame, threshold_pixels=1600):
        if frame is None:
            return self.occupancy_status

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)
        fg_mask = self.bg_subtractor.apply(blur)

        for slot_id, data in self.slots.items():
            x, y, w, h = data["bbox"]
            if y + h <= blur.shape[0] and x + w <= blur.shape[1]:
                roi_blur = blur[y:y+h, x:x+w]
                roi_edges = edges[y:y+h, x:x+w]
                roi_fg = fg_mask[y:y+h, x:x+w]

                edge_count = cv2.countNonZero(roi_edges)
                motion_count = cv2.countNonZero(roi_fg)
                total_density = edge_count + (motion_count * 2)
                self.roi_pixel_counts[slot_id] = total_density

                if slot_id in self.baseline_rois:
                    base_roi = self.baseline_rois[slot_id]
                    diff = cv2.absdiff(roi_blur, base_roi)
                    diff_count = cv2.countNonZero(diff > 35)
                    mean_diff = float(np.mean(diff))

                    # Occupied if significant change from baseline empty state
                    is_occupied = (diff_count > 1850) or (mean_diff > 20.0) or (motion_count > 1500)
                else:
                    is_occupied = (edge_count > 1500) or (motion_count > 1500)

                # Update live occupancy status
                if self.use_live_webcam:
                    self.occupancy_status[slot_id] = is_occupied

        self.last_detection_timestamp = time.monotonic()
        return self.occupancy_status

    # PART 4: Calculate Local Spatial Congestion
    def calculate_congestion(self, slot_id: str):
        neighbors = self.adjacent_network.get(slot_id, [])
        occupied_count = sum(1 for n in neighbors if self.occupancy_status.get(n, False))

        if occupied_count == 0:
            level = "LOW"
        elif occupied_count <= 2:
            level = "MEDIUM"
        else:
            level = "HIGH"

        return occupied_count, level

    # Live slot data payload — feeds distance + status + congestion
    def get_live_slot_feed(self) -> dict:
        """Returns real-time data for all slots with 4 states (VACANT, OCCUPIED, RESERVED, UNKNOWN)."""
        self.cleanup_expired_reservations()
        healthy = self.is_camera_healthy()
        feed = {}

        for slot_id, data in self.slots.items():
            is_occ = self.occupancy_status[slot_id]
            is_res = slot_id in self.reservations

            if not healthy:
                status = "UNKNOWN"
                vacant = False
                reserved = False
                unknown = True
            elif is_res:
                status = "RESERVED"
                vacant = False
                reserved = True
                unknown = False
            elif is_occ:
                status = "OCCUPIED"
                vacant = False
                reserved = False
                unknown = False
            else:
                status = "VACANT"
                vacant = True
                reserved = False
                unknown = False

            cg_count, cg_level = self.calculate_congestion(slot_id)

            feed[slot_id] = {
                "slot_id": slot_id,
                "distance": data["distance"],
                "status": status,
                "vacant": vacant,
                "reserved": reserved,
                "unknown": unknown,
                "congestion": cg_count,
                "congestion_level": cg_level,
                "pixel_density": self.roi_pixel_counts.get(slot_id, 0),
                "camera_active": healthy,
                "camera_index": self.active_camera_index,
                "last_frame_timestamp": self.last_frame_timestamp,
                "detection_fresh": healthy
            }

        return feed

    # Debug console output
    def print_debug_console(self):
        """Prints the current live slot feed to the terminal for debugging."""
        feed = self.get_live_slot_feed()

        print("\n------------------------------------------")
        print("SHUBAM'S LIVE PARKING DATA")
        print("------------------------------------------")
        for slot_id, s in feed.items():
            print(f"{slot_id} | {s['status']:<8} | Distance: {s['distance']:<4}m | Congestion: {s['congestion']} ({s['congestion_level']})")

        print("\n------------------------------------------")
        print("VACANT SLOT DATA (ready for scoring module)")
        print("------------------------------------------")
        vacant_list = [slot_id for slot_id, s in feed.items() if s["vacant"]]
        for v in vacant_list:
            s = feed[v]
            print(f"{v} -> distance={s['distance']}m, congestion={s['congestion']} ({s['congestion_level']})")
        print("------------------------------------------\n")

    def capture_next_frame(self):
        """Always returns a valid numpy frame (live webcam or synthetic canvas). Updates last_frame_timestamp on every read."""
        if self.use_live_webcam and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret and frame is not None:
                self.last_frame_timestamp = time.monotonic()
                self.consecutive_failures = 0
                self.reconnect_attempts = 0
                frame = cv2.resize(frame, (600, 520))
                self.process_camera_frame(frame)
                return self.draw_overlays(frame, is_live=True)
            else:
                self.consecutive_failures += 1
                print(f"[WARN] Camera read failure ({self.consecutive_failures}).")
                if self.consecutive_failures >= 3:
                    self.auto_reconnect()

        # Synthetic dark canvas fallback
        frame = np.zeros((520, 600, 3), dtype=np.uint8)
        frame[:] = (15, 23, 42)

        # Entry Gate marker
        cv2.rectangle(frame, (220, 12), (380, 48), (34, 197, 94), -1)
        cv2.putText(frame, 'ENTRY GATE', (245, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.line(frame, (300, 48), (300, 90), (34, 197, 94), 2)

        self.process_camera_frame(frame)
        return self.draw_overlays(frame, is_live=False)

    def draw_overlays(self, frame, is_live=False):
        for slot_id, data in self.slots.items():
            x, y, w, h = data["bbox"]
            is_occ = self.occupancy_status[slot_id]
            cg_count, cg_level = self.calculate_congestion(slot_id)

            if is_occ:
                bg_color = (30, 20, 25)
                border_color = (68, 68, 239)
                status_str = "OCCUPIED"
                status_color = (68, 68, 239)
            else:
                bg_color = (20, 35, 25)
                border_color = (94, 197, 34)
                status_str = "VACANT"
                status_color = (94, 197, 34)

            if not is_live:
                cv2.rectangle(frame, (x, y), (x + w, y + h), bg_color, -1)

            cv2.rectangle(frame, (x, y), (x + w, y + h), border_color, 2)
            cv2.putText(frame, f"Slot {slot_id}", (x + 8, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
            cv2.putText(frame, status_str, (x + 8, y + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.45, status_color, 1)
            cv2.putText(frame, f"Dist: {data['distance']}m", (x + 8, y + 62), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (200, 200, 200), 1)
            cv2.putText(frame, f"Cong: {cg_count} ({cg_level})", (x + 8, y + 80), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 220, 255), 1)

        # Scanner Line
        self.scan_line_y = (self.scan_line_y + 8) % 520
        cv2.line(frame, (0, self.scan_line_y), (600, self.scan_line_y), (34, 197, 94), 1)

        # Bottom Status Banner
        banner_txt = "REAL-TIME PARKING METRICS STREAM - GATE DISTANCES & LOCAL CONGESTION"
        cv2.rectangle(frame, (0, 485), (600, 520), (15, 23, 42), -1)
        cv2.putText(frame, banner_txt, (15, 508), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
        return frame

if __name__ == "__main__":
    tracker = RealtimeParkingTracker()
    tracker.print_debug_console()
