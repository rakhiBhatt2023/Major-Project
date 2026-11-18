import cv2
import time

def test_camera():
    backends = [
        ("CAP_DSHOW", cv2.CAP_DSHOW),
        ("CAP_MSMF", cv2.CAP_MSMF),
        ("CAP_ANY", cv2.CAP_ANY)
    ]
    results = []
    for idx in range(3):
        for name, backend in backends:
            cap = cv2.VideoCapture(idx, backend)
            opened = cap.isOpened()
            read_ok = False
            frame_shape = None
            if opened:
                ret, frame = cap.read()
                read_ok = ret and (frame is not None)
                if read_ok:
                    frame_shape = frame.shape
                cap.release()
            results.append({
                "index": idx,
                "backend": name,
                "opened": opened,
                "read_ok": read_ok,
                "shape": frame_shape
            })
    print("\n--- CAMERA HARDWARE DIAGNOSTIC RESULTS ---")
    for r in results:
        print(f"Index {r['index']} | Backend: {r['backend']:<10} | Opened: {str(r['opened']):<5} | Read OK: {str(r['read_ok']):<5} | Frame Shape: {r['shape']}")

if __name__ == "__main__":
    test_camera()
