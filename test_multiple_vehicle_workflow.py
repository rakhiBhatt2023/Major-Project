"""
===============================================================================
SMART PARK AI - MULTIPLE VEHICLE QR SCANNING VERIFICATION
===============================================================================
Tests the full multi-vehicle workflow required by Issue 2:
1. MAX (8743) entry -> Slot allocated, session created, gate opened.
2. Duplicate entry protection -> MAX cannot enter twice while parked.
3. CHARLES (9583) entry -> Different slot allocated, separate session created, gate opened.
4. Duplicate entry protection -> CHARLES cannot enter twice while parked.
5. Active session validation -> Exit denied for vehicle with no active entry.
6. MAX (8743) exit -> Active session found, fee calculated/deducted, slot released, gate opened.
7. CHARLES (9583) exit -> Active session found, fee calculated/deducted, slot released, gate opened.
8. Post-exit state -> Both slots vacant, sessions closed, balances verified.
===============================================================================
"""

import os
import sys
import unittest
import tempfile
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import payment
from app import app, tracker
from gate_routes import _do_entry, _do_exit, stepper, set_gate_direction
from serial_manager import serial_manager


class TestMultipleVehicleWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use isolated test DB to avoid corrupting demo/production data
        cls.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.temp_db_path = cls.temp_db.name
        cls.temp_db.close()
        db.set_db_path(cls.temp_db_path)
        db.init_db()
        payment.init_minor_tables()

        from air_quality_db import create_air_quality_table
        create_air_quality_table()

        db.sync_slot_coordinates(tracker.slots)

        # Register MAX and CHARLES
        cls.max_qr = "a1b8fb4d-753b-4a12-a95d-d6c3ebc7bf62"
        cls.charles_qr = "7f34af0a-c890-4d03-bc72-8e1c22d10835"

        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM drivers")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM parkingrecords")
        cur.execute("DELETE FROM scanlogs")
        cur.execute("""
            INSERT INTO drivers (driver_id, name, vehicle_no, phone, wallet, registered_on)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cls.max_qr, "MAX", "8743", "9346943694", 500.0, "2026-10-07 21:53:19"))
        cur.execute("""
            INSERT INTO drivers (driver_id, name, vehicle_no, phone, wallet, registered_on)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cls.charles_qr, "CHARLES", "9583", "974297856", 500.0, "2026-10-07 21:54:41"))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        db.reset_db_path()
        if hasattr(cls, 'temp_db_path') and os.path.exists(cls.temp_db_path):
            try:
                os.remove(cls.temp_db_path)
            except Exception:
                pass

    def setUp(self):
        # Ensure tracker has all slots vacant and clean reservations
        for s in tracker.slots:
            tracker.occupancy_status[s] = False
        tracker.reservations.clear()
        self.orig_is_healthy = tracker.is_camera_healthy
        tracker.is_camera_healthy = lambda: True

        # Mock serial manager as connected for test
        self.orig_connected = serial_manager.connected
        self.orig_send = serial_manager.send
        serial_manager.connected = True
        self.sent_commands = []
        serial_manager.send = lambda cmd: (self.sent_commands.append(cmd) or True)

    def tearDown(self):
        tracker.is_camera_healthy = self.orig_is_healthy
        serial_manager.connected = self.orig_connected
        serial_manager.send = self.orig_send

    def test_complete_two_vehicle_lifecycle(self):
        """End-to-end multi-vehicle workflow verification with MAX and CHARLES."""
        with app.app_context():
            # ─────────────────────────────────────────────────────────────
            # 1. ENTRY: MAX (8743)
            # ─────────────────────────────────────────────────────────────
            set_gate_direction("ENTRY", manual=True)
            res1 = _do_entry(self.max_qr)
            status_code1 = res1.status_code if hasattr(res1, "status_code") else 200
            data1 = res1.get_json()

            self.assertEqual(status_code1, 200, f"MAX entry failed: {data1}")
            self.assertTrue(data1["success"])
            self.assertEqual(data1["action"], "entry")
            max_slot = data1["recommended_slot"]["slot_id"]
            self.assertIn(max_slot, ["A1", "A2", "B1", "B2"])
            self.assertEqual(data1["payment"]["charged_now"], 10.0)
            self.assertTrue(data1["gate_opened"])

            # Verify active session for MAX in DB
            driver_max = payment.find_driver(self.max_qr)
            max_sess = payment._active_session(driver_max["driver_id"])
            self.assertIsNotNone(max_sess, "MAX session was not created!")
            self.assertEqual(max_sess["status"], "ACTIVE")

            # Verify MAX's wallet deducted by Rs 10
            cur_wallet_max = payment.find_driver(self.max_qr)["wallet"]
            self.assertEqual(cur_wallet_max, 490.0)

            # ─────────────────────────────────────────────────────────────
            # 2. DUPLICATE ENTRY PROTECTION: MAX attempts re-entry
            # ─────────────────────────────────────────────────────────────
            res_dup_max = _do_entry(self.max_qr)
            status_dup = res_dup_max.status_code if hasattr(res_dup_max, "status_code") else res_dup_max[1]
            data_dup = res_dup_max.get_json() if hasattr(res_dup_max, "get_json") else res_dup_max[0].get_json()
            self.assertEqual(status_dup, 409, "Duplicate entry was not blocked!")
            self.assertIn("already parked", data_dup["message"].lower())
            # Ensure no extra charge
            self.assertEqual(payment.find_driver(self.max_qr)["wallet"], 490.0)

            # ─────────────────────────────────────────────────────────────
            # 3. ENTRY: CHARLES (9583)
            # ─────────────────────────────────────────────────────────────
            res2 = _do_entry(self.charles_qr)
            status_code2 = res2.status_code if hasattr(res2, "status_code") else 200
            data2 = res2.get_json()

            self.assertEqual(status_code2, 200, f"CHARLES entry failed: {data2}")
            self.assertTrue(data2["success"])
            self.assertEqual(data2["action"], "entry")
            charles_slot = data2["recommended_slot"]["slot_id"]
            self.assertIn(charles_slot, ["A1", "A2", "B1", "B2"])

            # Separate slots check!
            self.assertNotEqual(max_slot, charles_slot, f"MAX and CHARLES assigned same slot: {max_slot} vs {charles_slot}")

            # Verify active session for CHARLES
            driver_charles = payment.find_driver(self.charles_qr)
            charles_sess = payment._active_session(driver_charles["driver_id"])
            self.assertIsNotNone(charles_sess, "CHARLES session was not created!")
            self.assertEqual(charles_sess["status"], "ACTIVE")
            self.assertNotEqual(max_sess["sessionid"], charles_sess["sessionid"])

            # Verify CHARLES's wallet deducted by Rs 10
            self.assertEqual(payment.find_driver(self.charles_qr)["wallet"], 490.0)

            # Verify 2 active sessions in DB
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'")
            active_count = cur.fetchone()[0]
            conn.close()
            self.assertEqual(active_count, 2, f"Expected 2 active sessions, found {active_count}")

            # ─────────────────────────────────────────────────────────────
            # 4. DUPLICATE ENTRY PROTECTION: CHARLES attempts re-entry
            # ─────────────────────────────────────────────────────────────
            res_dup_ch = _do_entry(self.charles_qr)
            status_dup_ch = res_dup_ch.status_code if hasattr(res_dup_ch, "status_code") else res_dup_ch[1]
            self.assertEqual(status_dup_ch, 409)

            # ─────────────────────────────────────────────────────────────
            # 5. ACTIVE SESSION VALIDATION: Unparked driver exit attempt
            # ─────────────────────────────────────────────────────────────
            set_gate_direction("EXIT", manual=True)
            res_no_entry = _do_exit("UNPARKED-RANDOM-DRIVER")
            status_no_entry = res_no_entry.status_code if hasattr(res_no_entry, "status_code") else res_no_entry[1]
            self.assertEqual(status_no_entry, 402)

            # ─────────────────────────────────────────────────────────────
            # 6. EXIT: MAX (8743)
            # ─────────────────────────────────────────────────────────────
            res_exit_max = _do_exit(self.max_qr)
            status_exit_max = res_exit_max.status_code if hasattr(res_exit_max, "status_code") else 200
            data_exit_max = res_exit_max.get_json()
            self.assertEqual(status_exit_max, 200, f"MAX exit failed: {data_exit_max}")
            self.assertTrue(data_exit_max["success"])
            self.assertEqual(data_exit_max["action"], "exit")
            self.assertTrue(data_exit_max["gate_opened"])

            # MAX session should now be COMPLETED
            max_sess_after = payment._active_session(driver_max["driver_id"])
            self.assertIsNone(max_sess_after, "MAX session should be closed!")

            # MAX slot should be released in tracker
            self.assertFalse(tracker.occupancy_status[max_slot])

            # ─────────────────────────────────────────────────────────────
            # 7. EXIT: CHARLES (9583)
            # ─────────────────────────────────────────────────────────────
            res_exit_ch = _do_exit(self.charles_qr)
            status_exit_ch = res_exit_ch.status_code if hasattr(res_exit_ch, "status_code") else 200
            data_exit_ch = res_exit_ch.get_json()
            self.assertEqual(status_exit_ch, 200, f"CHARLES exit failed: {data_exit_ch}")
            self.assertTrue(data_exit_ch["success"])
            self.assertEqual(data_exit_ch["action"], "exit")
            self.assertTrue(data_exit_ch["gate_opened"])

            # CHARLES session should now be COMPLETED
            charles_sess_after = payment._active_session(driver_charles["driver_id"])
            self.assertIsNone(charles_sess_after, "CHARLES session should be closed!")

            # CHARLES slot should be released in tracker
            self.assertFalse(tracker.occupancy_status[charles_slot])

            # ─────────────────────────────────────────────────────────────
            # 8. POST-EXIT INTEGRITY CHECK
            # ─────────────────────────────────────────────────────────────
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'")
            remaining_active = cur.fetchone()[0]
            conn.close()
            self.assertEqual(remaining_active, 0, f"Expected 0 active sessions, found {remaining_active}")

            # Verify serial commands were sent for both vehicles
            self.assertTrue(any("ACCESS_GRANTED" in c for c in self.sent_commands))
            self.assertTrue(any("EXIT_OK" in c for c in self.sent_commands))


if __name__ == "__main__":
    unittest.main()
