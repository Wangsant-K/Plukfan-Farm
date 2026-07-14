# 🌱 สวนปลูกฝัน — IoT Smart Farm Node (plukfan)

Firmware **MicroPython** สำหรับ **Raspberry Pi Pico W (RP2040)** ทำหน้าที่เป็น node รวม sensor + actuator ต่อโซนปลูก (ปั๊ม + วาล์ว + เซนเซอร์) ควบคุมการรดน้ำอัตโนมัติตามความชื้นดิน และสื่อสารกับ backend ผ่าน **MQTT over TLS**

> **ปรัชญาออกแบบ: SAFETY ก่อน FEATURE เสมอ**
> ทุก error path ลงเอยที่ "ปั๊มหยุดทำงาน" + แจ้งเตือน — ปั๊มทำงานได้เฉพาะใน state `WATERING` เท่านั้น (safe-by-structure)

---

## ภาพรวมสถาปัตยกรรม

ระบบทำงานแบบ **cooperative async** ด้วย `uasyncio` มีทั้งหมด 8 task โดยแบ่งเป็น critical task (มี heartbeat เฝ้าโดย watchdog) และ task เสริม

| Task | หน้าที่ | Critical (heartbeat) |
|---|---|---|
| `sensor_task` | อ่านเซนเซอร์ + กรอง noise (median + EMA) + ตรวจฝน | ✅ (5000 ms) |
| `irrigation_fsm_task` | **task เดียวที่สั่ง actuator** — เดิน FSM 5 สถานะ + อ่าน float switch สดทุกรอบ (เสริมด้วย IRQ latch จับน้ำหลุดระดับระหว่างรอบ) | ✅ (3000 ms) |
| `mqtt_task` | poll สถานะการเชื่อมต่อ MQTT/WiFi อัปเดต flag ให้ FSM | ✅ (8000 ms) |
| `watchdog_task` | อ่าน heartbeat ทุก task → feed Hardware WDT เฉพาะเมื่อทุก task สด | — |
| `button_task` | ปุ่มกายภาพ manual reset (fallback ตอนเน็ตล่ม) | — |
| `telemetry_task` | publish moisture / temp / humid / pH / EC | — |
| `diag_task` | publish uptime / freemem / mode / rssi | — |
| `gc_task` | `gc.collect()` เป็นระยะ กัน memory leak | — |

EMA (ย่อมาจาก Exponential Moving Average คือค่าเฉลี่ยเคลื่อนที่แบบถ่วงน้ำหนักแบบ exponential) ใช้ smoothing ค่าความชื้นให้นิ่งกว่าการใช้ median อย่างเดียว

