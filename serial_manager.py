"""
===============================================================================
SMART GREEN PARKING SYSTEM - SERIAL MANAGER
===============================================================================
ONE module owns the Arduino COM port. Everything else talks to the Arduino
through this module, so the gate and the air-quality reader never fight over
the same port.

  Arduino -> Python : {"sensor":"MQ135","raw_value":N}  -> air_quality_monitor
                      IR_ENTRY / IR_EXIT                 -> remembered + logged
                      anything else                      -> kept for debugging
  Python  -> Arduino: serial_manager.send("ACCESS_GRANTED:50") etc.

Auto-reconnects if the cable is unplugged.
===============================================================================
"""

import collections
import threading
import time
from datetime import datetime

import serial
from serial.tools import list_ports

import db
from air_quality_monitor import process_serial_json

# ---------------------------------------------------------------------------
# CONFIG
SERIAL_PORT = "COM7"          # Project default: COM7 @ 9600 baud
BAUD_RATE = 9600
RECONNECT_SECONDS = 5
BOOT_WAIT_SECONDS = 2.5       # Nano resets when the port opens

_NANO_KEYWORDS = ("CH340", "CH341", "USB-SERIAL", "USB SERIAL",
                  "ARDUINO", "FTDI")


def find_arduino_port():
    """Returns the first port that looks like a Nano/Arduino, or None."""
    for p in list_ports.comports():
        text = f"{p.description} {p.manufacturer} {p.hwid}".upper()
        if any(k in text for k in _NANO_KEYWORDS):
            return p.device
    return None


class SerialManager:
    def __init__(self):
        self._ser = None
        self._write_lock = threading.Lock()
        self._thread = None

        self.connected = False
        self.port = None
        self.connect_count = 0          # goes up on every (re)connect
        self.last_error = None

        self.last_air = None            # last processed air alert dict
        self.last_air_level = None
        self.last_ir_event = None
        self.recent_lines = collections.deque(maxlen=50)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def start(self):
        """Start the background reader (safe to call more than once)."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[SERIAL] Manager started.")

    def send(self, command: str) -> bool:
        """Send one command line to the Arduino. Returns True if written."""
        with self._write_lock:
            if not self.connected or self._ser is None:
                return False
            try:
                self._ser.write((command.strip() + "\n").encode("utf-8"))
                self._ser.flush()
                return True
            except (serial.SerialException, OSError) as e:
                self.last_error = str(e)
                return False

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "connect_count": self.connect_count,
            "last_error": self.last_error,
            "last_air": self.last_air,
            "last_ir_event": self.last_ir_event,
            "recent_lines": list(self.recent_lines)[-10:],
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _run(self):
        last_printed_error = None
        while True:
            if self._connect():
                last_printed_error = None
                try:
                    self._read_loop()
                except (serial.SerialException, OSError) as e:
                    self.last_error = str(e)
                    print(f"[SERIAL] Connection lost: {e}")
                finally:
                    self._disconnect()
            elif self.last_error != last_printed_error:
                print(f"[SERIAL] Not connected: {self.last_error} "
                      f"(retrying every {RECONNECT_SECONDS}s)")
                last_printed_error = self.last_error
            time.sleep(RECONNECT_SECONDS)

    def _connect(self) -> bool:
        import os
        ports_to_try = []
        if env_port and env_port.upper() != "AUTO":
            ports_to_try.append((env_port, "env"))
        else:
            if SERIAL_PORT:
                ports_to_try.append((SERIAL_PORT, "configured (COM7)"))
            auto_port = find_arduino_port()
            if auto_port and auto_port != SERIAL_PORT:
                ports_to_try.append((auto_port, "auto-detect"))

        if not ports_to_try:
            self.last_error = "No Arduino port found"
            return False

        ser = None
        used_mode = "unknown"
        for p, m in ports_to_try:
            try:
                ser = serial.Serial(p, BAUD_RATE, timeout=1)
                port = p
                used_mode = m
                break
            except (serial.SerialException, OSError) as e:
                self.last_error = str(e)

        if ser is None:
            return False

        time.sleep(BOOT_WAIT_SECONDS)
        ser.reset_input_buffer()

        with self._write_lock:
            self._ser = ser
            self.port = port
            self.connected = True
            self.connect_count += 1
            self.last_error = None
            self.last_air_level = None   # forces the LCD to resync
        print(f"[SERIAL] Connected to Arduino on {port} (Mode: {used_mode})")
        return True

    def _disconnect(self):
        with self._write_lock:
            self.connected = False
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

    def _read_loop(self):
        while self.connected:
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str):
        stamp = datetime.now().isoformat(timespec="seconds")
        self.recent_lines.append({"time": stamp, "line": line})

        # --- air quality JSON from the MQ-135 --------------------------
        if line.startswith("{"):
            result = process_serial_json(line)
            if result:
                self.last_air = result
                level = result["alert_level"]
                if level != self.last_air_level:
                    self.last_air_level = level
                    self.send(f"AIR:{level}")      # show it on the LCD
            return

        # --- IR events --------------------------------------------------
        if line in ("IR_ENTRY", "IR_EXIT"):
            self.last_ir_event = {"event": line, "time": stamp}
            try:
                db.log_gate_event(line, note="IR sensor triggered")
            except Exception as e:
                print(f"[SERIAL] Could not log IR event: {e}")
            print(f"[ARDUINO] {line}")
            return

        # --- everything else (acknowledgements etc.) --------------------
        print(f"[ARDUINO] {line}")


# One shared instance for the whole project
serial_manager = SerialManager()