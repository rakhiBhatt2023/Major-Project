"""
SmartPark AI - register a driver and make the QR code

Same as menu option 1 of smartpark_complete.py (same table, same QR content =
the driver_id), but it does NOT import pyzbar, so it works even when the
pyzbar/zbar DLL will not load on your machine.

    python register_driver.py

The QR image is saved in the qr_codes folder next to this file.
"""

import os
import uuid
from datetime import datetime

import qrcode

import db
import payment

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_DIR = os.path.join(BASE_DIR, "qr_codes")


def main():
    payment.init_minor_tables()
    os.makedirs(QR_DIR, exist_ok=True)

    print("\n=== REGISTER NEW DRIVER ===")
    name = input("Driver Name: ").strip()
    vehicle_no = input("Vehicle Number: ").strip().upper()
    phone = input("Phone Number: ").strip()
    wallet_text = input("Initial Wallet Balance (Rs.): ").strip()

    if not name or not vehicle_no:
        print("[ERROR] Name and vehicle number are required.")
        return
    if vehicle_no.startswith("TEST-"):
        print("[ERROR] TEST-* vehicle numbers are reserved for test_integration.py.")
        return
    if not phone.isdigit():
        print("[ERROR] Phone number must contain digits only.")
        return
    try:
        wallet = float(wallet_text)
        if wallet < 0:
            raise ValueError
    except ValueError:
        print("[ERROR] Wallet balance must be a non-negative number.")
        return

    driver_id = str(uuid.uuid4())
    registered_on = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO drivers
            (driver_id, name, vehicle_no, phone, wallet, registered_on)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (driver_id, name, vehicle_no, phone, wallet, registered_on))
        conn.commit()
    except Exception as err:
        conn.rollback()
        print("[ERROR] Driver could not be added:", err)
        print("Tip: the vehicle number must be unique.")
        return
    finally:
        conn.close()

    qr_path = os.path.join(QR_DIR, f"QR_{vehicle_no.replace(' ', '_')}.png")
    qrcode.make(driver_id).save(qr_path)

    print("\n========================================")
    print("DRIVER REGISTERED SUCCESSFULLY")
    print("========================================")
    print("Driver ID :", driver_id)
    print("Name      :", name)
    print("Vehicle   :", vehicle_no)
    print(f"Wallet    : Rs.{wallet:.2f}")
    print("QR Code   :", qr_path)
    print("========================================")


if __name__ == "__main__":
    main()