# =============================================================================
# config.py — ค่าตั้งทั้งหมดของ node "สวนปลูกฝัน" (prefix: plukfan)
# -----------------------------------------------------------------------------
# *** ไฟล์นี้ "ปรับได้ทั้งหมด" แยกจาก logic เพื่อให้แก้ค่าได้โดยไม่แตะโค้ดหลัก ***
# จุดที่ต้องแก้ก่อนใช้งานจริง (มาร์ก PLACEHOLDER):
#   - RAW_DRY / RAW_WET  : ต้องวัดกับเซนเซอร์ตัวจริง
#   - WIFI_SSID / WIFI_PASS
#   - MQTT_HOST / MQTT_USER / MQTT_PASS
# =============================================================================

# -----------------------------------------------------------------------------
# ค่าลับ (WiFi / MQTT credentials) — ดึงจาก secrets.py ที่ถูก gitignore
# ถ้ายังไม่มี secrets.py ให้คัดลอกจาก secrets.example.py แล้วกรอกค่าจริง
# -----------------------------------------------------------------------------
try:
    import secrets as _secrets
except ImportError:
    _secrets = None
    print("[config] เตือน: ไม่พบ secrets.py — คัดลอกจาก secrets.example.py แล้วกรอกค่าจริง")


def _secret(name, default):
    # อ่านค่าจาก secrets.py ถ้ามี ไม่งั้น fallback เป็น placeholder (จะ connect ไม่ได้)
    return getattr(_secrets, name, default) if _secrets is not None else default


# -----------------------------------------------------------------------------
# โซน + อุปกรณ์ (ใช้ประกอบ MQTT topic ตาม convention plukfan/<zone>/<device>/...)
# -----------------------------------------------------------------------------
ZONE      = "veggie"            # โซนของ node นี้ (ผักหลัก = กะเพรา)  *** ปรับได้ ***
NODE_ID   = "picow-01"          # ชื่อ node สำหรับ availability/diagnostics  *** ปรับได้ ***

# -----------------------------------------------------------------------------
# Pin map — relay เป็น active-LOW, ทุก actuator ต้อง SAFE/off ตอนบูต
# *** ขา relay (PUMP/VALVE) ต้องมี external pull-up บนบอร์ด เพื่อให้ตอน
#     Hardware WDT reset แล้ว GPIO เป็น hi-Z → relay คลายเป็น OFF จริง
#     = การันตี "ปั๊มหยุดทำงาน" แม้เฟิร์มแวร์พังกลางคัน ***
# -----------------------------------------------------------------------------
PIN_SOIL          = 26          # GP26 / ADC0  — soil moisture (capacitive, ค่าดิบ 0-4095)
PIN_PH            = 27          # GP27 / ADC1  — pH (telemetry เท่านั้น)
PIN_EC            = 28          # GP28 / ADC2  — EC (telemetry เท่านั้น)
PIN_I2C_SDA       = 2           # GP2          — I2C SDA (SHT31 / BME280)
PIN_I2C_SCL       = 3           # GP3          — I2C SCL
PIN_FLOAT_SWITCH  = 15          # GP15         — float switch (input, pull-up ภายใน)
PIN_PUMP          = 16          # GP16         — ปั๊ม (relay active-LOW, ต้องมี external pull-up)
PIN_VALVE_DRIP    = 17          # GP17         — วาล์วน้ำหยด (relay active-LOW, ต้องมี external pull-up)
PIN_VALVE_SPRINK  = 18          # GP18         — วาล์วสปริงเกอร์ (relay active-LOW, ต้องมี external pull-up)
PIN_RESET_BTN     = 14          # GP14         — ปุ่มกายภาพ manual reset (input, pull-up ภายใน)

# relay active-LOW: เขียน 0 = อุปกรณ์ทำงาน, เขียน 1 = อุปกรณ์หยุดทำงาน (SAFE)
RELAY_ACTIVE_LOW  = True

# float switch (input pull-up): กำหนดว่าค่าที่อ่านได้เท่าใด = "น้ำในถังพอ"
# ต่อแบบสวิตช์ลงกราวด์เมื่อมีน้ำลอย → อ่านได้ 0 เมื่อน้ำพอ
FLOAT_OK_LEVEL    = 0           # *** ปรับ polarity ให้ตรงกับการต่อจริง ***

# -----------------------------------------------------------------------------
# WiFi  *** ค่าจริงอยู่ใน secrets.py ***
# -----------------------------------------------------------------------------
WIFI_SSID = _secret("WIFI_SSID", "PLACEHOLDER_SSID")
WIFI_PASS = _secret("WIFI_PASS", "PLACEHOLDER_PASSWORD")

