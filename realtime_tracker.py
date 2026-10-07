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
import numpy as np

class RealtimeParkingTracker:
    def __init__(self):
        # PART 1: Configurable Parking Slot Distance Mapping (meters from entry gate)
        self.slots = {
            "A1": {"bbox": (60,  100, 130, 95), "distance": 5.0},
            "A2": {"bbox": (220, 100, 130, 95), "distance": 7.0},
            "A3": {"bbox": (380, 100, 130, 95), "distance": 9.0},
            "B1": {"bbox": (60,  240, 130, 95), "distance": 8.0},
            "B2": {"bbox": (220, 240, 130, 95), "distance": 10.0},
            "B3": {"bbox": (380, 240, 130, 95), "distance": 12.0},
            "C1": {"bbox": (60,  380, 130, 95), "distance": 11.0},
            "C2": {"bbox": (220, 380, 130, 95), "distance": 13.0},
            "C3": {"bbox": (380, 380, 130, 95), "distance": 15.0},
        }

        # PART 3: Configurable Spatial Adjacency Network for Local Congestion
        self.adjacent_network = {
            "A1": ["A2", "B1"],
            "A2": ["A1", "A3", "B2"],
            "A3": ["A2", "B3"],
            "B1": ["A1", "B2", "C1"],
            "B2": ["A2", "B1", "B3", "C2"],
            "B3": ["A3", "B2", "C3"],
            "C1": ["B1", "C2"],
            "C2": ["B2", "C1", "C3"],
            "C3": ["B3", "C2"],
        }

        # PART 2 & PART 9: Initial Slot Occupancy Status (Test Mode Default)
        self.occupancy_status = {
            "A1": True,  "A2": False, "A3": False,
            "B1": True,  "B2": False, "B3": True,
            "C1": False, "C2": False, "C3": True
        }
        self.roi_pixel_counts = {k: 0 for k in self.slots}

        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)

        self.cap = None
        self.use_live_webcam = False
        self.active_camera_index = -1
        self.scan_line_y = 0

        self.init_camera()

    def init_camera(self):
        """Try to open the webcam. Fast path: DirectShow on index 0 (Windows)."""
        if self.cap and self.cap.isOpened():
            self.cap.release()

        # Try the fastest combination first, then fallback
        attempts = [
            (0, cv2.CAP_DSHOW),
            (0, cv2.CAP_MSMF),
            (0, cv2.CAP_ANY),
            (1, cv2.CAP_DSHOW),
            (1, cv2.CAP_ANY),
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
                        print(f'[SUCCESS] Live Camera Connected on Device {idx}.')
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

    # PART 2 & PART 4: Process Camera Frame & Compute ROI Density
    def process_camera_frame(self, frame, threshold_pixels=1600):
        if frame is None:
            return self.occupancy_status

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        thresh = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                       cv2.THRESH_BINARY_INV, 25, 16)
        fg_mask = self.bg_subtractor.apply(blur)

        for slot_id, data in self.slots.items():
            x, y, w, h = data["bbox"]
            if y + h <= thresh.shape[0] and x + w <= thresh.shape[1]:
                roi_thresh = thresh[y:y+h, x:x+w]
                roi_fg = fg_mask[y:y+h, x:x+w]
                
                pixel_count = cv2.countNonZero(roi_thresh)
                motion_count = cv2.countNonZero(roi_fg)
                
                total_density = pixel_count + (motion_count * 2)
                self.roi_pixel_counts[slot_id] = total_density

                if self.use_live_webcam:
                    self.occupancy_status[slot_id] = (total_density > threshold_pixels)

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

    # Live slot data payload — feeds distance + status + congestion to the scoring module
    def get_live_slot_feed(self) -> dict:
        """Returns real-time data for all slots. Used by /api/slots endpoint."""
        feed = {}
        for slot_id, data in self.slots.items():
            is_occ = self.occupancy_status[slot_id]
            cg_count, cg_level = self.calculate_congestion(slot_id)

            feed[slot_id] = {
                "slot_id": slot_id,
                "distance": data["distance"],
                "status": "OCCUPIED" if is_occ else "VACANT",
                "vacant": not is_occ,
                "congestion": cg_count,
                "congestion_level": cg_level,
                "pixel_density": self.roi_pixel_counts.get(slot_id, 0),
                "camera_active": self.use_live_webcam,
                "camera_index": self.active_camera_index
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
        """Always returns a valid numpy frame (live webcam or synthetic canvas)."""
        if self.use_live_webcam and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret and frame is not None:
                frame = cv2.resize(frame, (600, 520))
                self.process_camera_frame(frame)
                return self.draw_overlays(frame, is_live=True)
            else:
                # Frame grab failed — mark camera as disconnected
                print('[WARN] Camera frame read failed, switching to synthetic view.')
                self.use_live_webcam = False

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
