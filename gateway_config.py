# =============================================================================
# gateway_config.py — ค่าตั้งทั้งหมดของ ESP32-S3 gateway "สวนปลูกฝัน" (prefix: plukfan)
# -----------------------------------------------------------------------------
# *** ไฟล์นี้ "ปรับได้ทั้งหมด" แยกจาก logic เพื่อให้แก้ค่าได้โดยไม่แตะโค้ดหลัก ***
# คู่กับ esp32s3_gateway.py (main firmware). ใช้ secrets.py "ตัวเดียวกับ Pico W"
# เพราะ gateway ต่อ WiFi + MQTT broker เดียวกัน → ไม่มีไฟล์ secrets แยก
#
# จุดที่ต้องแก้ก่อนใช้งานจริง (มาร์ก PLACEHOLDER ใน secrets.py):
#   - WIFI_SSID / WIFI_PASS
#   - MQTT_HOST / MQTT_USER / MQTT_PASS
#   - MQTT_SSL_CA (CA cert ของ broker — แนะนำตั้งจริงเพื่อ verify ตัว broker)
#
# หมายเหตุแพลตฟอร์ม (PSRAM = Pseudo-Static RAM): บอร์ด ESP32-S3-WROOM-1 มี PSRAM
# (R2 = Quad 2 MB / R8 = Octal 8 MB) → flash MicroPython variant ที่เปิด SPIRAM
# (เช่น ESP32_GENERIC_S3-SPIRAM_OCT) เผื่อรับกล้องในเฟสหน้า แม้ skeleton นี้ยังไม่ใช้
# =============================================================================

# -----------------------------------------------------------------------------
# ค่าลับ (WiFi / MQTT credentials) — ดึงจาก secrets.py ที่ถูก gitignore
# ถ้ายังไม่มี secrets.py ให้คัดลอกจาก secrets.example.py แล้วกรอกค่าจริง
# (gateway ใช้ secrets.py "ไฟล์เดียวกับ Pico W" — WiFi + broker ตัวเดียวกัน)
# -----------------------------------------------------------------------------
try:
    import secrets as _secrets
except ImportError:
    _secrets = None
    print("[gateway_config] เตือน: ไม่พบ secrets.py — คัดลอกจาก secrets.example.py แล้วกรอกค่าจริง")


def _secret(name, default):
    # อ่านค่าจาก secrets.py ถ้ามี ไม่งั้น fallback เป็น placeholder (จะ connect ไม่ได้)
    return getattr(_secrets, name, default) if _secrets is not None else default


# -----------------------------------------------------------------------------
# identity ของ node (gateway) — ใช้ประกอบ MQTT topic
# -----------------------------------------------------------------------------
NODE_ID      = "esp32s3-01"      # ชื่อ gateway สำหรับ availability/diagnostics  *** ปรับได้ ***
TOPIC_PREFIX = "plukfan"         # prefix ตาม convention ของระบบ

# -----------------------------------------------------------------------------
# WiFi  *** ค่าจริงอยู่ใน secrets.py ***
# -----------------------------------------------------------------------------
WIFI_SSID = _secret("WIFI_SSID", "PLACEHOLDER_SSID")
WIFI_PASS = _secret("WIFI_PASS", "PLACEHOLDER_PASSWORD")

# -----------------------------------------------------------------------------
# MQTT  *** credentials อยู่ใน secrets.py — บังคับ TLS + username/password ***
# -----------------------------------------------------------------------------
MQTT_HOST        = _secret("MQTT_HOST", "PLACEHOLDER_BROKER")  # จาก secrets.py
MQTT_PORT        = 8883                  # 8883 = MQTT over TLS
MQTT_USER        = _secret("MQTT_USER", "PLACEHOLDER_USER")    # จาก secrets.py
MQTT_PASS        = _secret("MQTT_PASS", "PLACEHOLDER_PASS")    # จาก secrets.py
MQTT_USE_TLS     = True                  # บังคับ TLS (ห้ามปิดในงานจริง)
MQTT_KEEPALIVE_S = 30                    # keepalive (วินาที) — broker ใช้คำนวณ LWT timeout

# CA cert สำหรับ verify broker (self-signed/private CA ใส่ใน secrets.py เป็น PEM bytes; None = default)
# *** แนะนำให้ตั้งค่า cert จริงเพื่อ verify ตัว broker (TLS = Transport Layer Security) ***
MQTT_SSL_CA      = _secret("MQTT_SSL_CA", None)