# -----------------------------------------------------------------------------
# MQTT  *** credentials อยู่ใน secrets.py — บังคับ TLS + username/password ***
# -----------------------------------------------------------------------------
MQTT_HOST     = _secret("MQTT_HOST", "PLACEHOLDER_BROKER")  # จาก secrets.py
MQTT_PORT     = 8883                    # 8883 = MQTT over TLS
MQTT_USER     = _secret("MQTT_USER", "PLACEHOLDER_USER")    # จาก secrets.py
MQTT_PASS     = _secret("MQTT_PASS", "PLACEHOLDER_PASS")    # จาก secrets.py
MQTT_USE_TLS  = True                    # บังคับ TLS (ห้ามปิดในงานจริง)
MQTT_KEEPALIVE_S = 30                   # keepalive (วินาที) — ใช้คำนวณ LWT timeout ที่ broker

# CA cert สำหรับ verify broker (self-signed ใส่ใน secrets.py เป็น PEM bytes; None = default)
# *** แนะนำให้ตั้งค่า cert จริงเพื่อ verify ตัว broker ***
MQTT_SSL_CA   = _secret("MQTT_SSL_CA", None)

# connect timeout: ถ้า WiFi/MQTT connect (รวม TLS handshake) นานเกินนี้
# = เน็ตมีปัญหาจริง → ปล่อยให้ Hardware WDT reset เป็นพฤติกรรมที่ถูก
MQTT_CONNECT_TIMEOUT_MS = 6000          # *** ปรับได้ ***

# -----------------------------------------------------------------------------
# MQTT topic convention (plukfan/)
#   telemetry : plukfan/<zone>/<device>/<channel>
#   command   : plukfan/<zone>/<actuator>/cmd     (QoS1, ห้าม retain)
#   state     : plukfan/<zone>/<actuator>/state   (retained — closed-loop แยกจาก cmd)
#   system    : plukfan/<zone>/system/cmd         (manual reset)
#   avail/LWT : plukfan/node/<node>/availability  (retained)
#   sys/diag  : plukfan/node/<node>/sys/<metric>
# -----------------------------------------------------------------------------
TOPIC_PREFIX = "plukfan"

# -----------------------------------------------------------------------------
# Threshold mode: เลือกใช้ค่าตามโซน หรือ ตามชนิดพืช
# -----------------------------------------------------------------------------
THRESHOLD_MODE = "zone"          # "zone" หรือ "crop"  *** ปรับได้ ***

# เกณฑ์ความชื้นตามโซน (low = เริ่มรด, high = หยุดรด) — หน่วย %
THRESHOLDS_ZONE = {
    "veggie": {"low": 60, "high": 70},   # ผักหลัก = กะเพรา
    "banana": {"low": 60, "high": 75},
}

# --- เกณฑ์รายพืช (ปิดไว้) ---
# วิธีเปิดใช้: ตั้ง THRESHOLD_MODE = "crop", เอาคอมเมนต์ block ข้างล่างออก,
# แล้วตั้ง CROP ให้ตรงกับพืชที่ปลูกในโซนนี้
# THRESHOLDS_CROP = {
#     "basil":         {"low": 60, "high": 70},
#     "cucumber":      {"low": 65, "high": 75},
#     "yardlong_bean": {"low": 55, "high": 65},
#     "banana":        {"low": 60, "high": 75},
# }
CROP = "basil"                   # ใช้เฉพาะตอน THRESHOLD_MODE == "crop"  *** ปรับได้ ***

# -----------------------------------------------------------------------------
# พารามิเตอร์ควบคุมปั๊ม / FSM (อิงเวลา monotonic — ticks_ms/ticks_diff)
# -----------------------------------------------------------------------------
MAX_RUN_S        = 300           # ปั๊มทำงานสูงสุดต่อรอบ = 5 นาที  *** ปรับได้ ***
MIN_OFF_S        = 10            # กัน relay สลับถี่ (ต้องหยุดอย่างน้อยเท่านี้ก่อนเริ่มใหม่)  *** ปรับได้ ***
VALVE_SETTLE_MS  = 500           # หน่วงรอวาล์วเปิดจริงก่อนสั่งปั๊มทำงาน (กัน dead-head)  *** ปรับตามสเปควาล์วจริง ***
COOLDOWN_S       = 15 * 60       # พักหลังรด 15 นาที  *** ปรับได้ ***
PUMP_TIMEOUT_MAX = 3             # ชนเพดานเวลา 3 รอบติด → critical ERROR (ล็อกรอ manual reset)  *** ปรับได้ ***

# ช่วงห้ามรด (กันรดกลางแดดจัด) — (ชั่วโมงเริ่ม, ชั่วโมงสิ้นสุด) แบบ 24h
# *** พึ่ง NTP wall-clock; ถ้า NTP ไม่ sync → ข้าม guard นี้ (ยอมให้รด) ***
# ตั้ง None เพื่อปิด guard นี้ทั้งหมด
NO_WATER_WINDOW  = (11, 15)      # ห้ามรด 11:00-15:00  *** ปรับได้ / None=ปิด ***

# -----------------------------------------------------------------------------
# Sensor / ADC calibration (capacitive soil sensor)
#   - ค่าดิบ 0-4095 (12-bit). capacitive: ค่ายิ่งน้อย = ยิ่งเปียก → RAW_DRY > RAW_WET
#   - map: moisture_% = (RAW_DRY - raw_now) / (RAW_DRY - RAW_WET) * 100
# -----------------------------------------------------------------------------
RAW_DRY = 3200   # *** PLACEHOLDER: วัดค่าดิบตอนเซนเซอร์แห้งสนิท (ตากอากาศ) แล้วแก้ ***
RAW_WET = 1400   # *** PLACEHOLDER: วัดค่าดิบตอนจุ่มน้ำ/ดินชุ่ม แล้วแก้ ***

