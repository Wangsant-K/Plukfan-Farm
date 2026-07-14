# แดชบอร์ดสวนปลูกฝัน (Smart Farm Dashboard) — โหมดสาธิต/Mock

แดชบอร์ดหน้าจอเดียวสำหรับดูสถานะระบบรดน้ำอัตโนมัติของ node `picow_node.py`
ออกแบบให้ **ใช้งานได้ดีบน iPad 9 (10.2") และ iPhone 13** โดยเฉพาะ

ตอนนี้ทำงานด้วย **ข้อมูลจำลอง (mock)** จึงเปิดดูได้ทันทีโดยไม่ต้องมีฮาร์ดแวร์
หรือ MQTT broker จริง

## วิธีเปิดใช้งาน

เปิดไฟล์ `index.html` ด้วยเบราว์เซอร์ได้เลย หรือเสิร์ฟผ่าน static server:

```bash
cd dashboard
python3 -m http.server 8000
# เปิด http://<ip-เครื่อง>:8000 บน iPad/iPhone ที่อยู่ในวง LAN เดียวกัน
```

### ติดตั้งเป็นแอปบนหน้าจอ (iPad 9 / iPhone 13)

เปิดใน Safari → ปุ่ม **Share** → **Add to Home Screen**
จะได้ไอคอนเปิดแบบเต็มจอ (standalone) ผ่าน `manifest.webmanifest` +
meta `apple-mobile-web-app-capable`

## รองรับ iPad 9 และ iPhone 13 อย่างไร

| รายการ | รายละเอียด |
|--------|-----------|
| **Safe area (notch/home bar)** | `viewport-fit=cover` + `env(safe-area-inset-*)` เนื้อหาไม่ถูก notch ของ iPhone 13 บัง |
| **Touch target** | ปุ่ม/แท็บทุกตัว ≥ 44×44px ตาม Apple HIG |
| **ไม่มี hover-only** | ใช้ `:active` ให้ feedback แทน hover (จอสัมผัสไม่มี hover) |
| **กันซูมเองตอนแตะ** | `maximum-scale=1` กัน Safari iOS ซูมตอนแตะ |
| **Responsive layout** | 1 คอลัมน์บน iPhone, 2–3 คอลัมน์บน iPad (portrait/landscape) |
| **จอแนวนอนเตี้ย** | มี media query เฉพาะ iPhone 13 landscape (`max-height:480px`) |
| **Reduce Motion** | เคารพ `prefers-reduced-motion` ของ iOS |
| **ฟอนต์ไทย** | ใช้ฟอนต์ระบบ iOS (`-apple-system`, `Noto Sans Thai`, `Sukhumvit Set`) |

ทดสอบแล้วไม่มี horizontal overflow และไม่มี console error ที่ขนาดจอ:
iPhone 13 (390×844 / 844×390) และ iPad 9 (810×1080 / 1080×810)

## ข้อมูลที่แสดง (ตรงกับ firmware)

- **FSM 5 สถานะ**: INIT → IDLE → WATERING → COOLDOWN → ERROR
  (ปั๊มทำงานเฉพาะ WATERING — safe-by-structure เหมือน `picow_node.py`)
- **ความชื้นในดิน** เทียบเกณฑ์ low/high ตามโซน (จาก `THRESHOLDS_ZONE`)
- **เซนเซอร์**: อุณหภูมิ / ความชื้นอากาศ / **pH (กรด–ด่าง 0–14)** / EC (raw)
  - pH: firmware ส่งค่าดิบ แล้ว map เป็น pH ที่ฝั่งแสดงผล + ป้ายกรด/ด่าง (กรดจัด/กรดอ่อน/เป็นกลาง/ด่างอ่อน/ด่างจัด)
- **Fail-safe guard**: น้ำในถัง (float switch), ตรวจฝน, สถานะเซนเซอร์, ช่วงห้ามรด
- **Diagnostics**: node id, uptime, free RAM, RSSI, สาเหตุ reset ล่าสุด
- **ปุ่ม Manual Reset** โผล่เฉพาะตอน ERROR (จำลองคำสั่ง `system/cmd {"action":"reset"}`)
- เลือกดูได้หลายโซน (veggie/banana) ผ่านแท็บด้านบน

### การ์ด Gateway (ESP32-S3 · `esp32s3-01`)

gateway เป็น **คนละชนิด node** กับ Pico W — ไม่มีระบบรดน้ำ/เซนเซอร์ดิน แต่เป็น backbone
ที่ต่อ MQTT over TLS การ์ดนี้สะท้อน `esp32s3_gateway.py` / `gateway_config.py`:

- **Availability**: online / offline (retained + LWT) แสดงเป็น badge มุมขวา
- **Diagnostics** (`sys/{uptime,freemem,rssi,temp}`): uptime, free RAM, WiFi RSSI, อุณหภูมิชิป
- **Dual watchdog**:
  - Hardware WDT (`HW_WDT_MS` = 8000 ms) — สถานะ "เลี้ยงปกติ"
  - Software WDT ราย task (`mqtt` เกณฑ์ 5000 ms / `health` เกณฑ์ 3000 ms) — แสดงอายุ heartbeat ว่ายัง "สด"
- **กล้อง**: เป็น stub (ยังไม่รองรับ) ตรงกับ `capture_stub()` ใน firmware
- **ปุ่มคำสั่งสาธิต** (จำลอง `plukfan/node/esp32s3-01/cmd`):
  - **Ping** → ตอบ `pong` พร้อม RTT จำลอง
  - **Capture** → ตอบ `not_implemented` (กล้องยังเป็น stub)
  - **Reboot** → จำลอง `machine.reset()` → gateway บูตใหม่ (uptime กลับเป็น 0, reset = `software_reset`)

### ลองเล่นในโหมดสาธิต
- แตะการ์ด **"น้ำในถัง"** เพื่อจำลองถังน้ำหมด → ระบบเข้า ERROR → กด Manual Reset
- แตะการ์ด **"ตรวจพบฝน"** เพื่อจำลองฝนตก → ระบบงดรดน้ำชั่วคราว
- แตะการ์ด **"เซนเซอร์"** เพื่อจำลองเซนเซอร์ผิดปกติ → ระบบเข้า ERROR (sensor_fault); แตะอีกครั้งให้กลับมาปกติ
- ปุ่ม ⏸/▶ มุมขวาบน หยุด/เริ่ม การจำลองข้อมูล

## ต่อกับข้อมูลจริง (MQTT) ภายหลัง

โค้ดแยกชั้น **แหล่งข้อมูล** (`js/mock-data.js`) ออกจาก **การแสดงผล** (`js/app.js`)
อย่างชัดเจน — `app.js` รับข้อมูลผ่าน `PlukfanMock.onUpdate(callback)` เท่านั้น

เมื่อพร้อมต่อของจริง ให้สร้าง adapter ที่ subscribe MQTT-over-WebSocket
(เช่น MQTT.js ต่อพอร์ต WSS ของ broker) ตาม topic convention ใน `config.py`:

```
plukfan/<zone>/<device>/<channel>     # telemetry: soil/moisture, air/temp, ...
plukfan/<zone>/<actuator>/state       # pump/state (retained)
plukfan/node/<node>/availability      # online/offline (LWT)
plukfan/node/<node>/sys/<metric>      # uptime/freemem/rssi/last_error
```

แล้วแปลง payload เป็นรูปแบบ state เดียวกับ mock ก่อนเรียก callback
โครงสร้าง UI ไม่ต้องแก้
