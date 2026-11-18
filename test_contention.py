import cv2

print("Opening Camera 0 (Primary capture service)...")
cap1 = cv2.VideoCapture(0, cv2.CAP_DSHOW)
print(f"Cap 1 isOpened: {cap1.isOpened()}")
ret1, frame1 = cap1.read()
print(f"Cap 1 read: ret={ret1}, frame={frame1.shape if frame1 is not None else None}")

print("\nAttempting to open Camera 0 second time (qr_gate_client)...")
cap2 = cv2.VideoCapture(0, cv2.CAP_DSHOW)
print(f"Cap 2 isOpened: {cap2.isOpened()}")
ret2, frame2 = cap2.read()
print(f"Cap 2 read: ret={ret2}, frame={frame2.shape if frame2 is not None else None}")

if not ret2 or frame2 is None:
    print("\n[CONTENTION PROOF] Cap 2 read failed (ret=False, frame=None). Windows DirectShow locks webcam exclusively to 1st capture instance!")

if cap1: cap1.release()
if cap2: cap2.release()
