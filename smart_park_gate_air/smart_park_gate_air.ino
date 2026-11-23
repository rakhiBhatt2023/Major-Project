// =====================================================================
// SmartPark AI - Gate + Air Quality controller (Arduino Nano)
//
// ONE sketch for everything: IR sensors, servo gate, LCD, LEDs, MQ-135.
// Serial speed: 9600
//
// Python -> Arduino commands (each ends with a newline):
//   ACCESS_GRANTED[:amount]   payment done, open gate, close after 3 s
//   EXIT_OK                   exit approved, open gate, close after 3 s
//   DENIED                    invalid QR / payment failed
//   FULL                      parking full (no payment taken)
//   TOTAL:n                   total slots shown on LCD
//   AVAILABLE:n               free slots shown on LCD
//   GOTO:id                   show "Go to Slot <id>" for 4 s (AI recommendation)
//   SLOT:...                  accepted and ignored (old minor-project command)
//   AIR:SAFE|WARNING|HARMFUL  air quality level shown on LCD top row
//   OPEN_ENTRY / OPEN_EXIT / CLOSE_GATE   manual gate control
//   GET_SENSORS / GET_AIR     on-demand reports
//
// Arduino -> Python lines:
//   ARDUINO_READY, IR_ENTRY, IR_EXIT, acknowledgements,
//   {"sensor":"MQ135","raw_value":N}   every 2 seconds
// =====================================================================

#include <Servo.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

LiquidCrystal_I2C lcd(0x27, 16, 2);   // use 0x3F if the screen stays blank
Servo gateServo;

// ---------------------------------------------------------------------
// PINS
// ---------------------------------------------------------------------
const int IR_ENTRY  = 2;
const int IR_EXIT   = 3;
const int RED_LED   = 4;
const int GREEN_LED = 5;
const int SERVO_PIN = 6;
const int MQ135_PIN = A0;

// Only 2 IR sensors are fitted (entry + exit). Slot occupancy comes from
// the camera software, so the 4 slot-sensor pins are switched off.
const bool HAS_SLOT_SENSORS = false;
const int SLOT_SENSORS[4] = {7, 8, 9, 10};
const int TOTAL_SLOTS = 4;

// ---------------------------------------------------------------------
// STATE
// ---------------------------------------------------------------------
int TOTAL_DISPLAY     = 4;
int AVAILABLE_DISPLAY = 4;
String airLevel       = "SAFE";

bool gateOpen = false;

unsigned long lastEntryTrigger = 0;
unsigned long lastExitTrigger  = 0;
unsigned long lastAirSend      = 0;

const unsigned long IR_DEBOUNCE_MS = 3000;
const unsigned long AIR_SEND_MS    = 2000;

// ---------------------------------------------------------------------
// LCD HELPERS
// ---------------------------------------------------------------------
void clearRow(int row) {
  lcd.setCursor(0, row);
  lcd.print("                ");
  lcd.setCursor(0, row);
}

void lcdRow0(const String &msg) {
  clearRow(0);
  lcd.print(msg);
}

void lcdShowSlots() {
  clearRow(1);
  lcd.print("Avl:");
  lcd.print(AVAILABLE_DISPLAY);
  lcd.print("/");
  lcd.print(TOTAL_DISPLAY);
}

void lcdIdle() {
  if (airLevel == "SAFE") {
    lcdRow0("SmartPark AI");
  } else {
    lcdRow0("Air: " + airLevel);
  }
  lcdShowSlots();
}

// ---------------------------------------------------------------------
// LEDs
// ---------------------------------------------------------------------
void updateParkingLED() {
  if (AVAILABLE_DISPLAY <= 0) {
    digitalWrite(RED_LED, HIGH);
  } else {
    digitalWrite(RED_LED, LOW);
  }
}

// ---------------------------------------------------------------------
// GATE
// ---------------------------------------------------------------------
void openGate() {
  digitalWrite(GREEN_LED, HIGH);
  digitalWrite(RED_LED, LOW);
  gateServo.write(90);
  delay(1000);
  Serial.println("SERVO_OPEN");
  gateOpen = true;
}

void closeGate() {
  gateServo.write(0);
  delay(1000);
  Serial.println("SERVO_CLOSE");
  gateOpen = false;
  digitalWrite(GREEN_LED, LOW);
  updateParkingLED();
}

// ---------------------------------------------------------------------
// TEMPORARY MESSAGE SCREEN (red LED on while shown)
// ---------------------------------------------------------------------
void showMessage(const char* line0, const char* line1, int holdMs) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line0);
  lcd.setCursor(0, 1);
  lcd.print(line1);

  digitalWrite(RED_LED, HIGH);
  delay(holdMs);
  digitalWrite(RED_LED, LOW);

  updateParkingLED();
  lcdIdle();
}

// ---------------------------------------------------------------------
// REPORTS
// ---------------------------------------------------------------------
void sendSlotStatus() {
  if (!HAS_SLOT_SENSORS) {
    Serial.println("SENSORS:NA");
    return;
  }
  Serial.print("SENSORS:");
  for (int i = 0; i < TOTAL_SLOTS; i++) {
    Serial.print(digitalRead(SLOT_SENSORS[i]) == LOW ? 1 : 0);
    if (i < TOTAL_SLOTS - 1) Serial.print(",");
  }
  Serial.println();
}

void sendAirReading() {
  Serial.print("{\"sensor\":\"MQ135\",\"raw_value\":");
  Serial.print(analogRead(MQ135_PIN));
  Serial.println("}");
}