FSM (ย่อมาจาก Finite State Machine คือเครื่องสถานะจำกัด — โครงสร้างที่ระบบอยู่ได้ทีละสถานะ และเปลี่ยนสถานะตามเงื่อนไขที่กำหนด) เป็นหัวใจของ control logic ดูรายละเอียดในหัวข้อ [State Machine](#state-machine-fsm)

---

## โครงสร้างไฟล์

```
สวนปลูกฝัน/
├── picow_node.py         # firmware หลัก (actuator, sensor, FSM, MQTT, watchdog)
├── config.py             # ค่าตั้งทั้งหมด แยกจาก logic (ปรับได้โดยไม่แตะโค้ดหลัก)
├── secrets.py            # ค่าลับ WiFi/MQTT (gitignore — ห้าม commit)
├── secrets.example.py    # เทมเพลตให้คัดลอกไปสร้าง secrets.py
├── README.md             # ไฟล์นี้
├── wiring_diagram.mermaid# แผนผังการต่อวงจร
└── state_machine.mermaid # flowchart ของ FSM
```

---

## การติดตั้ง (Setup)

1. **แฟลช MicroPython** ลง Pico W (เวอร์ชันรองรับ `uasyncio`, `machine.WDT`)
2. **ติดตั้ง dependency** — ต้องมี `mqtt_as.py` (async MQTT ของ Peter Hinch) วางไว้บนบอร์ด
   > ถ้าไม่มี `mqtt_as` firmware ยังบูตได้แต่จะทำงานแบบ offline (ไม่มี MQTT)
3. **สร้างไฟล์ค่าลับ** — คัดลอก `secrets.example.py` เป็น `secrets.py` แล้วกรอกค่าจริง:
   ```
   WIFI_SSID, WIFI_PASS
   MQTT_HOST, MQTT_USER, MQTT_PASS
   MQTT_SSL_CA (แนะนำ — CA cert ไว้ verify broker)
   ```
4. **Calibrate เซนเซอร์ดิน** (สำคัญมาก) — แก้ค่าใน `config.py`:
   - `RAW_DRY` = ค่าดิบตอนเซนเซอร์แห้งสนิท (ตากอากาศ)
   - `RAW_WET` = ค่าดิบตอนจุ่มน้ำ / ดินชุ่ม
   - capacitive sensor: ค่ายิ่งน้อย = ยิ่งเปียก → `RAW_DRY > RAW_WET`
5. **ตรวจ polarity ของ float switch** ให้ `FLOAT_OK_LEVEL` ตรงกับการต่อจริง
6. อัปโหลด `picow_node.py` + `config.py` แล้วตั้งให้รันตอนบูต (เปลี่ยนชื่อเป็น `main.py` หรือเรียกจาก `main.py`)

---

## การต่อวงจร (Wiring)

ดูแผนผังเต็มใน [`wiring_diagram.mermaid`](wiring_diagram.mermaid)

| Pin | ขา | อุปกรณ์ | ทิศทาง | หมายเหตุ |
|---|---|---|---|---|
| `PIN_SOIL` | GP26 / ADC0 | Soil moisture (capacitive) | อ่าน analog | ใช้ใน **control** |
| `PIN_PH` | GP27 / ADC1 | pH sensor | อ่าน analog | telemetry เท่านั้น |
| `PIN_EC` | GP28 / ADC2 | EC sensor | อ่าน analog | telemetry เท่านั้น |
| `PIN_I2C_SDA` | GP2 | SHT31 / BME280 (SDA) | I2C1 | temp/humid telemetry |
| `PIN_I2C_SCL` | GP3 | SHT31 / BME280 (SCL) | I2C1 | addr 0x44 |
| `PIN_FLOAT_SWITCH` | GP15 | Float switch | อ่าน (pull-up ภายใน) | guard สำคัญสุด — น้ำในถัง |
| `PIN_PUMP` | GP16 | Relay → ปั๊ม | ควบคุม | **active-LOW + external pull-up** |
| `PIN_VALVE_DRIP` | GP17 | Relay → วาล์วน้ำหยด | ควบคุม | **active-LOW + external pull-up** |
| `PIN_VALVE_SPRINK` | GP18 | Relay → วาล์วสปริงเกอร์ | ควบคุม | **active-LOW + external pull-up** |
| `PIN_RESET_BTN` | GP14 | ปุ่ม manual reset | อ่าน (pull-up ภายใน) | กดลง GND |

> ⚠️ **ขา relay ทุกตัวต้องมี external pull-up บนบอร์ด** เพื่อให้ตอน Hardware WDT reset (GPIO กลายเป็น hi-Z) relay คลายเป็น OFF จริง = การันตี "ปั๊มหยุดทำงาน" แม้เฟิร์มแวร์พังกลางคัน

---

## Relay logic (active-LOW)

| เขียนค่า GPIO | relay | อุปกรณ์ |
|---|---|---|
| `0` | ON | **ทำงาน** |
| `1` | OFF | **หยุดทำงาน (SAFE)** |

ตั้งค่าผ่าน `RELAY_ACTIVE_LOW = True` ใน `config.py`

---

## State Machine (FSM)

ดู flowchart เต็มใน [`state_machine.mermaid`](state_machine.mermaid)

5 สถานะ: `INIT → IDLE → WATERING → COOLDOWN → ERROR`
ปั๊มทำงานเฉพาะใน `WATERING`; อีก 4 สถานะ = ปั๊มหยุดทำงานเสมอ

**เงื่อนไขเริ่มรด (IDLE → WATERING)** — ต้องครบทุกข้อ:
- ค่าความชื้นใช้ได้ (ไม่ fault, ไม่ None) และ `moisture < LOW`
- ไม่ได้กำลังฝนตก (`!is_raining`)
- น้ำในถังพอ (`float_ok`)
- หยุดปั๊มมาแล้วนานพอ (`≥ COOLDOWN_S` และ `≥ MIN_OFF_S`)
- ไม่อยู่ในช่วงห้ามรด (`NO_WATER_WINDOW`, พึ่ง NTP)
- ผ่านการเช็ค float **สดซ้ำ** ก่อนเปิดวาล์ว และอีกครั้ง**หลัง valve settle** ก่อนจ่ายไฟปั๊ม — น้ำหลุดระดับระหว่างนั้น = ยกเลิก คง IDLE (ปิดหน้าต่าง dry-run ช่วง settle)

**เงื่อนไขหยุดรด (WATERING → COOLDOWN)**:
- สำเร็จ: `moisture ≥ HIGH` (reset ตัวนับ timeout)
- ชนเพดานเวลา: `run_time ≥ MAX_RUN_S` (นับ timeout; ถ้าวนซ้ำ `≥ PUMP_TIMEOUT_MAX` → critical ERROR)

**Safety override** (เช็คทุกรอบก่อน logic ปกติ ในสถานะ IDLE/WATERING/COOLDOWN):
- `!mqtt_connected` → ERROR (`network_loss`)
- `!float_ok` → ERROR (`tank_empty`)
- `sensor_fault` → ERROR

**การออกจาก ERROR**:
- **transient error** (ไม่ critical): เหตุหายเอง (mqtt + float + sensor ปกติ) → auto กลับ IDLE
- **critical error** (เช่น pump_timeout วนซ้ำ, FSM exception): ล็อก รอ **manual reset** เท่านั้น
- manual reset: ผ่าน MQTT (`system/cmd`) หรือปุ่มกายภาพ (กดค้าง >2s — ปุ่มข้าม guard `mqtt_connected` ได้)

---

## เกณฑ์ความชื้น (Threshold)

เลือกโหมดผ่าน `THRESHOLD_MODE` = `"zone"` (ตามโซน) หรือ `"crop"` (ตามชนิดพืช)

| โซน | low (เริ่มรด) | high (หยุดรด) |
|---|---|---|
| `veggie` (กะเพรา) | 60% | 70% |
| `banana` | 60% | 75% |

---

## MQTT Topic Convention

Prefix: `plukfan/`

| ประเภท | รูปแบบ topic | QoS / retain |
|---|---|---|
| telemetry | `plukfan/<zone>/<device>/<channel>` | QoS0 |
| command | `plukfan/<zone>/<actuator>/cmd` | QoS1, ห้าม retain |
| state | `plukfan/<zone>/<actuator>/state` | retained (closed-loop แยกจาก cmd) |
| system | `plukfan/<zone>/system/cmd` | manual reset `{"action":"reset"}` |
| availability / LWT | `plukfan/node/<node>/availability` | retained (`online`/`offline`) |
| sys / diag | `plukfan/node/<node>/sys/<metric>` | uptime, freemem, rssi, last_error |

LWT (ย่อมาจาก Last Will and Testament คือข้อความที่ broker จะ publish แทน node โดยอัตโนมัติเมื่อ node หลุดการเชื่อมต่อกะทันหัน) ใช้ประกาศ `offline` ให้ backend รู้ทันทีเมื่อ node หาย

---

## Fail-safe 6 ชั้น

ระบบออกแบบให้ทุกความล้มเหลวลงเอยที่ "ปั๊มหยุดทำงาน":

1. **Default off ตอนบูต** — ตั้ง actuator = SAFE ตั้งแต่ constructor และก่อนเข้า event loop
2. **Software Watchdog** — แต่ละ critical task ตอกบัตร heartbeat; ถ้า task ใดค้างเกินเกณฑ์ → ตั้งใจไม่ feed WDT
3. **Hardware WDT** — วงจรในชิป RP2040 (timeout ~8s) reset บอร์ดถ้าไม่ถูก feed
4. **`MAX_RUN_S` cutoff** — ปั๊มทำงานเกินเวลาสูงสุด/รอบ → ตัดทันที + นับ timeout
5. **Float switch** — น้ำในถังไม่พอ → ERROR (`tank_empty`) ทันที
6. **Network-loss → SAFE** — WiFi/MQTT หลุด → ERROR (`network_loss`)

เสริม: global exception handler + `try/finally` รอบ `main()` บังคับ `all_safe()` + `machine.reset()` เมื่อเกิด uncaught exception

WDT (ย่อมาจาก Watchdog Timer คือตัวนับถอยหลังในฮาร์ดแวร์ที่จะ reset บอร์ดโดยอัตโนมัติถ้าโปรแกรมไม่มา "feed" ภายในเวลาที่กำหนด — ใช้กู้ระบบเมื่อ firmware ค้าง)

---

## ⚠️ ข้อควรระวังด้านความปลอดภัย (Hardware)

โค้ดชุดนี้ควบคุม **ปั๊มน้ำจริงผ่าน relay** — ก่อนทดสอบและแก้ไขให้ระวังดังนี้:

- **ทดสอบแบบ dry-run ก่อนเสมอ** — ยังไม่ต่อปั๊ม/วาล์วจริง ให้ดู LED บน relay board ยืนยัน logic ก่อน
- **ยืนยัน external pull-up ที่ขา relay** ให้ครบทุกตัวก่อนจ่ายไฟ ไม่งั้นตอน reset ปั๊มอาจค้างทำงาน
- **แยกไฟเลี้ยงปั๊ม/วาล์วออกจากไฟ Pico** — อย่าดึงโหลดปั๊มผ่านราง 3V3/5V ของบอร์ด และใช้ flyback diode / opto-isolation ตามสเปค relay
- **ห้ามลบเงื่อนไขที่บังคับให้ปั๊มทำงานเฉพาะ state `WATERING`** และห้ามแตะ safety override โดยไม่ทดสอบครบทุก path
- **ตรวจ polarity ของ float switch จริง** ก่อนใช้งาน — ถ้ากลับด้าน ระบบอาจรดจนถังแห้ง
- **ตั้ง `MAX_RUN_S` ให้เหมาะกับปริมาณน้ำและพืชจริง** เพื่อกันน้ำท่วม/ปั๊มเดินแห้ง
- ควรมี **คนดูแลตอนทดสอบครั้งแรก** และเข้าถึงสวิตช์ตัดไฟหลักได้ทันที
