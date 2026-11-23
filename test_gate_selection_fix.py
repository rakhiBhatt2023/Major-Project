"""
===============================================================================
SMART PARK AI — GATE SELECTION & QR SCANNING VERIFICATION TEST SUITE
===============================================================================
Tests:
  1. Manual Entry Gate selection sets ENTRY and persists
  2. Manual Exit Gate selection sets EXIT and persists
  3. Arduino IR_EXIT event does NOT override manual ENTRY selection (Auto Mode OFF)
  4. Arduino IR_EXIT event respects 30-second manual guard even if Auto Mode is ON
  5. API endpoints (/api/system/status, /api/gate/direction, /api/frame) do not clobber gate selection
  6. Independent QR scanning in ENTRY mode (creates session, allocates slot, opens barrier)
  7. Independent QR scanning in EXIT mode (verifies active session, calculates fee, releases slot)
  8. Idempotency & active session verification in ENTRY & EXIT modes
===============================================================================
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import payment
from app import app, tracker, _on_arduino_ir_entry, _on_arduino_ir_exit
from gate_routes import (
    stepper,
    set_gate_direction,
    get_gate_direction,
    set_auto_gate_mode,
    get_auto_gate_mode,
    is_manual_override_active,
    handle_ir_event,
    _do_entry,
    _do_exit,
)
from serial_manager import serial_manager


class TestGateSelectionFix(unittest.TestCase):
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
        cls.client = app.test_client()

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
        self.orig_is_healthy = self.tracker.is_camera_healthy
        self.tracker.is_camera_healthy = lambda: True
        self.orig_connected = serial_manager.connected
        self.orig_send = serial_manager.send
        serial_manager.connected = True
        serial_manager.send = lambda msg: True
        set_auto_gate_mode(False)
        set_gate_direction("ENTRY", manual=True)

    def tearDown(self):
        self.tracker.is_camera_healthy = self.orig_is_healthy
        serial_manager.connected = self.orig_connected
        serial_manager.send = self.orig_send

    def test_01_manual_entry_selection_persists(self):
        """Clicking Entry Gate sets direction to ENTRY and marks manual selection active."""
        res = self.client.post("/api/gate/direction", json={"direction": "ENTRY", "manual": True})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["direction"], "ENTRY")
        self.assertTrue(data["manual_active"])
        self.assertEqual(get_gate_direction(), "ENTRY")
        self.assertTrue(is_manual_override_active())

    def test_02_manual_exit_selection_persists(self):
        """Clicking Exit Gate sets direction to EXIT and marks manual selection active."""
        res = self.client.post("/api/gate/direction", json={"direction": "EXIT", "manual": True})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["direction"], "EXIT")
        self.assertTrue(data["manual_active"])
        self.assertEqual(get_gate_direction(), "EXIT")

    def test_03_arduino_ir_exit_does_not_override_manual_entry_when_auto_off(self):
        """When user selects ENTRY, repeated Arduino IR_EXIT events MUST NOT switch gate to EXIT."""
        set_gate_direction("ENTRY", manual=True)
        set_auto_gate_mode(False)
        self.assertEqual(get_gate_direction(), "ENTRY")

        # Simulate multiple serial IR_EXIT triggers from Arduino callback
        for _ in range(5):
            _on_arduino_ir_exit()

        # Must still be ENTRY!
        self.assertEqual(get_gate_direction(), "ENTRY")

    def test_04_arduino_ir_exit_respects_manual_guard_when_auto_on(self):
        """Even when Auto Gate mode is ON, manual selection within 30s guards against IR switches."""
        set_gate_direction("ENTRY", manual=True)
        set_auto_gate_mode(True)
        self.assertEqual(get_gate_direction(), "ENTRY")

        # Trigger Arduino IR_EXIT callback
        _on_arduino_ir_exit()

        # Direction must remain ENTRY because manual guard (30s) is active
        self.assertEqual(get_gate_direction(), "ENTRY")

    def test_05_api_polling_does_not_clobber_gate_direction(self):
        """System status and frame polling endpoints must reflect current direction without changing it."""
        set_gate_direction("ENTRY", manual=True)

        # Poll /api/system/status
        res = self.client.get("/api/system/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["gate_direction"], "ENTRY")
        self.assertTrue(data["manual_active"])

        # Poll /api/frame
        frame_res = self.client.get("/api/frame")
        self.assertEqual(frame_res.status_code, 200)
        self.assertEqual(get_gate_direction(), "ENTRY")

        # Poll /api/gate/stepper
        stepper_res = self.client.get("/api/gate/stepper")
        self.assertEqual(stepper_res.status_code, 200)
        self.assertEqual(stepper_res.get_json()["direction"], "ENTRY")
        self.assertEqual(get_gate_direction(), "ENTRY")

    def test_06_qr_scanning_independent_entry_mode(self):
        """QR scanning in ENTRY mode creates active session, allocates slot, and deducts fee."""
        driver_id = payment.create_test_driver("TEST-FIX-ENTRY", wallet=100.0)
        try:
            set_gate_direction("ENTRY", manual=True)

            with app.app_context():
                res = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "ENTRY"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data["success"])
                self.assertEqual(data["action"], "entry")
                self.assertIsNotNone(data.get("recommended_slot", {}).get("slot_id"))

                # Verify active parking session exists in DB
                self.assertTrue(payment.has_active_session(driver_id), "Parking session should be ACTIVE after entry scan")
        finally:
            payment.remove_test_driver("TEST-FIX-ENTRY")

    def test_07_qr_scanning_independent_exit_mode(self):
        """QR scanning in EXIT mode verifies session, calculates fee, releases slot, and opens gate."""
        driver_id = payment.create_test_driver("TEST-FIX-EXIT", wallet=100.0)
        try:
            # First, enter vehicle
            set_gate_direction("ENTRY", manual=True)
            with app.app_context():
                res_entry = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "ENTRY"})
                self.assertEqual(res_entry.status_code, 200)
                entry_data = res_entry.get_json()
                allocated_slot = entry_data.get("recommended_slot", {}).get("slot_id")
                self.assertIsNotNone(allocated_slot)

            # Next, switch to EXIT mode
            set_gate_direction("EXIT", manual=True)
            self.assertEqual(get_gate_direction(), "EXIT")

            # Scan in EXIT mode
            with app.app_context():
                res_exit = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "EXIT"})
                self.assertEqual(res_exit.status_code, 200)
                exit_data = res_exit.get_json()
                self.assertTrue(exit_data["success"])
                self.assertEqual(exit_data["action"], "exit")

                # Verify session is now COMPLETED (no active session)
                self.assertFalse(payment.has_active_session(driver_id), "Parking session should be COMPLETED after exit scan")
        finally:
            payment.remove_test_driver("TEST-FIX-EXIT")

    def test_08_active_session_verification_preserved(self):
        """Exit scan fails if driver is not inside; Entry scan fails if driver is already inside."""
        driver_id = payment.create_test_driver("TEST-FIX-VERIFY", wallet=100.0)
        try:
            # 1. Attempt Exit without entering first -> Must fail
            set_gate_direction("EXIT", manual=True)
            with app.app_context():
                res_exit_early = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "EXIT"})
                self.assertEqual(res_exit_early.status_code, 402)
                data_fail = res_exit_early.get_json()
                self.assertFalse(data_fail["success"])
                self.assertIn("No active entry", data_fail["message"])

            # 2. Enter vehicle
            set_gate_direction("ENTRY", manual=True)
            with app.app_context():
                res_entry = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "ENTRY"})
                self.assertEqual(res_entry.status_code, 200)

            # 3. Attempt Entry again while already parked -> Must fail
            with app.app_context():
                res_entry_again = self.client.post("/api/gate/scan", json={"qr": driver_id, "direction": "ENTRY"})
                self.assertEqual(res_entry_again.status_code, 409)
                data_again = res_entry_again.get_json()
                self.assertFalse(data_again["success"])
                self.assertIn("already parked", data_again["message"].lower())
        finally:
            payment.remove_test_driver("TEST-FIX-VERIFY")


if __name__ == "__main__":
    unittest.main()