// ---------------------------------------------------------------------
// SERIAL COMMANDS
// ---------------------------------------------------------------------
void handleSerialCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  Serial.print("Received: ");
  Serial.println(cmd);

  if (cmd.startsWith("ACCESS_GRANTED")) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Payment Done");
    lcd.setCursor(0, 1);

    int idx = cmd.indexOf(':');
    if (idx != -1) {
      lcd.print("Rs ");
      lcd.print(cmd.substring(idx + 1));
      lcd.print(" Deduct");
    } else {
      lcd.print("Gate Opening");
    }

    openGate();
    Serial.println("ENTRY_GATE_OPENED");
    delay(3000);
    closeGate();
    lcdIdle();
    Serial.println("GATE_CLOSED");
  }

  else if (cmd == "EXIT_OK") {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Exit Approved");
    lcd.setCursor(0, 1);
    lcd.print("Gate Opening");

    openGate();
    Serial.println("EXIT_GATE_OPENED");
    delay(3000);
    closeGate();
    lcdIdle();
    Serial.println("GATE_CLOSED");
  }

  else if (cmd == "DENIED") {
    showMessage("Access Denied", "Invalid QR", 1500);
    Serial.println("ACCESS_DENIED");
  }

  else if (cmd == "FULL") {
    showMessage("Parking Full", "Try Later", 2000);
    Serial.println("PARKING_FULL");
  }

  else if (cmd.startsWith("TOTAL:")) {
    TOTAL_DISPLAY = cmd.substring(6).toInt();
    if (!gateOpen) lcdShowSlots();
    Serial.println("TOTAL_UPDATED");
  }

  else if (cmd.startsWith("AVAILABLE:")) {
    AVAILABLE_DISPLAY = cmd.substring(10).toInt();
    if (!gateOpen) lcdShowSlots();
    updateParkingLED();
    Serial.println("AVAILABLE_UPDATED");
  }

  else if (cmd.startsWith("GOTO:")) {
    String id = cmd.substring(5);
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Go to Slot");
    lcd.setCursor(0, 1);
    lcd.print(id);
    delay(4000);
    lcdIdle();
    Serial.println("GOTO_SHOWN");
  }

  else if (cmd.startsWith("SLOT:")) {
    // old minor-project command (SLOT:3:1) - nothing to do, just acknowledge
    Serial.println("SLOT_STATUS_UPDATED");
  }

  else if (cmd.startsWith("AIR:")) {
    airLevel = cmd.substring(4);
    if (!gateOpen) lcdIdle();
    Serial.println("AIR_UPDATED");
  }

  else if (cmd == "OPEN_ENTRY") {
    lcdRow0("Entry Open");
    lcdShowSlots();
    openGate();
    Serial.println("ENTRY_GATE_OPENED");
  }

  else if (cmd == "OPEN_EXIT") {
    lcdRow0("Exit Open");
    lcdShowSlots();
    openGate();
    Serial.println("EXIT_GATE_OPENED");
  }

  else if (cmd == "CLOSE_GATE") {
    closeGate();
    lcdRow0("Gate Closed");
    lcdShowSlots();
    Serial.println("GATE_CLOSED");
  }

  else if (cmd == "GET_SENSORS") {
    sendSlotStatus();
  }

  else if (cmd == "GET_AIR") {
    sendAirReading();
  }
}

// ---------------------------------------------------------------------
// SETUP
// ---------------------------------------------------------------------
void setup() {
  Serial.begin(9600);

  lcd.init();
  lcd.backlight();

  pinMode(IR_ENTRY, INPUT_PULLUP);
  pinMode(IR_EXIT, INPUT_PULLUP);
  pinMode(RED_LED, OUTPUT);
  pinMode(GREEN_LED, OUTPUT);

  if (HAS_SLOT_SENSORS) {
    for (int i = 0; i < TOTAL_SLOTS; i++) {
      pinMode(SLOT_SENSORS[i], INPUT_PULLUP);
    }
  }

  digitalWrite(RED_LED, LOW);
  digitalWrite(GREEN_LED, LOW);

  gateServo.attach(SERVO_PIN);
  delay(500);
  gateServo.write(0);
  delay(1000);

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("SmartPark AI");
  lcd.setCursor(0, 1);
  lcd.print("System Ready");
  delay(1500);

  lcdIdle();
  Serial.println("ARDUINO_READY");
}

// ---------------------------------------------------------------------
// LOOP
// ---------------------------------------------------------------------
void loop() {
  unsigned long now = millis();

  // 1) commands from Python
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    handleSerialCommand(cmd);
  }

  // 2) entry IR (Edge-triggered: HIGH -> LOW transition on vehicle arrival)
  static int prevEntryVal = HIGH;
  int currEntryVal = digitalRead(IR_ENTRY);
  if (currEntryVal == LOW && prevEntryVal == HIGH && (now - lastEntryTrigger > IR_DEBOUNCE_MS)) {
    lastEntryTrigger = now;
    Serial.println("IR_ENTRY");
  }
  prevEntryVal = currEntryVal;

  // 3) exit IR (Edge-triggered: HIGH -> LOW transition on vehicle arrival)
  static int prevExitVal = HIGH;
  int currExitVal = digitalRead(IR_EXIT);
  if (currExitVal == LOW && prevExitVal == HIGH && (now - lastExitTrigger > IR_DEBOUNCE_MS)) {
    lastExitTrigger = now;
    Serial.println("IR_EXIT");
  }
  prevExitVal = currExitVal;

  // 4) MQ-135 reading every 2 seconds, as JSON
  if (now - lastAirSend >= AIR_SEND_MS) {
    lastAirSend = now;
    sendAirReading();
  }
}