# connect timeout: ถ้า MQTT connect (รวม TLS handshake) นานเกินนี้ = เน็ตมีปัญหาจริง
# *** ตั้งให้ < HW_WDT_MS เพื่อให้ handshake fit ใน 1 รอบ Hardware WDT (ไม่เกิด reboot loop) ***
MQTT_CONNECT_TIMEOUT_MS = 6000           # *** ปรับได้ (ต้อง < HW_WDT_MS) ***

# -----------------------------------------------------------------------------
# MQTT topic convention (plukfan/) — เฉพาะ topic ที่ gateway ใช้
#   avail/LWT : plukfan/node/<node>/availability   (retained, online/offline)
#   sys/diag  : plukfan/node/<node>/sys/<metric>   (freemem/uptime/rssi/temp)
#   cmd       : plukfan/node/<node>/cmd            (node-level cmd, ห้าม retained)
# *** topic cmd เป็น node-level — ถ้ายังไม่อยู่ในตาราง convention ของสเปก ให้เสนอเพิ่ม ***
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Timing ของแต่ละ task (มิลลิวินาที / วินาที)
# -----------------------------------------------------------------------------
WATCHDOG_WAKE_MS = 2000          # คาบตื่นของ Software Watchdog
HEALTH_TICK_MS   = 1000          # คาบ tick ของ health_task (ตอก heartbeat + สะสม uptime)
HEALTH_PUBLISH_S = 15            # คาบ publish diagnostics (วินาที)
MQTT_POLL_MS     = 200           # คาบ poll สถานะการเชื่อมต่อ MQTT
GC_PERIOD_MS     = 60000         # คาบเรียก gc.collect() (เฝ้า memory leak)

# -----------------------------------------------------------------------------
# Watchdog (2 ชั้น — อิสระต่อกัน ห้ามแทนกัน)
# -----------------------------------------------------------------------------
# [Hardware Watchdog (WDT = Watchdog Timer) — วงจรในชิป ESP32-S3]
HW_WDT_MS = 8000                 # timeout (ms) — ไม่ถูก feed จนถึง 0 → reset ชิป

# [Software Watchdog — เกณฑ์ staleness ต่อ task (อายุ heartbeat ที่ยังถือว่า "สด")]
# ถ้า task ใด age > เกณฑ์ → ถือว่าค้าง → "ตั้งใจไม่ feed" Hardware WDT → ปล่อยให้ reset
HB_LIMIT_MS = {
    "mqtt":   5000,              # mqtt อาจช้าตอน reconnect — ผ่อนกว่า health
    "health": 3000,              # health tick ถี่ → เกณฑ์เข้มกว่า
}
MQTT_STALE_MS   = 5000           # alias อ่านง่าย (ตรงกับ HB_LIMIT_MS["mqtt"])
HEALTH_STALE_MS = 3000           # alias อ่านง่าย (ตรงกับ HB_LIMIT_MS["health"])

# -----------------------------------------------------------------------------
# Backoff (ใช้ตอน boot WiFi-connect retry; steady-state reconnect mqtt_as จัดการเอง)
# -----------------------------------------------------------------------------
BACKOFF_BASE_MS = 1000           # หน่วงเริ่มต้นก่อน retry รอบถัดไป
BACKOFF_MAX_MS  = 16000          # เพดานหน่วง (กัน busy-loop ตอนเน็ตล่มนาน)
WIFI_CONNECT_TIMEOUT_MS = 15000  # รอ WiFi associate นานสุดต่อรอบก่อนยอมแพ้ → reset

# -----------------------------------------------------------------------------
# NTP (Network Time Protocol — wall-clock)
#   *** ต้อง sync ก่อน TLS handshake: ESP32-S3 ไม่มี battery-backed RTC →
#       เวลาเพี้ยน → cert validity (CERT_REQUIRED) ตรวจวันที่ fail ***
#   NTP wall-clock ใช้เฉพาะ field ts ใน payload — ห้ามเอามาทำ timer (timer ใช้ ticks_ms)
# -----------------------------------------------------------------------------
NTP_HOST       = "pool.ntp.org"
NTP_TIMEOUT_MS = 3000            # ครอบ DNS/UDP กันค้าง (blocking call)
NTP_RETRIES    = 3               # ลอง sync กี่รอบก่อนยอมไป handshake (อาจ fail แล้ว reset)
TZ_OFFSET_S    = 7 * 3600        # ไทย = UTC+7 (ใช้แค่กรณีต้องแปลงเวลาแสดงผล)
