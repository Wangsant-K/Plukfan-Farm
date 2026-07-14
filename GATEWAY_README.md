# ESP32-S3 Gateway — "สวนปลูกฝัน" (plukfan)

Firmware ฝั่ง **gateway** ประจำสวน (node id = `esp32s3-01`) เขียนด้วย **MicroPython + MQTT**
คู่กับ node ฝั่ง Pico W (`picow_node.py`).

## สโคปเฟสนี้ (Phase 1 — Connectivity + Health Skeleton)

พิสูจน์ว่า backbone รันบน ESP32-S3 ได้จริง — **ยังไม่มี feature การเกษตร**:

| | สถานะ |
|---|---|
| WiFi + MQTT over TLS (`CERT_REQUIRED`) | ✅ |
| NTP sync ก่อน TLS handshake | ✅ |
| ประกาศตัว `online` (retained) + LWT `offline` | ✅ |
| Diagnostics `sys/{freemem,uptime,rssi,temp}` ทุก 15s | ✅ |
| Dual watchdog (Software + Hardware) | ✅ |
| กล้อง (camera) | ❌ stub เปล่า + TODO เท่านั้น |
| Actuator (ปั๊ม/วาล์ว) | ❌ gateway ไม่มี control authority |
| Farm sensor (ดิน/อากาศ) | ❌ เป็นงานของ Pico W |

> **ความเป็นอิสระ:** gateway ล่ม **ต้องไม่กระทบระบบรดน้ำ** — Pico W รัน irrigation logic
> ฉุกเฉินในเครื่องเองได้ firmware นี้จึงไม่ publish อะไรที่ node อื่นต้องรอ

## ไฟล์

- `esp32s3_gateway.py` — main firmware (boot + 3 task + dual watchdog + LWT + diagnostics + cmd stub)
- `gateway_config.py` — ค่าตั้งทั้งหมด (timing / topic / WDT / NTP)
- `secrets.py` — **ใช้ไฟล์เดียวกับ Pico W** (WiFi + broker ตัวเดียวกัน) คัดลอกจาก `secrets.example.py`
- ต้องมี `mqtt_as.py` (async MQTT ของ Peter Hinch) + `uasyncio` บนบอร์ด

## การ flash (สำคัญ: ตรวจ PSRAM ก่อน)

บอร์ดเป้าหมาย: **ESP32-S3-WROOM-1** (มี PSRAM = Pseudo-Static RAM)

1. **ตรวจชนิด PSRAM** ให้ตรงกับ firmware variant:
   - **R2** = Quad 2 MB → variant Quad SPIRAM
   - **R8** = Octal 8 MB → variant Octal SPIRAM
   - แนะนำใช้ variant ที่เปิด SPIRAM เช่น **`ESP32_GENERIC_S3-SPIRAM_OCT`** (Octal)
     เพื่อให้พร้อมรับกล้องในเฟสหน้า แม้ skeleton นี้ยังไม่ใช้ PSRAM หนัก
2. flash MicroPython variant ที่เลือก
3. คัดลอกไฟล์ลงบอร์ด: `mqtt_as.py`, `gateway_config.py`, `esp32s3_gateway.py`, `secrets.py`
4. ตั้ง `esp32s3_gateway.py` ให้รันตอนบูต (เช่น เรียกจาก `main.py` หรือเปลี่ยนชื่อเป็น `main.py`)

## ลำดับบูต (Boot Sequence)

1. boot — **ไม่ขับ GPIO output** (gateway ไม่มี actuator)
2. เปิด **Hardware WDT** (Watchdog Timer, 8000 ms) ทันทีหลังเข้า `main`
3. ต่อ WiFi เอง (backoff retry; feed WDT ระหว่างรอ)
4. **sync NTP ก่อน TLS handshake** — ESP32-S3 ไม่มี battery-backed RTC →
   เวลาเพี้ยน → `CERT_REQUIRED` ตรวจวันที่ cert แล้ว fail
5. MQTT connect (TLS, `CERT_REQUIRED`, CA cert จาก `secrets.py`) — ตั้ง LWT=`offline` ตอน config
6. ประกาศ `availability=online` (retained) + subscribe `cmd`
7. spawn 3 task → `asyncio.run`

## Watchdog 2 ชั้น (อิสระ ห้ามแทนกัน)

- **Hardware Watchdog** = `machine.WDT(timeout=8000)` วงจรในชิป — ไม่ถูก feed จนถึง 0 → reset ชิป
  (จับ "ระบบค้างทั้งก้อน / event loop hang")
- **Software Watchdog** = `watchdog_task` ตื่นทุก 2000 ms อ่าน heartbeat ของแต่ละ task →
  **สดครบทุก task ค่อย** `wdt.feed()`; ถ้า task ใดค้างเกิน staleness → **ไม่ feed** →
  ปล่อยให้ Hardware WDT reset (staleness: mqtt 5000 ms / health 3000 ms)

## MQTT Topics (convention `plukfan/`)

| ประเภท | Topic | retained |
|---|---|---|
| Availability / LWT | `plukfan/node/esp32s3-01/availability` | ✅ (`online`/`offline`) |
| Diagnostics | `plukfan/node/esp32s3-01/sys/<metric>` | ไม่ (`freemem`,`uptime`,`rssi`,`temp`) |
| Command | `plukfan/node/esp32s3-01/cmd` | ❌ ห้าม retained |

- Diagnostics payload = JSON มี field `ts` (epoch หลัง NTP) เช่น `{"v": 1234, "ts": ...}`
- Command รองรับ: `ping` (ตอบ pong), `reboot` (`machine.reset`), `capture` (→ stub, ตอบ `not_implemented`)
  ส่งได้ทั้ง JSON `{"cmd":"ping"}` และ plain string `ping`

> **หมายเหตุ:** topic `plukfan/node/<id>/cmd` เป็น node-level — ถ้ายังไม่อยู่ในตาราง
> convention ของสเปก ถือเป็นข้อ **เสนอเพิ่ม** (TODO) ไม่ใช่ตัดสินเอง

## สิ่งที่ยังไม่ทำ (เฟสหน้า)

- **กล้อง (camera):** มี `capture_stub()` ที่ `raise NotImplementedError` เป็น extension point
  เท่านั้น — ยังไม่มีโค้ดกล้องจริง และ **ไม่ import โมดูล `camera`**
  - MicroPython official **ไม่มีโมดูล `camera` ในตัว** → เฟสกล้องต้องใช้ custom build ที่ผูก
    driver `esp32-camera` ของ Espressif (เช่น `lemariva/micropython-camera-driver` หรือ `mp_camera`)
  - PSRAM ใช้พักเฟรมภาพ/โมเดล AI → ต้อง flash variant ที่เปิด SPIRAM

## ทดสอบ (on-device)

- ดู log บูต: WiFi ต่อได้ → `[ntp] sync สำเร็จ` → MQTT connect → `online (retained)`
- subscribe `plukfan/node/esp32s3-01/sys/#` → เห็น `freemem/uptime/rssi/temp` ทุก ~15 s
- ดับ WiFi → ต้อง reconnect เองแบบ backoff (mqtt_as) ไม่ค้าง ไม่ busy-loop
- publish ไป `plukfan/node/esp32s3-01/cmd` payload `ping` / `capture` → เห็นการตอบกลับ