# ขอบเขต raw ที่ถือว่า "เสีย" (sensor_fault) — ค้างสุดขอบ = สายหลุด/ลัดวงจร
ADC_RAW_MIN_VALID = 50           # ต่ำกว่านี้ (รวม 0) = น่าจะลัดวงจร/สายหลุด → fault
ADC_RAW_MAX_VALID = 4045         # สูงกว่านี้ (รวม 4095) = น่าจะขาด/ไม่มีเซนเซอร์ → fault
ADC_SAMPLES       = 7            # จำนวน sample ต่อรอบ เพื่อทำ median filter (เลขคี่)
EMA_ALPHA         = 0.3          # ค่าถ่วงน้ำหนัก EMA (0-1) ยิ่งมาก = ตามค่าจริงไว ยิ่งน้อย = นิ่ง

# -----------------------------------------------------------------------------
# Rain detection (ไม่มี rain sensor → อนุมานจาก soil moisture)
#   ถ้าความชื้นกระโดดขึ้นเอง (ไม่ได้สั่งปั๊ม) เกิน RAIN_JUMP_PCT ในรอบเดียว
#   = น่าจะฝนตก → ตั้ง flag is_raining=True กันรดซ้ำ
# -----------------------------------------------------------------------------
RAIN_JUMP_PCT   = 8              # ความชื้นเด้งขึ้น >= เท่านี้ (%) ในรอบเดียว = สงสัยฝน  *** ปรับได้ ***
RAIN_HOLD_S     = 30 * 60        # คงสถานะ "ฝนตก" หลังตรวจพบ นานเท่านี้  *** ปรับได้ ***

# -----------------------------------------------------------------------------
# Timing ของแต่ละ task (มิลลิวินาที)
# -----------------------------------------------------------------------------
SENSOR_PERIOD_MS      = 2000     # คาบอ่านเซนเซอร์
FSM_PERIOD_MS         = 500      # คาบประมวลผล FSM (task เดียวที่สั่ง actuator)
WATCHDOG_PERIOD_MS    = 2000     # คาบตื่นของ Software Watchdog
MQTT_PERIOD_MS        = 200      # คาบ poll mqtt_as in/out
GC_PERIOD_MS          = 60000    # คาบเรียก gc.collect() + publish freemem
TELEMETRY_PERIOD_MS   = 30000    # คาบ publish telemetry (moisture/temp/pH/EC)
DIAG_PERIOD_MS        = 60000    # คาบ publish diagnostics (uptime/freemem/rssi/...)

# -----------------------------------------------------------------------------
# Watchdog (2 ชั้น)
# -----------------------------------------------------------------------------
# [Hardware WDT — วงจรในชิป RP2040]
HW_WDT_TIMEOUT_MS = 8000         # ใกล้เพดาน ~8.3s ให้ margin มากสุด

# [Software Watchdog — เกณฑ์ staleness ต่อ task (อายุ heartbeat ที่ยังถือว่า "สด")]
# ถ้า task ใด age > เกณฑ์ → ถือว่าค้าง → "ตั้งใจไม่ feed" Hardware WDT → reset
HB_LIMIT_MS = {
    "irrigation_fsm": 3000,      # FSM = critical ที่สุด เกณฑ์เข้มสุด (ตามสเปค)
    "sensor":         5000,      # (ตามสเปค)
    "mqtt":           8000,      # mqtt อาจช้าตอน reconnect — ผ่อนกว่าตัวอื่น
}

# -----------------------------------------------------------------------------
# NTP (wall-clock — ใช้แค่ schedule mode + timestamp telemetry)
#   NTP fail ต้องไม่ทำให้ auto mode ล่ม
# -----------------------------------------------------------------------------
NTP_HOST          = "pool.ntp.org"
NTP_TIMEOUT_MS    = 3000
TZ_OFFSET_S       = 7 * 3600     # ไทย = UTC+7 (ใช้แปลงเวลาเช็ค NO_WATER_WINDOW)

# -----------------------------------------------------------------------------
# I2C sensor (SHT31 / BME280) — temp/humidity ส่งเป็น telemetry
# -----------------------------------------------------------------------------
I2C_FREQ          = 100000
SHT31_ADDR        = 0x44         # 0x45 ถ้า ADDR pin = HIGH
BME280_ADDR       = 0x76         # 0x77 เป็นอีกที่อยู่หนึ่ง

# -----------------------------------------------------------------------------
# ปุ่มกายภาพ manual reset
# -----------------------------------------------------------------------------
BTN_DEBOUNCE_MS   = 50           # เวลากันเด้ง
BTN_HOLD_MS       = 2000         # กดค้างนานกว่านี้ (ตอน ERROR) = ขอ reset
