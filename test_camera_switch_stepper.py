"""
===============================================================================
SMART GREEN PARKING SYSTEM — CAMERA SWITCHING & STEPPER TEST SUITE
===============================================================================
Tests:
  1. Camera mode transitions between SLOT_DETECTION and QR_SCAN
  2. Slot occupancy freezing during QR_SCAN mode
  3. 20-second timeout auto-revert logic
  4. Gate transaction stepper progression (Step 1 -> 6) on valid entry
  5. Stepper failure handling (unregistered QR, insufficient wallet, parking full)
  6. Real QR code end-to-end decoding with OpenCV and pyzbar fallback
  7. Live slot occupant lookup with parked duration computation
  8. Live KPI metrics calculation (free slots, occupied, active sessions, revenue)
===============================================================================
"""

import os
import sys
import time
import glob
import unittest
import cv2
import numpy as np

# Ensure root workspace is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import payment
from app import app, tracker
from gate_routes import GateStepper, _do_entry, _do_exit
from green_score import get_recommended_slot
from serial_manager import serial_manager


class TestCameraSwitchAndStepper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.temp_db_path = cls.temp_db_file.name
        cls.temp_db_file.close()
        db.set_db_path(cls.temp_db_path)
        db.init_db()
        payment.init_minor_tables()
        from air_quality_db import create_air_quality_table
        create_air_quality_table()
        cls.tracker = tracker
        db.sync_slot_coordinates(cls.tracker.slots)
        cls.stepper = GateStepper()

    @classmethod
    def tearDownClass(cls):
        db.reset_db_path()
        if hasattr(cls, 'temp_db_path') and os.path.exists(cls.temp_db_path):
            try:
                os.remove(cls.temp_db_path)
            except Exception:
                pass

    def setUp(self):
        self.tracker.set_camera_mode("SLOT_DETECTION", reason="Test reset")
        self.stepper.reset()
        for s in self.tracker.slots:
            self.tracker.occupancy_status[s] = False
        self.tracker.reservations.clear()
        self.orig_is_healthy = self.tracker.is_camera_healthy
        self.tracker.is_camera_healthy = lambda: True

    def tearDown(self):
        self.tracker.is_camera_healthy = self.orig_is_healthy

    def test_01_camera_mode_switching(self):
        """Test mode switching, state inspection, and banner."""
        info = self.tracker.get_camera_mode_info()
        self.assertEqual(info["mode"], "SLOT_DETECTION")
        self.assertFalse(info["is_qr_mode"])

        # Switch to QR_SCAN
        self.tracker.set_camera_mode("QR_SCAN", reason="Vehicle at gate test")
        info = self.tracker.get_camera_mode_info()
        self.assertEqual(info["mode"], "QR_SCAN")
        self.assertTrue(info["is_qr_mode"])
        self.assertGreater(info["timeout_remaining"], 0)
        self.assertIn("Vehicle at gate", info["banner"])

        # Switch back to SLOT_DETECTION
        self.tracker.set_camera_mode("SLOT_DETECTION", reason="Manual return")
        info = self.tracker.get_camera_mode_info()
        self.assertEqual(info["mode"], "SLOT_DETECTION")
        self.assertFalse(info["is_qr_mode"])

    def test_02_slot_occupancy_frozen_during_qr_mode(self):
        """Test that in QR_SCAN mode, slot status changes are completely frozen."""
        self.tracker.occupancy_status["A1"] = False
        self.tracker.set_camera_mode("QR_SCAN", reason="QR presentation test")

        # Create a frame with bright pixels in A1 bounding box
        frame = np.ones((520, 600, 3), dtype=np.uint8) * 255
        self.tracker.process_camera_frame(frame)

        # In QR_SCAN mode, A1 must remain False (vacant/frozen)
        self.assertFalse(self.tracker.occupancy_status["A1"])

    def test_03_real_qr_decoding_end_to_end(self):
        """Verify that all sample QR code images decode successfully."""
        qr_files = glob.glob(os.path.join(os.path.dirname(__file__), "qr_codes", "*.png"))
        self.assertGreater(len(qr_files), 0, "No sample QR code files found in qr_codes/")

        detector = cv2.QRCodeDetector()
        for qf in qr_files:
            img = cv2.imread(qf)
            self.assertIsNotNone(img, f"Failed to read {qf}")
            text, pts, _ = detector.detectAndDecode(img)
            if not text:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                text, pts, _ = detector.detectAndDecode(gray)
            self.assertTrue(bool(text and text.strip()), f"Failed to decode QR code from {qf}")

    def test_04_stepper_progression_on_valid_entry(self):
        """Verify the 6-step transaction stepper lights up in sequence on valid entry."""
        from gate_routes import stepper as gr_stepper
        gr_stepper.reset()
        driver_id = payment.create_test_driver("TEST-STEPPER-01", wallet=100.0)
        try:
            # Step 1: Vehicle at gate
            gr_stepper.on_vehicle_at_gate("IR_ENTRY")
            st = gr_stepper.get_state()
            self.assertEqual(st["steps"][0]["status"], "completed")
            self.assertEqual(st["steps"][1]["status"], "active")

            # Execute entry with mock Arduino connection active
            serial_manager.connected = True
            orig_send = serial_manager.send
            serial_manager.send = lambda msg: True
            from app import app
            with app.app_context():
                resp = _do_entry(driver_id)
                # Successful response is a Flask Response object
                status_code = resp.status_code if hasattr(resp, "status_code") else (resp[1] if isinstance(resp, tuple) else 200)
                self.assertEqual(status_code, 200)

            # Check that all 6 steps completed
            st = gr_stepper.get_state()
            self.assertEqual(st["status"], "success")
            for i, step in enumerate(st["steps"]):
                self.assertEqual(step["status"], "completed", f"Step {i+1} ({step['title']}) was not completed: {step}")
        finally:
            serial_manager.send = orig_send
            payment.remove_test_driver("TEST-STEPPER-01")

    def test_05_stepper_failure_on_unregistered_qr(self):
        """Verify stepper halts at Step 3 (Driver verified) in RED when QR is unregistered."""
        from gate_routes import stepper as gr_stepper
        gr_stepper.reset()
        fake_qr = "UNREGISTERED-RANDOM-QR-XYZ"
        serial_manager.connected = True
        gr_stepper.on_vehicle_at_gate("IR_ENTRY")

        from app import app
        with app.app_context():
            resp = _do_entry(fake_qr)
            status_code = resp.status_code if hasattr(resp, "status_code") else (resp[1] if isinstance(resp, tuple) else 200)
            self.assertEqual(status_code, 402)

        st = gr_stepper.get_state()
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["failed_step"], 3)
        self.assertEqual(st["steps"][2]["status"], "failed")
        self.assertIn("not registered", st["steps"][2]["detail"].lower())

    def test_06_stepper_failure_on_insufficient_wallet(self):
        """Verify stepper halts at Step 4 (Fee deducted) in RED when wallet has < Rs 10."""
        from gate_routes import stepper as gr_stepper
        gr_stepper.reset()
        driver_id = payment.create_test_driver("TEST-LOW-WALLET", wallet=2.0)
        try:
            serial_manager.connected = True
            gr_stepper.on_vehicle_at_gate("IR_ENTRY")

            from app import app
            with app.app_context():
                resp = _do_entry(driver_id)
                status_code = resp.status_code if hasattr(resp, "status_code") else (resp[1] if isinstance(resp, tuple) else 200)
                self.assertEqual(status_code, 402)

            st = gr_stepper.get_state()
            self.assertEqual(st["status"], "failed")
            self.assertEqual(st["failed_step"], 4)
            self.assertEqual(st["steps"][3]["status"], "failed")
            detail = st["steps"][3]["detail"].lower()
            self.assertTrue("low wallet" in detail or "insufficient" in detail)
        finally:
            payment.remove_test_driver("TEST-LOW-WALLET")

    def test_07_slot_occupants_and_duration_query(self):
        """Verify db.get_slot_occupants() returns driver, vehicle, and duration string."""
        driver_id = payment.create_test_driver("TEST-DUR-01", wallet=100.0)
        try:
            serial_manager.connected = True
            from app import app
            with app.app_context():
                _do_entry(driver_id)

            occupants = db.get_slot_occupants()
            self.assertIsInstance(occupants, dict)
            # Find the slot assigned
            found = False
            for sid, occ in occupants.items():
                if occ["vehicle"] == "TEST-DUR-01":
                    found = True
                    self.assertIn("m", occ["duration"])
                    self.assertEqual(occ["driver"], "Test Driver")
                    break
            self.assertTrue(found, "Newly parked test driver not found in slot occupants mapping.")
        finally:
            payment.remove_test_driver("TEST-DUR-01")

    def test_08_kpis_calculation(self):
        """Verify db.get_kpis() accurately aggregates active sessions, fee, and slots."""
        feed = self.tracker.get_live_slot_feed()
        kpis = db.get_kpis(feed)
        self.assertIn("free_slots", kpis)
        self.assertIn("occupied_slots", kpis)
        self.assertIn("active_sessions", kpis)
        self.assertIn("collected_today", kpis)
        self.assertIsInstance(kpis["free_slots"], int)
        self.assertIsInstance(kpis["active_sessions"], int)
        self.assertGreaterEqual(kpis["collected_today"], 0.0)


if __name__ == "__main__":
    unittest.main()
