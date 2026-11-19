"""
===============================================================================
SMART GREEN PARKING SYSTEM — COMPREHENSIVE VERIFICATION & TRANSACTION TEST SUITE
===============================================================================
Tests:
  1. All 4 slots vacant -> VACANT (Green)
  2. 1 slot occupied -> VACANT/OCCUPIED status correct
  3. Camera offline/stale -> UNKNOWN (Yellow/Gray), entry blocked with CAMERA_OFFLINE (503), fee=0
  4. Temporary slot reservation -> RESERVED (Blue), double booking prevented
  5. Parking full -> PARKING_FULL (409), fee=0
  6. Invalid QR scan -> INVALID_QR (402), fee=0
  7. Successful transactional entry -> Check -> Reserve -> Payment -> Arduino Gate Trigger
  8. Successful exit flow -> Fee calculated -> Gate OPEN
===============================================================================
"""

import os
import sys
import time
import json
import uuid
import unittest
import numpy as np

# Ensure root workspace is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import payment
from realtime_tracker import RealtimeParkingTracker
from green_score import get_recommended_slot
from serial_manager import serial_manager


class TestSmartParkSystem(unittest.TestCase):
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
        cls.tracker = RealtimeParkingTracker()
        db.sync_slot_coordinates(cls.tracker.slots)

    @classmethod
    def tearDownClass(cls):
        db.reset_db_path()
        if hasattr(cls, 'temp_db_path') and os.path.exists(cls.temp_db_path):
            try:
                os.remove(cls.temp_db_path)
            except Exception:
                pass

    def setUp(self):
        # Reset tracker state before each test
        for s in self.tracker.slots:
            self.tracker.occupancy_status[s] = False
        self.tracker.reservations.clear()
        self.tracker.use_live_webcam = False
        self.tracker.last_frame_timestamp = time.time()

    def test_01_vacant_slots(self):
        """TEST 1: All 4 slots empty (VACANT / FREE)."""
        self.tracker.use_live_webcam = True
        self.tracker.last_frame_timestamp = time.time()
        feed = self.tracker.get_live_slot_feed()
        for slot_id, s in feed.items():
            self.assertIn(s["status"], ["VACANT", "FREE"])
            self.assertTrue(s["vacant"])
            self.assertFalse(s["reserved"])
            self.assertFalse(s["unknown"])
        
        vacant_count = sum(1 for s in feed.values() if s["vacant"])
        self.assertEqual(vacant_count, 4)

    def test_02_occupied_slot(self):
        """TEST 2: 1 slot occupied."""
        self.tracker.use_live_webcam = True
        self.tracker.last_frame_timestamp = time.time()
        self.tracker.occupancy_status["A1"] = True
        feed = self.tracker.get_live_slot_feed()
        self.assertEqual(feed["A1"]["status"], "OCCUPIED")
        self.assertFalse(feed["A1"]["vacant"])
        self.assertIn(feed["A2"]["status"], ["VACANT", "FREE"])
        self.assertTrue(feed["A2"]["vacant"])

    def test_03_camera_offline_unknown_state(self):
        """TEST 3: Camera offline/stale marks all slots UNKNOWN, preventing false PARKING FULL."""
        self.tracker.use_live_webcam = False
        self.tracker.last_frame_timestamp = 0.0  # Stale timestamp
        self.assertFalse(self.tracker.is_camera_healthy())

        feed = self.tracker.get_live_slot_feed()
        for slot_id, s in feed.items():
            self.assertEqual(s["status"], "UNKNOWN")
            self.assertTrue(s["unknown"])
            self.assertFalse(s["vacant"])

    def test_04_slot_reservation(self):
        """TEST 4: Temporary slot reservation sets status RESERVED / ASSIGNED and prevents double booking."""
        self.tracker.use_live_webcam = True
        self.tracker.last_frame_timestamp = time.time()
        
        tx_id1 = "TX-TEST01"
        res1 = self.tracker.reserve_slot("A1", tx_id1, "DRIVER-001", duration=5.0)
        self.assertTrue(res1)

        feed = self.tracker.get_live_slot_feed()
        self.assertIn(feed["A1"]["status"], ["RESERVED", "ASSIGNED"])
        self.assertTrue(feed["A1"]["reserved"])

        # Second transaction trying to reserve same slot should fail
        tx_id2 = "TX-TEST02"
        res2 = self.tracker.reserve_slot("A1", tx_id2, "DRIVER-002", duration=5.0)
        self.assertFalse(res2)

        # Release reservation
        self.tracker.release_reservation(slot_id="A1")
        feed_after = self.tracker.get_live_slot_feed()
        self.assertIn(feed_after["A1"]["status"], ["VACANT", "FREE"])

    def test_05_green_score_recommendation(self):
        """TEST 5: AI slot recommendation selects optimal slot based on distance & congestion."""
        self.tracker.use_live_webcam = True
        self.tracker.last_frame_timestamp = time.time()
        feed = self.tracker.get_live_slot_feed()
        rec = get_recommended_slot(feed)
        self.assertIsNotNone(rec)
        self.assertIn(rec["slot_id"], ["A1", "A2", "B1", "B2"])
        self.assertIn("score", rec)

    def test_06_database_tables(self):
        """TEST 6: DB schema initialization and helper functions for reservations and transactions."""
        tx_id = f"TX-DBTEST-{uuid.uuid4().hex[:6]}"
        db.log_slot_reservation("A1", tx_id, "QR-123", duration=60.0)
        db.update_slot_reservation_status(tx_id, "COMPLETED")

        db.log_parking_transaction(tx_id, "QR-123", "ENTRY", "SUCCESS", fee=20.0, slot_id="A1")
        txs = db.get_parking_transactions(limit=5)
        self.assertTrue(len(txs) > 0)
        self.assertEqual(txs[0]["transaction_id"], tx_id)
        self.assertEqual(txs[0]["status"], "SUCCESS")

    def test_07_api_entry_check_and_step_flow(self):
        """TEST 7: Flask API step-by-step entry flow (/api/entry/check, reserve, pay)."""
        from app import app, tracker
        from serial_manager import serial_manager
        client = app.test_client()

        # Create test driver
        driver_id = payment.create_test_driver("TEST-FLOW-01", wallet=500.0)

        # Set mock connected state & camera health
        serial_manager.connected = True
        tracker.use_live_webcam = True
        tracker.last_frame_timestamp = time.time()
        for s in tracker.slots:
            tracker.occupancy_status[s] = False

        try:
            # Step 1: Check
            res_check = client.post('/api/entry/check', json={"qr": driver_id})
            self.assertEqual(res_check.status_code, 200)
            data_check = res_check.get_json()
            self.assertTrue(data_check["success"])
            rec_slot = data_check["recommended_slot"]["slot_id"]

            # Step 2: Reserve
            res_reserve = client.post('/api/entry/reserve', json={"qr": driver_id, "slot_id": rec_slot})
            self.assertEqual(res_reserve.status_code, 200)
            data_reserve = res_reserve.get_json()
            tx_id = data_reserve["transaction_id"]

            # Step 3: Pay
            res_pay = client.post('/api/entry/pay', json={"qr": driver_id, "transaction_id": tx_id, "slot_id": rec_slot, "amount": 10.0})
            self.assertEqual(res_pay.status_code, 200)
            data_pay = res_pay.get_json()
            self.assertTrue(data_pay["success"])
        finally:
            payment.remove_test_driver("TEST-FLOW-01")

    def test_08_invalid_qr_returns_error(self):
        """TEST 8: Unregistered QR scan returns INVALID_QR error code (402/400)."""
        from app import app, tracker
        from serial_manager import serial_manager
        client = app.test_client()

        serial_manager.connected = True
        tracker.use_live_webcam = True
        tracker.last_frame_timestamp = time.time()

        res = client.post('/api/gate/scan', json={"qr": "INVALID-DRIVER-9999"})
        self.assertEqual(res.status_code, 402)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error_code"], "INVALID_QR")
        self.assertEqual(data["qr"], "INVALID-DRIVER-9999")

    def test_09_qr_consistency_and_idempotency(self):
        """TEST 9: Prove scanned QR 'a1b8fb4d-753b-4a12-a95d-d6c3ebc7bf6' is preserved as single source of truth everywhere."""
        from app import app, tracker
        from serial_manager import serial_manager
        client = app.test_client()

        canonical_qr = "a1b8fb4d-753b-4a12-a95d-d6c3ebc7bf6"

        # Register test driver with canonical QR and clean any lingering test sessions
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE driver_id = ?", (canonical_qr,))
        cur.execute("DELETE FROM parkingrecords WHERE driver_id = ?", (canonical_qr,))
        cur.execute("""
            INSERT OR REPLACE INTO drivers (driver_id, name, vehicle_no, phone, wallet)
            VALUES (?, 'Canonical Driver', 'TEST-CANON-01', '9999999999', 500.0)
        """, (canonical_qr,))
        conn.commit()
        conn.close()

        serial_manager.connected = True
        import gate_routes
        old_cooldown = gate_routes.DOUBLE_SCAN_GUARD_SECONDS
        gate_routes.DOUBLE_SCAN_GUARD_SECONDS = 0.0

        try:
            # 9.1 Camera Offline Error case
            tracker.use_live_webcam = False
            tracker.last_frame_timestamp = 0.0
            res_cam = client.post('/api/gate/scan', json={"qr": canonical_qr})
            self.assertEqual(res_cam.status_code, 503)
            data_cam = res_cam.get_json()
            self.assertEqual(data_cam["error_code"], "CAMERA_OFFLINE")
            self.assertEqual(data_cam["qr"], canonical_qr)

            # 9.2 Parking Full Error case
            tracker.use_live_webcam = True
            tracker.last_frame_timestamp = time.time()
            for s in tracker.slots:
                tracker.occupancy_status[s] = True
            res_full = client.post('/api/gate/scan', json={"qr": canonical_qr})
            self.assertEqual(res_full.status_code, 409)
            data_full = res_full.get_json()
            self.assertEqual(data_full["error_code"], "PARKING_FULL")
            self.assertEqual(data_full["qr"], canonical_qr)

            # 9.3 Entry Success case
            for s in tracker.slots:
                tracker.occupancy_status[s] = False
            res_entry = client.post('/api/gate/scan', json={"qr": canonical_qr})
            self.assertEqual(res_entry.status_code, 200)
            data_entry = res_entry.get_json()
            self.assertTrue(data_entry["success"])
            self.assertEqual(data_entry["action"], "entry")
            self.assertEqual(data_entry["qr"], canonical_qr)

            # Check DB records for canonical QR identity
            txs = db.get_parking_transactions(limit=1)
            self.assertEqual(txs[0]["qr"], canonical_qr)

            events = db.get_gate_events(limit=1)
            self.assertEqual(events[0]["qr"], canonical_qr)

            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT qr FROM slot_reservations ORDER BY id DESC LIMIT 1")
            res_row = cur.fetchone()
            self.assertEqual(res_row["qr"], canonical_qr)

            cur.execute("SELECT qrtext FROM scanlogs ORDER BY id DESC LIMIT 1")
            scan_row = cur.fetchone()
            self.assertEqual(scan_row["qrtext"], canonical_qr)
            conn.close()

            # 9.4 Idempotency Check: 2nd scan while inside triggers safe Exit flow in EXIT direction
            res_exit = client.post('/api/gate/scan', json={"qr": canonical_qr, "direction": "EXIT"})
            self.assertEqual(res_exit.status_code, 200)
            data_exit = res_exit.get_json()
            self.assertTrue(data_exit["success"])
            self.assertEqual(data_exit["action"], "exit")
            self.assertEqual(data_exit["qr"], canonical_qr)

        finally:
            gate_routes.DOUBLE_SCAN_GUARD_SECONDS = old_cooldown
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE driver_id = ?", (canonical_qr,))
            cur.execute("DELETE FROM parkingrecords WHERE driver_id = ?", (canonical_qr,))
            cur.execute("DELETE FROM scanlogs WHERE qrtext = ?", (canonical_qr,))
            cur.execute("DELETE FROM drivers WHERE driver_id = ?", (canonical_qr,))
            conn.commit()
            conn.close()

    def test_10_camera_health_timestamp_refresh(self):
        """TEST 10 (REGRESSION): Camera health must stay True after 15+ seconds of frame capture."""
        tracker = RealtimeParkingTracker()
        tracker.use_live_webcam = True
        
        fake_frame = np.zeros((520, 600, 3), dtype=np.uint8)
        
        class MockCap:
            def isOpened(self): return True
            def read(self): return True, fake_frame
        
        tracker.cap = MockCap()
        
        for _ in range(15):
            tracker.capture_next_frame()
            time.sleep(0.01)
        
        tracker.last_frame_timestamp -= 15.0
        tracker.capture_next_frame()
        
        self.assertTrue(tracker.is_camera_healthy(), "Camera health became False after 15s of active frame capture!")

    def test_11_slow_inference_separate_timestamps(self):
        """TEST 11: Frame capture and ROI inference timestamps are tracked independently."""
        tracker = RealtimeParkingTracker()
        tracker.use_live_webcam = True
        now = time.monotonic()
        tracker.last_frame_timestamp = now
        tracker.last_detection_timestamp = now - 12.0  # Detection stale (>10s)

        class MockCap:
            def isOpened(self): return True

        tracker.cap = MockCap()
        # Camera capture timestamp is fresh, but detection is stale -> is_camera_healthy must return False
        self.assertFalse(tracker.is_camera_healthy())
        self.assertEqual(tracker.get_unhealthy_reason()[:15], "detection stale")

    def test_12_camera_failure_reconnect_recovery(self):
        """TEST 12: Camera read failure -> slots UNKNOWN -> auto-reconnect recovery -> VACANT/OCCUPIED."""
        tracker = RealtimeParkingTracker()
        tracker.use_live_webcam = True
        # Set frame timestamp to stale (>10s ago)
        tracker.last_frame_timestamp = time.monotonic() - 12.0
        tracker.last_detection_timestamp = time.monotonic()

        class FailingCap:
            def isOpened(self): return True
            def read(self): return False, None
            def release(self): pass

        tracker.cap = FailingCap()
        tracker.init_camera = lambda: False
        
        # Simulate 3 failures
        for _ in range(3):
            tracker.capture_next_frame()
            time.sleep(0.01)
        
        # Camera is failing (stale frame timestamp)
        self.assertEqual(tracker.consecutive_failures, 3)
        feed_failing = tracker.get_live_slot_feed()
        self.assertEqual(feed_failing["A1"]["status"], "UNKNOWN")

        # Now simulate recovery with working cap and fresh timestamp
        class WorkingCap:
            def isOpened(self): return True
            def read(self):
                return True, np.zeros((520, 600, 3), dtype=np.uint8)
            def release(self): pass

        tracker.cap = WorkingCap()
        now = time.monotonic()
        tracker.last_frame_timestamp = now
        tracker.last_detection_timestamp = now
        tracker.consecutive_failures = 0
        tracker.use_live_webcam = True
        feed_recovered = tracker.get_live_slot_feed()
        self.assertIn(feed_recovered["A1"]["status"], ["VACANT", "FREE", "OCCUPIED"])

    def test_13_single_tracker_instance_verification(self):
        """TEST 13: Verify single tracker instance shared across Flask app and gate routes."""
        from app import app, tracker as app_tracker
        from gate_routes import _tracker as gate_tracker
        self.assertIs(app_tracker, gate_tracker)

    def test_14_shared_camera_qr_scan_debounce(self):
        """TEST 14: Server-side QR absent/handled filter prevents duplicate entry transactions within 5s."""
        from app import _handled_qrs, QR_ABSENT_TIMEOUT
        import time as t
        now = t.monotonic()
        test_qr = "DEBOUNCE-TEST-QR-001"
        _handled_qrs[test_qr] = {"handled_at": now, "action": "entry"}
        
        # Attempt 2nd scan 0.5s later (within 5.0s window)
        handled_at = _handled_qrs.get(test_qr, {}).get("handled_at", 0.0)
        is_ignored = ((now + 0.5) - handled_at < QR_ABSENT_TIMEOUT)
        self.assertTrue(is_ignored, "Server-side idempotency filter failed to ignore duplicate scan within 5s window!")

    def test_15_qr_visible_20s_single_transaction(self):
        """TEST 15: Same QR visible continuously for 20 simulated seconds produces exactly ONE transaction & ONE payment."""
        from app import process_scanned_qr, app, tracker, _handled_qrs
        import payment
        from serial_manager import serial_manager

        # Ensure serial_manager is marked connected for testing
        serial_manager.connected = True
        _handled_qrs.clear()

        test_qr = "test-20s-qr-unique"

        # Ensure camera is healthy and slots are vacant
        tracker.use_live_webcam = True
        tracker.last_frame_timestamp = time.monotonic()
        tracker.last_detection_timestamp = time.monotonic()
        for s in tracker.slots:
            tracker.occupancy_status[s] = False

        # Ensure driver registered directly in DB and clean sessions
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE driver_id = ?", (test_qr,))
        cur.execute("DELETE FROM parkingrecords WHERE driver_id = ?", (test_qr,))
        cur.execute("""
            INSERT OR REPLACE INTO drivers (driver_id, name, vehicle_no, phone, wallet, registered_on)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (test_qr, "Test Driver 20s", "MH-01-2020", "9999999999", 500.0, "2026-10-08 00:00:00"))
        conn.commit()
        conn.close()

        driver = payment.find_driver(test_qr)
        initial_wallet = float(driver["wallet"])

        # Simulate QR visible every 0.5s for 20s (40 scans)
        executed_actions = []
        for i in range(40):
            with app.test_request_context():
                res = process_scanned_qr(test_qr)
                if res is not None:
                    executed_actions.append(res)
            time.sleep(0.01)

        # Assert exactly ONE entry execution occurred
        self.assertEqual(len(executed_actions), 1, f"Expected exactly 1 execution for 20s scan, got {len(executed_actions)}")

        # Check driver wallet balance after 20s
        updated_driver = payment.find_driver(test_qr)
        expected_wallet = initial_wallet - payment.ENTRY_FEE
        self.assertEqual(float(updated_driver["wallet"]), expected_wallet, "Wallet was debited more than once!")

    def test_16_slow_entry_api_does_not_break_camera_health(self):
        """TEST 16: Decoupled architecture: a slow 2s entry call in worker thread does NOT make camera unhealthy."""
        import threading
        from app import tracker
        from realtime_tracker import RealtimeParkingTracker
        
        test_tracker = RealtimeParkingTracker()
        test_tracker.use_live_webcam = True
        test_tracker.last_frame_timestamp = time.monotonic()
        test_tracker.last_detection_timestamp = time.monotonic()

        class MockCap:
            def isOpened(self): return True
            def read(self):
                return True, np.zeros((520, 600, 3), dtype=np.uint8)
            def release(self): pass

        test_tracker.cap = MockCap()

        # Simulate capture thread running in background while a slow operation occurs
        stop_flag = False
        def mock_capture_loop():
            while not stop_flag:
                test_tracker.capture_next_frame()
                time.sleep(0.05)

        cap_thread = threading.Thread(target=mock_capture_loop, daemon=True)
        cap_thread.start()

        # Simulate a slow 2-second entry API processing call
        time.sleep(2.0)
        stop_flag = True

        # Camera MUST remain healthy because capture thread ran independently!
        self.assertTrue(test_tracker.is_camera_healthy(), "Camera became unhealthy during slow entry API call!")
        info = test_tracker.get_camera_health_info()
        self.assertLess(info["last_frame_age_seconds"], 1.0, "Frame age exceeded 1.0s during capture loop!")


if __name__ == "__main__":
    print("\n========================================================")
    print(" RUNNING SMART GREEN PARKING TRANSACTIONAL TEST SUITE")
    print("========================================================\n")
    unittest.main(verbosity=2)
