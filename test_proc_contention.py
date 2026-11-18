import cv2
import subprocess
import time
import sys

if len(sys.argv) > 1 and sys.argv[1] == "child":
    print("Process 2 attempting cv2.VideoCapture(0, cv2.CAP_DSHOW)...")
    cap2 = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    print(f"Process 2 cap2.isOpened(): {cap2.isOpened()}")
    ret, frame = cap2.read()
    print(f"Process 2 cap2.read() ret: {ret}, frame: {frame}")
    if not ret or frame is None:
        print("[CONTENTION ERROR] Process 2 failed to capture frame because Process 1 holds lock on camera 0.")
    if cap2: cap2.release()
else:
    print("Process 1 opening cv2.VideoCapture(0, cv2.CAP_DSHOW)...")
    cap1 = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    print(f"Process 1 cap1.isOpened(): {cap1.isOpened()}")
    ret1, frame1 = cap1.read()
    print(f"Process 1 read ret: {ret1}, frame shape: {frame1.shape if frame1 is not None else None}")
    
    # Spawn process 2 while process 1 holds camera
    p2 = subprocess.run([sys.executable, __file__, "child"], capture_output=True, text=True)
    print("\n--- Process 2 Output ---")
    print(p2.stdout)
    if p2.stderr:
        print("--- Process 2 Stderr ---")
        print(p2.stderr)
    
    cap1.release()
