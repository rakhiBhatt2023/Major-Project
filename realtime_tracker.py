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

        # Baseline calibration tracking & prompt state
        self.baseline_calibrated_at = 0.0
        self.baseline_mean_intensity = 0.0

        # Hysteresis thresholds per slot:
        # free_threshold: metric must drop BELOW this to become FREE
        # occupied_threshold: metric must EXCEED this to become OCCUPIED
        # Deadband between the two eliminates noise flicker
        self.thresholds = {
            slot_id: {
                "free_threshold": 1100,
                "occupied_threshold": 1850,
                "mean_diff_free": 13.0,
                "mean_diff_occ": 22.0
            }
            for slot_id in self.slots
        }

        # Temporal debounce: state change must hold continuously for ~2.0 seconds
        self.DEBOUNCE_HOLD_SECONDS = 2.0
        self.slot_debounce = {
            k: {"pending_state": False, "pending_since": 0.0}
            for k in self.slots
        }
        self.slot_metrics = {
            k: {
                "edge_count": 0,
                "diff_count": 0,
                "mean_diff": 0.0,
                "motion_count": 0,
                "raw_metric": 0,
                "free_threshold": 1100,
                "occupied_threshold": 1850,
                "state": "FREE"
            }
            for k in self.slots
        }

        # Single camera debounce & QR freeze protection
        self.qr_freeze_until = 0.0           # Monotonic timestamp until which slot status is frozen
        self.stable_frames_required = 5

        # Camera Mode State (SLOT_DETECTION vs QR_SCAN)
        self.camera_mode = "SLOT_DETECTION"
        self.last_mode_change = time.time()
        self.mode_reason = "System initialized in Slot Detection mode"
        self.qr_mode_timeout_at = 0.0
        self.last_qr_detected = ""
        self.qr_banner = "Vehicle at gate - show your QR code"

        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)

        self.cap = None
        self.use_live_webcam = False
        self.active_camera_index = -1
        self.scan_line_y = 0
        self.last_mean_brightness = 120.0

        self.init_camera()

    def set_camera_mode(self, mode: str, reason: str = None) -> str:
        """Switches the camera operational mode between SLOT_DETECTION and QR_SCAN."""
        valid_modes = ("SLOT_DETECTION", "QR_SCAN")
        if mode not in valid_modes:
            mode = "SLOT_DETECTION"
        old_mode = self.camera_mode
        self.camera_mode = mode
        self.last_mode_change = time.time()
        if reason:
            self.mode_reason = reason
        elif mode == "QR_SCAN":
            self.mode_reason = "Switched to QR scanning"
        else:
            self.mode_reason = "Switched to slot detection"

        if mode == "QR_SCAN":
            # Freeze slot status while in QR mode so phone/hand cannot manipulate slots!
            self.freeze_occupancy_for_qr(30.0)
            self.qr_mode_timeout_at = time.time() + 20.0
        else:
            # Extra 2.0s freeze when leaving QR mode to prevent exit artifact
            self.freeze_occupancy_for_qr(2.0)
            self.qr_mode_timeout_at = 0.0

        print(f"[CAMERA MODE] Switched from {old_mode} to {mode} ({self.mode_reason})")
        return self.camera_mode

    def get_camera_mode_info(self) -> dict:
        """Returns structured metadata about the current camera mode for UI and API."""
        from datetime import datetime
        remaining = max(0.0, round(self.qr_mode_timeout_at - time.time(), 1)) if self.camera_mode == "QR_SCAN" else 0.0
        return {
            "mode": self.camera_mode,
            "last_mode_change": datetime.fromtimestamp(self.last_mode_change).strftime("%H:%M:%S"),
            "last_mode_change_iso": datetime.fromtimestamp(self.last_mode_change).isoformat(),
            "reason": self.mode_reason,
            "is_qr_mode": (self.camera_mode == "QR_SCAN"),
            "banner": self.qr_banner,
            "timeout_remaining": remaining
        }

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

    def reserve_slot(self, slot_id: str, tx_id: str, qr: str, duration: float = 60.0, demo_mode: bool = False) -> bool:
        """Temporarily reserves a vacant slot for an active transaction."""
        self.cleanup_expired_reservations()
        if not self.is_camera_healthy() and not demo_mode:
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

    def _lock_camera_hardware(self, enable_lock: bool = False):
        """Ensures camera is never dark. By default restores and leaves hardware in auto mode.
        If enable_lock is requested, lets auto-exposure settle for ~2s, attempts lock,
        and immediately reverts to auto mode if brightness drops below 60 or call fails."""
        if not self.cap or not self.cap.isOpened():
            return

        # 1. Restore auto exposure and auto white balance (ensuring camera is not left dark)
        try:
            # 0.75 enables auto-exposure in OpenCV DirectShow (CAP_DSHOW)
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            self.cap.set(cv2.CAP_PROP_AUTO_WB, 1.0)
            self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1.0)
        except Exception:
            pass

        if not enable_lock:
            print("[CAMERA] Hardware auto-controls active (auto-exposure, auto-wb enabled).")
            return

        # 2. Exposure lock attempt with brightness safety check
        try:
            time.sleep(2.0)
            for _ in range(5):
                self.cap.read()
            cur_exp = self.cap.get(cv2.CAP_PROP_EXPOSURE)
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            if cur_exp is not None and cur_exp > -99:
                self.cap.set(cv2.CAP_PROP_EXPOSURE, cur_exp)
            time.sleep(0.5)
            for _ in range(3):
                self.cap.read()
            ret, frame = self.cap.read()
            if ret and frame is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                mean_b = float(np.mean(gray))
                if mean_b < 60.0:
                    print(f"[CAMERA] Exposure lock dropped brightness to {mean_b:.1f} (<60). Undoing lock and staying in auto mode.")
                    self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
                else:
                    print(f"[CAMERA] Exposure lock succeeded at exposure {cur_exp} (mean brightness: {mean_b:.1f}).")
            else:
                self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
        except Exception as e:
            try:
                self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            except Exception:
                pass
            print(f"[CAMERA] Exposure lock error ({e}); reverted to auto mode.")

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
                if slot_id in self.slot_debounce:
                    self.slot_debounce[slot_id]["pending_state"] = False
                    self.slot_debounce[slot_id]["pending_since"] = 0.0

        self.baseline_calibrated_at = time.time()
        self.baseline_mean_intensity = float(np.mean(gray))
        print(f"[CALIBRATION] Baseline empty slot reference saved for slots: {list(self.slots.keys())}")
        return True

    def get_calibration_info(self) -> dict:
        """Returns baseline calibration status and whether recalibration is needed."""
        from datetime import datetime
        has_baseline = len(self.baseline_rois) == len(self.slots)
        now = time.time()
        age = (now - self.baseline_calibrated_at) if self.baseline_calibrated_at > 0 else 999999.0
        is_stale = (not has_baseline) or (age > 3600.0)

        if not has_baseline:
            prompt = "Baseline empty slots reference not calibrated. Please calibrate empty slots."
        elif age > 3600.0:
            prompt = f"Baseline calibration is stale ({int(age // 60)}m old). Recalibration recommended."
        else:
            prompt = "Baseline calibration is fresh and active."

        return {
            "calibrated": has_baseline,
            "calibrated_at": self.baseline_calibrated_at,
            "calibrated_at_str": datetime.fromtimestamp(self.baseline_calibrated_at).strftime("%H:%M:%S") if self.baseline_calibrated_at > 0 else "Never",
            "age_seconds": round(age, 1),
            "is_stale": is_stale,
            "prompt": prompt
        }

    def get_camera_hardware_info(self) -> dict:
        """Returns live camera brightness and hardware control values for /admin."""
        mean_b = round(getattr(self, "last_mean_brightness", 0.0), 1)
        cap_opened = bool(self.cap and self.cap.isOpened())
        b_val = None
        exp_val = None
        auto_exp_val = None
        if cap_opened:
            try:
                b_val = self.cap.get(cv2.CAP_PROP_BRIGHTNESS)
            except Exception:
                pass
            try:
                exp_val = self.cap.get(cv2.CAP_PROP_EXPOSURE)
            except Exception:
                pass
            try:
                auto_exp_val = self.cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            except Exception:
                pass
        return {
            "mean_brightness": mean_b,
            "is_too_dark": mean_b < 50.0,
            "camera_opened": cap_opened,
            "device_index": self.active_camera_index,
            "backend": getattr(self, "active_backend_name", "UNKNOWN"),
            "brightness": b_val,
            "exposure": exp_val,
            "auto_exposure": auto_exp_val,
        }

    def set_camera_hardware(self, brightness=None, exposure=None, auto_exposure=None):
        """Sets hardware brightness, exposure, or auto-exposure mode."""
        if not self.cap or not self.cap.isOpened():
            return False, "Camera is not connected"
        applied = {}
        if auto_exposure is not None:
            val = 0.75 if auto_exposure else 0.25
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, val)
            applied["auto_exposure"] = "auto (0.75)" if auto_exposure else "manual (0.25)"
        if brightness is not None:
            try:
                b_val = float(brightness)
                self.cap.set(cv2.CAP_PROP_BRIGHTNESS, b_val)
                applied["brightness"] = b_val
            except Exception as e:
                return False, f"Failed setting brightness: {e}"
        if exposure is not None:
            try:
                exp_val = float(exposure)
                self.cap.set(cv2.CAP_PROP_EXPOSURE, exp_val)
                applied["exposure"] = exp_val
            except Exception as e:
                return False, f"Failed setting exposure: {e}"
        # Flush a couple frames so new settings take effect
        for _ in range(3):
            ret, frame = self.cap.read()
            if ret and frame is not None:
                self.last_mean_brightness = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
        return True, applied

    def get_slot_diagnostics(self) -> dict:
        """Returns per-slot live detection metrics vs thresholds and calibration status for /admin."""
        calib = self.get_calibration_info()
        diag = {}
        now = time.time()
        for sid in self.slots:
            th = self.thresholds.get(sid, {"free_threshold": 1100, "occupied_threshold": 1850})
            m = self.slot_metrics.get(sid, {})
            has_base = (sid in self.baseline_rois) and (self.baseline_calibrated_at > 0)
            base_age = round(now - self.baseline_calibrated_at, 1) if has_base else None
            status_val = "NOT CALIBRATED" if (not has_base and self.use_live_webcam) else ("OCCUPIED" if self.occupancy_status.get(sid, False) else "FREE")
            diag[sid] = {
                "slot_id": sid,
                "status": status_val,
                "vacant": (status_val == "FREE"),
                "baseline_present": has_base,
                "baseline_age_seconds": base_age,
                "metrics": m,
                "diff_metric": m.get("diff_count", 0),
                "trigger_condition": m.get("trigger_condition", "NONE"),
                "free_threshold": th.get("free_threshold", 1100),
                "occupied_threshold": th.get("occupied_threshold", 1850),
            }
        mean_b = round(getattr(self, "last_mean_brightness", 0.0), 1)
        return {
            "calibration": calib,
            "slots": diag,
            "mean_brightness": mean_b,
            "is_too_dark": mean_b < 50.0,
            "hardware": self.get_camera_hardware_info()
        }

    def set_slot_thresholds(self, slot_id: str, free_th: int = None, occ_th: int = None) -> bool:
        """Dynamically tunes per-slot hysteresis thresholds from /admin."""
        if slot_id in self.thresholds:
            if free_th is not None:
                self.thresholds[slot_id]["free_threshold"] = int(free_th)
            if occ_th is not None:
                self.thresholds[slot_id]["occupied_threshold"] = int(occ_th)
            return True
        return False

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
                        # Ensure auto controls are active (do not lock hardware)
                        self._lock_camera_hardware(enable_lock=False)
                        # Warm up for a few frames to let auto-exposure settle naturally
                        for _ in range(8):
                            ret_w, f_w = self.cap.read()
                            if ret_w and f_w is not None:
                                frame = f_w
                        if frame is not None:
                            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                            self.last_mean_brightness = float(np.mean(gray))
                            print(f"[CAMERA] Frame mean brightness settled at: {self.last_mean_brightness:.1f}")
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

    def freeze_occupancy_for_qr(self, duration: float = 2.0):
        """Freezes slot status during QR scan and for ~2s after, preventing phone/hand from flipping slots."""
        self.qr_freeze_until = max(self.qr_freeze_until, time.monotonic() + duration)
        self.last_detection_timestamp = time.monotonic()

    # PART 2 & PART 4: Process Camera Frame & Compute ROI Occupancy
    def process_camera_frame(self, frame, threshold_pixels=1600):
        if frame is None:
            return self.occupancy_status

        now_mono = time.monotonic()

        # In QR_SCAN mode, or during freeze period, slot status transitions are strictly frozen
        if self.camera_mode == "QR_SCAN" or now_mono < self.qr_freeze_until:
            self.last_detection_timestamp = now_mono
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

                th = self.thresholds.get(slot_id, {
                    "free_threshold": 1100,
                    "occupied_threshold": 1850,
                    "mean_diff_free": 13.0,
                    "mean_diff_occ": 22.0
                })
                free_th = th.get("free_threshold", 1100)
                occ_th = th.get("occupied_threshold", 1850)
                mean_free_th = th.get("mean_diff_free", 13.0)
                mean_occ_th = th.get("mean_diff_occ", 22.0)

                current_status = self.occupancy_status[slot_id]

                has_slot_baseline = (slot_id in self.baseline_rois) and (self.baseline_calibrated_at > 0)

                if has_slot_baseline:
                    base_roi = self.baseline_rois[slot_id]
                    diff = cv2.absdiff(roi_blur, base_roi)
                    diff_count = int(np.count_nonzero(diff > 35))
                    mean_diff = float(np.mean(diff))
                    raw_metric = diff_count

                    # HYSTERESIS DETECTION LOGIC:
                    if not current_status:
                        # Currently FREE: requires crossing high threshold to become OCCUPIED
                        is_occupied_instant = (diff_count > occ_th) or (mean_diff > mean_occ_th)
                    else:
                        # Currently OCCUPIED: remains OCCUPIED unless it drops BELOW low threshold
                        is_occupied_instant = not ((diff_count < free_th) and (mean_diff < mean_free_th))

                    if is_occupied_instant:
                        if diff_count > occ_th:
                            trigger_condition = f"BASELINE_DIFF_COUNT ({diff_count} > {occ_th})"
                        elif mean_diff > mean_occ_th:
                            trigger_condition = f"BASELINE_MEAN_DIFF ({mean_diff:.1f} > {mean_occ_th})"
                        else:
                            trigger_condition = f"HYSTERESIS_HOLD (diff {diff_count} >= free_th {free_th})"
                    else:
                        trigger_condition = f"BELOW_THRESHOLDS (diff {diff_count} < free_th {free_th})"
                    slot_state = "OCCUPIED" if current_status else "FREE"
                else:
                    raw_metric = 0
                    diff_count = 0
                    mean_diff = 0.0
                    # STRICT: Do NOT use fallback motion/edge detector in busy room background!
                    is_occupied_instant = False
                    trigger_condition = "NOT_CALIBRATED (Awaiting baseline calibration)"
                    slot_state = "NOT CALIBRATED"

                # Store live metric diagnostics for /admin per-slot tuning
                self.slot_metrics[slot_id] = {
                    "edge_count": edge_count,
                    "diff_count": diff_count,
                    "mean_diff": round(mean_diff, 1),
                    "motion_count": motion_count,
                    "raw_metric": raw_metric,
                    "free_threshold": free_th,
                    "occupied_threshold": occ_th,
                    "state": slot_state,
                    "baseline_present": has_slot_baseline,
                    "trigger_condition": trigger_condition
                }

                # TEMPORAL DEBOUNCE (~2.0 seconds hold requirement):
                if self.use_live_webcam:
                    # Do not flip state if slot baseline is not calibrated
                    if not has_slot_baseline:
                        continue

                    # If QR code was detected recently, freeze all slot status transitions
                    if now_mono < self.qr_freeze_until:
                        continue

                    deb = self.slot_debounce.get(slot_id, {"pending_state": current_status, "pending_since": 0.0})
                    if is_occupied_instant == current_status:
                        # No state change requested: reset pending hold
                        deb["pending_state"] = current_status
                        deb["pending_since"] = 0.0
                    else:
                        # State change requested: must hold for DEBOUNCE_HOLD_SECONDS
                        if deb["pending_state"] != is_occupied_instant or deb["pending_since"] == 0.0:
                            deb["pending_state"] = is_occupied_instant
                            deb["pending_since"] = now_mono
                        else:
                            hold_time = now_mono - deb["pending_since"]
                            if hold_time >= self.DEBOUNCE_HOLD_SECONDS:
                                self.occupancy_status[slot_id] = is_occupied_instant
                                deb["pending_since"] = 0.0
                                print(f"[SLOT STATE CHANGE] Slot {slot_id} transitioned to {'OCCUPIED' if is_occupied_instant else 'FREE'} (held for {hold_time:.1f}s)")
                    self.slot_debounce[slot_id] = deb

        self.last_detection_timestamp = now_mono
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
    def get_live_slot_feed(self, demo_mode: bool = False) -> dict:
        """Returns real-time data for all slots with one source of truth:
        FREE (green), ASSIGNED (amber - waiting for car), OCCUPIED (red - camera confirmed or assigned).
        """
        self.cleanup_expired_reservations()
        healthy = self.is_camera_healthy() or demo_mode
        feed = {}

        # Look up active slot occupants from DB to ensure single source of truth
        occupants = {}
        try:
            import db
            occupants = db.get_slot_occupants()
        except Exception:
            pass

        has_baseline = (len(self.baseline_rois) == len(self.slots)) and (self.baseline_calibrated_at > 0)

        for slot_id, data in self.slots.items():
            is_camera_occ = bool(self.occupancy_status.get(slot_id, False))
            is_res = slot_id in self.reservations
            occupant = occupants.get(slot_id, None)
            has_active_session = (occupant is not None)

            if not healthy:
                status = "UNKNOWN"
                vacant = False
                reserved = False
                unknown = True
            elif not has_baseline and self.use_live_webcam and not demo_mode:
                status = "NOT CALIBRATED"
                vacant = False
                reserved = False
                unknown = False
            elif is_camera_occ:
                # Camera confirmed physical presence of car -> OCCUPIED (Red)
                status = "OCCUPIED"
                vacant = False
                reserved = False
                unknown = False
            elif has_active_session or is_res:
                # Active session in DB or active reservation, waiting for car -> ASSIGNED (Amber)
                status = "ASSIGNED"
                vacant = False
                reserved = True
                unknown = False
            else:
                # Slot is free (Green)
                status = "FREE"
                vacant = True
                reserved = False
                unknown = False

            cg_count, cg_level = self.calculate_congestion(slot_id)

            th = self.thresholds.get(slot_id, {"free_threshold": 1100, "occupied_threshold": 1850})
            slot_diag = self.slot_metrics.get(slot_id, {})
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
                "raw_metric": slot_diag.get("raw_metric", self.roi_pixel_counts.get(slot_id, 0)),
                "free_threshold": th.get("free_threshold", 1100),
                "occupied_threshold": th.get("occupied_threshold", 1850),
                "metric": slot_diag,
                "camera_active": healthy,
                "camera_index": self.active_camera_index,
                "last_frame_timestamp": self.last_frame_timestamp,
                "detection_fresh": healthy,
                "occupant": occupant
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
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                self.last_mean_brightness = float(np.mean(gray))
                if self.camera_mode == "SLOT_DETECTION":
                    self.process_camera_frame(frame)
                else:
                    # In QR_SCAN mode, update detection timestamp so camera health stays fresh
                    self.last_detection_timestamp = time.monotonic()
                return self.draw_overlays(frame, is_live=True)
            else:
                self.consecutive_failures += 1
                print(f"[WARN] Camera read failure ({self.consecutive_failures}).")
                if self.consecutive_failures >= 3:
                    self.auto_reconnect()

        # Synthetic dark canvas fallback
        frame = np.zeros((520, 600, 3), dtype=np.uint8)
        frame[:] = (15, 23, 42)

        if self.camera_mode == "SLOT_DETECTION":
            # Entry Gate marker
            cv2.rectangle(frame, (220, 12), (380, 48), (34, 197, 94), -1)
            cv2.putText(frame, 'ENTRY GATE', (245, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
            cv2.line(frame, (300, 48), (300, 90), (34, 197, 94), 2)
            self.process_camera_frame(frame)

        return self.draw_overlays(frame, is_live=False)

    def draw_overlays(self, frame, is_live=False):
        if self.camera_mode == "QR_SCAN":
            # ─────────────────────────────────────────────────────────────
            # QR SCANNING OVERLAY VIEW
            # ─────────────────────────────────────────────────────────────
            # Top Banner Bar
            cv2.rectangle(frame, (0, 0), (600, 44), (15, 23, 42), -1)
            cv2.putText(frame, "MODE: QR SCAN (ACTIVE)", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (34, 197, 94), 2)
            cv2.putText(frame, "FREEZE: SLOTS LOCKED", (410, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (245, 158, 11), 1)

            # Viewfinder Reticle (280x280 box centered)
            rx, ry, rw, rh = 160, 90, 280, 280
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), (51, 65, 85), 1)

            # L-shaped Corner Brackets in vivid green
            blen = 32
            # Top-Left
            cv2.line(frame, (rx, ry), (rx + blen, ry), (34, 197, 94), 3)
            cv2.line(frame, (rx, ry), (rx, ry + blen), (34, 197, 94), 3)
            # Top-Right
            cv2.line(frame, (rx + rw, ry), (rx + rw - blen, ry), (34, 197, 94), 3)
            cv2.line(frame, (rx + rw, ry), (rx + rw, ry + blen), (34, 197, 94), 3)
            # Bottom-Left
            cv2.line(frame, (rx, ry + rh), (rx + blen, ry + rh), (34, 197, 94), 3)
            cv2.line(frame, (rx, ry + rh), (rx, ry + rh - blen), (34, 197, 94), 3)
            # Bottom-Right
            cv2.line(frame, (rx + rw, ry + rh), (rx + rw - blen, ry + rh), (34, 197, 94), 3)
            cv2.line(frame, (rx + rw, ry + rh), (rx + rw, ry + rh - blen), (34, 197, 94), 3)

            # Center Crosshair
            cx, cy = rx + rw // 2, ry + rh // 2
            cv2.line(frame, (cx - 16, cy), (cx + 16, cy), (34, 197, 94), 1)
            cv2.line(frame, (cx, cy - 16), (cx, cy + 16), (34, 197, 94), 1)

            # Helper instruction
            cv2.putText(frame, "HOLD QR CODE IN RETICLE", (195, ry + rh + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (148, 163, 184), 1)

            # Sweeping Scan Laser
            self.scan_line_y = (self.scan_line_y + 8) % rh
            cv2.line(frame, (rx + 4, ry + self.scan_line_y), (rx + rw - 4, ry + self.scan_line_y), (34, 197, 94), 2)

            # Bottom Status Banner
            cv2.rectangle(frame, (0, 476), (600, 520), (15, 23, 42), -1)
            banner_msg = self.qr_banner or "Vehicle at gate - show your QR code"
            cv2.putText(frame, banner_msg, (16, 504), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (226, 232, 240), 1)
            return frame

        # ─────────────────────────────────────────────────────────────
        # SLOT DETECTION OVERLAY VIEW (Default)
        # ─────────────────────────────────────────────────────────────
        # Top Banner Bar
        cv2.rectangle(frame, (0, 0), (600, 44), (15, 23, 42), -1)
        cv2.putText(frame, "MODE: SLOT DETECTION (ACTIVE)", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (34, 197, 94), 2)

        # Look up active occupants from DB for single source of truth overlay
        occupants = {}
        try:
            import db
            occupants = db.get_slot_occupants()
        except Exception:
            pass

        has_baseline = (len(self.baseline_rois) == len(self.slots)) and (self.baseline_calibrated_at > 0)

        for slot_id, data in self.slots.items():
            x, y, w, h = data["bbox"]
            is_occ = self.occupancy_status[slot_id]
            occ_info = occupants.get(slot_id)
            has_session = (occ_info is not None)
            cg_count, cg_level = self.calculate_congestion(slot_id)

            if not has_baseline and is_live:
                bg_color = (20, 25, 35)
                border_color = (0, 165, 255)   # Amber / Orange
                status_str = "NOT CALIBRATED"
                status_color = (0, 165, 255)
            elif is_occ:
                bg_color = (30, 20, 25)
                border_color = (68, 68, 239)   # Red
                status_str = "OCCUPIED"
                status_color = (68, 68, 239)
            elif has_session:
                bg_color = (20, 25, 35)
                border_color = (11, 158, 245)  # Amber
                status_str = "ASSIGNED"
                status_color = (11, 158, 245)
            else:
                bg_color = (20, 35, 25)
                border_color = (94, 197, 34)   # Green
                status_str = "FREE"
                status_color = (94, 197, 34)

            if not is_live:
                cv2.rectangle(frame, (x, y), (x + w, y + h), bg_color, -1)

            cv2.rectangle(frame, (x, y), (x + w, y + h), border_color, 2)
            cv2.putText(frame, f"Slot {slot_id}", (x + 8, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
            cv2.putText(frame, status_str, (x + 8, y + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.45, status_color, 1)
            if occ_info:
                d_name = (occ_info.get("driver") or "")[:10]
                cv2.putText(frame, d_name, (x + 8, y + 60), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (255, 255, 255), 1)
            else:
                cv2.putText(frame, f"Dist: {data['distance']}m", (x + 8, y + 60), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (200, 200, 200), 1)
            cv2.putText(frame, f"Cong: {cg_count} ({cg_level})", (x + 8, y + 78), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 220, 255), 1)

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
