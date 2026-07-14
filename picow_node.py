# =============================================================================
# picow_node.py — MicroPython firmware สำหรับ Raspberry Pi Pico W (RP2040)
# ระบบ IoT Smart Farm "สวนปลูกฝัน" (prefix: plukfan)
# node รวม sensor + actuator ต่อโซน (ปั๊ม + วาล์ว + เซนเซอร์)
# -----------------------------------------------------------------------------
# ปรัชญาออกแบบ: SAFETY ก่อน FEATURE เสมอ
#   ทุก error path ลงเอยที่ "ปั๊มหยุดทำงาน" + แจ้งเตือน
#   ปั๊มทำงานได้เฉพาะใน state WATERING เท่านั้น (safe-by-structure)
#
# Fail-safe 6 ชั้น (รายละเอียดท้ายไฟล์):
#   1) default off ตอนบูต  2) Software Watchdog  3) Hardware WDT
#   4) max_run_s cutoff     5) float switch       6) network-loss → SAFE
# -----------------------------------------------------------------------------
# *** คำศัพท์ log/comment: ใช้ "ปั๊มทำงาน" / "ปั๊มหยุดทำงาน" เท่านั้น ***
# =============================================================================

import sys
import gc
import json
import machine
import network
from machine import Pin, ADC, I2C, WDT, reset_cause
import uasyncio as asyncio
from utime import ticks_ms, ticks_diff, ticks_add, time, localtime

import config as cfg

try:
    # mqtt_as = async MQTT ของ Peter Hinch (จัดการ WiFi/MQTT reconnect + backoff ให้)
    from mqtt_as import MQTTClient, config as mqtt_base
except ImportError:
    MQTTClient = None
    mqtt_base = {}


# =============================================================================
# ส่วนที่ 0: ACTUATOR — ตัวเดียวที่แตะ relay ทางกายภาพ
# relay active-LOW: เขียน 0 = ทำงาน, เขียน 1 = หยุดทำงาน (SAFE)
# *** สำคัญ: ขา relay มี external pull-up → ตอน WDT reset GPIO เป็น hi-Z
#     relay จะคลายเป็น OFF เอง = การันตีปั๊มหยุดทำงานแม้เฟิร์มแวร์พัง ***
# =============================================================================
def _level(active):
    # แปลง "ต้องการให้ทำงานไหม" → ระดับ logic ของขา (active-LOW)
    if cfg.RELAY_ACTIVE_LOW:
        return 0 if active else 1
    return 1 if active else 0


class Actuators:
    def __init__(self):
        # init ขาเป็น OUTPUT พร้อมตั้งค่าเริ่มต้น = OFF ทันที (value ใน constructor)
        # ตั้ง value ตั้งแต่ตอน Pin() เพื่อลดช่วง glitch ระหว่างบูต
        off = _level(False)
        self.pump = Pin(cfg.PIN_PUMP, Pin.OUT, value=off)
        self.valve_drip = Pin(cfg.PIN_VALVE_DRIP, Pin.OUT, value=off)
        self.valve_sprink = Pin(cfg.PIN_VALVE_SPRINK, Pin.OUT, value=off)
        self._pump_on = False

    def all_safe(self):
        # บังคับทุก actuator เข้าสถานะ SAFE: ปั๊มหยุดทำงาน + วาล์วปิดทั้งหมด
        self.pump.value(_level(False))
        self.valve_drip.value(_level(False))
        self.valve_sprink.value(_level(False))
        self._pump_on = False

    async def pump_run(self):
        # สั่ง "ปั๊มทำงาน" — เปิดวาล์วน้ำหยดก่อน (โซนนี้รดแบบ drip เป็นหลัก)
        # แล้วหน่วงรอ valve settle ให้วาล์วเปิดจริงก่อนค่อยสั่งปั๊ม
        # กัน dead-head (ปั๊มดันน้ำใส่วาล์วที่ยังไม่เปิด) — ห้ามเปิดสองขาพร้อมกัน
        self.valve_drip.value(_level(True))
        await asyncio.sleep_ms(cfg.VALVE_SETTLE_MS)
        self.pump.value(_level(True))
        self._pump_on = True

    def pump_stop(self):
        # สั่ง "ปั๊มหยุดทำงาน" — ปิดปั๊มก่อน แล้วค่อยปิดวาล์ว
        self.pump.value(_level(False))
        self.valve_drip.value(_level(False))
        self.valve_sprink.value(_level(False))
        self._pump_on = False

    @property
    def pump_is_on(self):
        return self._pump_on


# =============================================================================
# ส่วนที่ 1: SHARED STATE — task ทุกตัวคุยกันผ่าน object นี้ (ไม่เรียกตรง)
# =============================================================================
class State:
    def __init__(self):
        # --- heartbeat ("ตอกบัตร"): แต่ละ task เขียน ticks_ms() ท้ายลูปทุกรอบ ---
        self.hb = {
            "sensor": ticks_ms(),
            "irrigation_fsm": ticks_ms(),
            "mqtt": ticks_ms(),
        }

        # --- ค่าเซนเซอร์ที่กรอง noise แล้ว ---
        self.moisture = None          # ความชื้น % (None = ยังไม่มีค่า / fault)
        self.moisture_ema = None      # ค่า EMA สะสม (ใช้ smoothing)
        self.prev_moisture = None     # ค่ารอบก่อน (ใช้ตรวจ rain jump)
        self.temp = None              # อุณหภูมิ (telemetry)
        self.humid = None             # ความชื้นอากาศ (telemetry)
        self.ph = None                # pH (telemetry เท่านั้น — ไม่ใช้ใน control)
        self.ec = None                # EC (telemetry เท่านั้น — ไม่ใช้ใน control)
        self.sensor_fault = False     # True = ค่าดิบเสีย → ห้าม map แล้วสั่งปั๊ม

        # --- สถานะ guard / safety ---
        self.float_ok = False         # น้ำในถังพอไหม (FSM อ่านสดทุกรอบ — writer เดียว)
        self.float_fault_latch = False  # IRQ latch: float เด้งเป็น "ไม่ ok" ระหว่างรอบ FSM
        self.is_raining = False       # อนุมานฝนจาก moisture jump
        self.rain_until = 0           # ticks_ms ที่ flag ฝนจะหมดอายุ
        self.mqtt_connected = False   # สถานะการเชื่อมต่อ MQTT/WiFi

        # --- FSM ---
        self.fsm_state = "INIT"
        self.last_stop = ticks_ms()   # ticks_ms ครั้งล่าสุดที่ปั๊มหยุดทำงาน
        self.water_start = 0          # ticks_ms ตอนเริ่ม WATERING
        self.cooldown_start = 0       # ticks_ms ตอนเข้า COOLDOWN
        self.pump_timeout_count = 0   # นับครั้งชนเพดาน max_run_s ติดกัน
        self.error_reason = ""        # เหตุผลที่เข้า ERROR
        self.error_critical = False   # True = critical (รอ manual reset เท่านั้น)

        # --- flag จาก callback / ปุ่ม (callback แค่ตั้ง flag ไม่สั่งปั๊ม) ---
        self.reset_requested = False  # ขอ manual reset (จาก MQTT หรือปุ่ม)
        self.reset_from_button = False  # True = มาจากปุ่มกายภาพ (ข้าม guard mqtt ได้)

        # --- เวลา / ระบบ ---
        self.ntp_ok = False           # NTP sync สำเร็จไหม (มีผลแค่ schedule guard)
        self.last_reset_cause = ""    # สาเหตุ reboot รอบนี้ (debug reboot loop)
        self.last_error = ""          # ข้อความ error ล่าสุด (publish ออก sys/)


st = State()
act = Actuators()
client = None  # MQTTClient (สร้างใน main)


# =============================================================================
# ส่วนที่ 2: MQTT topic helper
# =============================================================================
def t_tele(device, channel):
    return "{}/{}/{}/{}".format(cfg.TOPIC_PREFIX, cfg.ZONE, device, channel)

def t_state(actuator):
    return "{}/{}/{}/state".format(cfg.TOPIC_PREFIX, cfg.ZONE, actuator)

def t_cmd(actuator):
    return "{}/{}/{}/cmd".format(cfg.TOPIC_PREFIX, cfg.ZONE, actuator)

def t_sys(metric):
    return "{}/node/{}/sys/{}".format(cfg.TOPIC_PREFIX, cfg.NODE_ID, metric)

AVAIL_TOPIC  = "{}/node/{}/availability".format(cfg.TOPIC_PREFIX, cfg.NODE_ID)
SYSTEM_CMD   = "{}/{}/system/cmd".format(cfg.TOPIC_PREFIX, cfg.ZONE)
# หมายเหตุ: ไม่มี PUMP_CMD โดยตั้งใจ — pump ไม่รับคำสั่งผ่าน MQTT
# (safe-by-structure: ปั๊มทำงานเฉพาะ state WATERING ที่ FSM คุมตัวเดียว)


def _now_ts():
    # timestamp สำหรับ payload — epoch วินาที (wall-clock) เท่านั้น
    # NTP ยังไม่ sync → คืน None (JSON null) ให้ backend ใช้เวลารับแทน
    # *** ห้ามคืน ticks_ms ปน: คนละหน่วย/คนละฐาน backend แยกไม่ออก ***
    if st.ntp_ok:
        try:
            return time()
        except Exception:
            pass
    return None


async def pub(topic, payload, retain=False, qos=0):
    # publish แบบปลอดภัย — ครอบ exception ไม่ให้ MQTT ล้มทำ task ตาย
    if client is None or not st.mqtt_connected:
        return
    try:
        if not isinstance(payload, (bytes, str)):
            payload = json.dumps(payload)
        await client.publish(topic, payload, retain=retain, qos=qos)
    except Exception as e:
        print("[mqtt] publish fail:", topic, e)


async def pub_json(topic, value, retain=False, qos=0):
    # ห่อ value + ts เป็น JSON ตาม convention (ทุก payload มี field ts)
    await pub(topic, {"v": value, "ts": _now_ts()}, retain=retain, qos=qos)


async def set_last_error(msg):
    # บันทึก + แจ้งเตือน error ออก sys/last_error (backend เอาไปต่อ LINE)
    st.last_error = msg
    print("[error]", msg)
    await pub(t_sys("last_error"), {"msg": msg, "ts": _now_ts()}, retain=False)


# =============================================================================
# ส่วนที่ 3: SENSOR — อ่าน + กรอง noise (median + EMA)
# =============================================================================
adc_soil = ADC(cfg.PIN_SOIL)
adc_ph   = ADC(cfg.PIN_PH)
adc_ec   = ADC(cfg.PIN_EC)
float_sw = Pin(cfg.PIN_FLOAT_SWITCH, Pin.IN, Pin.PULL_UP)


def _float_ok_now():
    # อ่าน float switch สด ณ ตอนเรียก — dry-run protection ห้ามพึ่ง cache
    return float_sw.value() == cfg.FLOAT_OK_LEVEL


def _float_irq(pin):
    # IRQ (soft) บนขา float: latch ทันทีที่น้ำหลุดระดับ ระหว่างรอรอบ FSM ถัดไป
    # ISR ตั้ง flag อย่างเดียว — ห้ามแตะ actuator/publish (iron rule เดียวกับ callback)
    if pin.value() != cfg.FLOAT_OK_LEVEL:
        st.float_fault_latch = True


float_sw.irq(handler=_float_irq, trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING)

# I2C SHT31/BME280 — temp/humidity (telemetry); ครอบ try กัน bus ค้าง
try:
    i2c = I2C(1, sda=Pin(cfg.PIN_I2C_SDA), scl=Pin(cfg.PIN_I2C_SCL), freq=cfg.I2C_FREQ)
except Exception as e:
    print("[i2c] init fail:", e)
    i2c = None


def _read_raw_12bit(adc):
    # อ่าน ADC แล้วแปลงเป็น 12-bit (0-4095). MicroPython read_u16() คืน 16-bit
    # → เลื่อนขวา 4 บิตให้เป็นสเกล 12-bit ตามที่ calibrate ไว้
    return adc.read_u16() >> 4


def _median(samples):
    s = sorted(samples)
    return s[len(s) // 2]


def read_soil_raw():
    # เก็บหลาย sample แล้วใช้ median กัน spike (noise filter ชั้นที่ 1)
    samples = []
    for _ in range(cfg.ADC_SAMPLES):
        samples.append(_read_raw_12bit(adc_soil))
    return _median(samples)


def raw_is_valid(raw):
    # เช็คก่อน map: 0 / 4095 / นอกช่วง = สายหลุด/ลัดวงจร = sensor_fault
    return cfg.ADC_RAW_MIN_VALID <= raw <= cfg.ADC_RAW_MAX_VALID


def map_moisture(raw):
    # capacitive: ค่าดิบยิ่งน้อย = ยิ่งเปียก → RAW_DRY > RAW_WET
    # moisture_% = (RAW_DRY - raw) / (RAW_DRY - RAW_WET) * 100
    span = cfg.RAW_DRY - cfg.RAW_WET
    if span == 0:
        return None
    pct = (cfg.RAW_DRY - raw) / span * 100.0
    # clamp 0-100 (raw นอกช่วง calibrate ก็ยังให้ค่าขอบ ไม่ใช่ fault)
    if pct < 0:
        pct = 0.0
    elif pct > 100:
        pct = 100.0
    return pct


def apply_ema(new_pct):
    # EMA smoothing (noise filter ชั้นที่ 2) — นิ่งกว่า median อย่างเดียว
    a = cfg.EMA_ALPHA
    if st.moisture_ema is None:
        st.moisture_ema = new_pct
    else:
        st.moisture_ema = a * new_pct + (1 - a) * st.moisture_ema
    return st.moisture_ema


async def read_temp_humid():
    # อ่าน SHT31 (0x44) — temp/humidity เป็น telemetry; คืน (temp, humid) หรือ (None, None)
    if i2c is None:
        return None, None
    try:
        # SHT31: คำสั่งวัดแม่นยำสูง (clock stretch off) = 0x2400
        i2c.writeto(cfg.SHT31_ADDR, b'\x24\x00')
        # หน่วงรอเซนเซอร์วัดเสร็จ (~15 ms ที่ high repeatability) ก่อนอ่านผล
        await asyncio.sleep_ms(20)
        data = i2c.readfrom(cfg.SHT31_ADDR, 6)
        raw_t = data[0] << 8 | data[1]
        raw_h = data[3] << 8 | data[4]
        temp = -45 + (175 * raw_t / 65535.0)
        humid = 100 * raw_h / 65535.0
        return temp, humid
    except Exception as e:
        print("[sht31] read fail:", e)
        return None, None


def detect_rain(new_pct):
    # อนุมานฝน: ความชื้นกระโดดขึ้นเองเร็วผิดปกติ "ตอนปั๊มไม่ได้ทำงาน" = น่าจะฝนตก
    now = ticks_ms()
    if st.prev_moisture is not None and not act.pump_is_on:
        jump = new_pct - st.prev_moisture
        if jump >= cfg.RAIN_JUMP_PCT:
            st.is_raining = True
            st.rain_until = ticks_add(now, cfg.RAIN_HOLD_S * 1000)
            print("[rain] ตรวจพบความชื้นเด้งขึ้น {:.1f}% → ตั้ง flag ฝนตก".format(jump))
    # หมดอายุ flag ฝน
    if st.is_raining and ticks_diff(now, st.rain_until) >= 0:
        st.is_raining = False


async def sensor_task():
    # อ่านเซนเซอร์ + กรอง noise + ตรวจฝน
    # *** float switch ไม่อ่านที่นี่แล้ว — FSM อ่านสดเองทุกรอบ (500 ms) + มี IRQ latch
    #     เพราะ cache คาบ 2 s ทำให้ dry-run protection ช้าเกิน (ต้อง instant) ***
    while True:
        try:
            # --- soil moisture ---
            raw = read_soil_raw()
            if not raw_is_valid(raw):
                # ค่าดิบเสีย → ตั้ง fault, อย่า map, อย่าให้ FSM เอาไปสั่งปั๊ม
                st.sensor_fault = True
                st.moisture = None
                await set_last_error("sensor_fault: soil raw=%d นอกช่วง" % raw)
            else:
                st.sensor_fault = False
                pct = map_moisture(raw)
                smoothed = apply_ema(pct)
                detect_rain(smoothed)
                st.prev_moisture = smoothed
                st.moisture = smoothed

            # --- temp/humidity (telemetry) ---
            t, h = await read_temp_humid()
            if t is not None:
                st.temp, st.humid = t, h

            # --- pH / EC (telemetry เท่านั้น — ยังไม่ใช้ใน control logic) ---
            st.ph = _read_raw_12bit(adc_ph)   # ค่าดิบ; การ map pH ทำที่ backend
            st.ec = _read_raw_12bit(adc_ec)

        except Exception as e:
            # อ่านเซนเซอร์ล้ม → ถือเป็น fault ชั่วคราว (ปลอดภัยไว้ก่อน)
            st.sensor_fault = True
            st.moisture = None
            print("[sensor] task error:", e)

        # ตอกบัตร heartbeat ท้ายลูปทุกรอบ
        st.hb["sensor"] = ticks_ms()
        await asyncio.sleep_ms(cfg.SENSOR_PERIOD_MS)


# =============================================================================
# ส่วนที่ 4: IRRIGATION FSM — task เดียวที่สั่ง actuator
# 5 สถานะ: INIT → IDLE → WATERING → COOLDOWN → ERROR
# ปั๊มทำงานเฉพาะ WATERING; อีก 4 สถานะ = ปั๊มหยุดทำงาน (safe-by-structure)
# =============================================================================
def _get_thresholds():
    # เลือกเกณฑ์ตาม mode (zone/crop)
    if cfg.THRESHOLD_MODE == "crop":
        try:
            return cfg.THRESHOLDS_CROP[cfg.CROP]
        except (AttributeError, KeyError):
            pass  # crop mode เปิดไม่ครบ → fallback ไป zone
    return cfg.THRESHOLDS_ZONE.get(cfg.ZONE, {"low": 60, "high": 70})


def _in_no_water_window():
    # ช่วงห้ามรด — พึ่ง NTP; ถ้า NTP ไม่ sync → ข้าม guard นี้ (ยอมให้รด)
    if cfg.NO_WATER_WINDOW is None:
        return False
    if not st.ntp_ok:
        return False  # ไม่มีเวลาจริง → ไม่ห้าม (auto mode ต้องไม่ล่มเพราะ NTP)
    start_h, end_h = cfg.NO_WATER_WINDOW
    try:
        hour = localtime(time() + cfg.TZ_OFFSET_S)[3]
    except Exception:
        return False
    return start_h <= hour < end_h


def _moisture_usable():
    # ค่าความชื้นใช้ตัดสินใจได้ไหม (ไม่ fault, ไม่ None)
    return (not st.sensor_fault) and (st.moisture is not None)


async def _enter_error(reason, critical=False):
    # เข้า ERROR: ปั๊มหยุดทำงานทันที + แจ้งเตือน
    act.pump_stop()
    st.last_stop = ticks_ms()
    st.fsm_state = "ERROR"
    st.error_reason = reason
    st.error_critical = critical
    await pub_json(t_state("pump"), "off", retain=True)
    await set_last_error(("critical " if critical else "") + "ERROR: " + reason)


async def _publish_pump_state(on):
    # state แยกจาก cmd = closed-loop (retained)
    await pub_json(t_state("pump"), "on" if on else "off", retain=True)


def try_manual_reset(skip_mqtt_guard=False):
    # ตรวจ guard 4 ข้อก่อนปลด ERROR. คืน (ok, reason)
    # (ปุ่มกายภาพข้ามได้เฉพาะ guard (3) mqtt_connected)
    if not _float_ok_now():   # อ่านสด ไม่พึ่ง cache
        return False, "float ไม่ ok (น้ำในถังยังไม่พอ)"
    if not _moisture_usable():
        return False, "sensor ยังอ่านค่าไม่ปกติ"
    if not skip_mqtt_guard and not st.mqtt_connected:
        return False, "mqtt ไม่เชื่อมต่อ"
    if st.pump_timeout_count >= cfg.PUMP_TIMEOUT_MAX:
        return False, "pump_timeout วนซ้ำ (fault ถาวร)"
    return True, "ok"


async def _do_manual_reset(skip_mqtt_guard):
    ok, reason = try_manual_reset(skip_mqtt_guard=skip_mqtt_guard)
    if ok:
        # ผ่านหมด → เคลียร์ error, reset cooldown timer + pump_timeout, กลับ IDLE
        # *** ปั๊มหยุดทำงาน — ห้ามสั่งปั๊มทำงานต่อทันที ***
        act.pump_stop()
        st.error_critical = False
        st.error_reason = ""
        st.pump_timeout_count = 0
        st.last_stop = ticks_ms()
        st.cooldown_start = ticks_ms()
        st.fsm_state = "IDLE"
        print("[reset] manual reset สำเร็จ → กลับ IDLE (ปั๊มหยุดทำงาน)")
        await _publish_pump_state(False)
    else:
        # ไม่ผ่าน → คง ERROR + แจ้งเหตุผล
        await set_last_error("reset_rejected: " + reason)


async def irrigation_fsm_task():
    th = _get_thresholds()
    while True:
        try:
            now = ticks_ms()
            state = st.fsm_state

            # --- float switch: อ่านสดทุกรอบ + consume IRQ latch (FSM เป็น writer เดียว) ---
            # latch จับกรณีน้ำหลุดระดับชั่วขณะระหว่างรอบ; เคลียร์เฉพาะเมื่อเห็นว่า set
            # (ถ้า IRQ ยิงหลังอ่าน จะคงค้างไว้ให้รอบถัดไปเห็นแทน — ไม่มี event หาย)
            latched = st.float_fault_latch
            if latched:
                st.float_fault_latch = False
            st.float_ok = _float_ok_now() and not latched

            # --- safety overrides ก่อนทุกอย่าง (ยกเว้นตอน ERROR/INIT) ---
            # network loss / tank empty / sensor fault → ปั๊มหยุดทำงาน + ERROR
            if state in ("IDLE", "WATERING", "COOLDOWN"):
                if not st.mqtt_connected:
                    await _enter_error("network_loss (WiFi/MQTT หลุด)")
                    state = "ERROR"
                elif not st.float_ok:
                    await _enter_error("tank_empty (float switch)")
                    state = "ERROR"
                elif st.sensor_fault:
                    await _enter_error("sensor_fault")
                    state = "ERROR"

            # ---------------- INIT ----------------
            if state == "INIT":
                # actuator ถูกตั้ง SAFE ใน sync code แล้ว; init เสร็จ → IDLE
                act.pump_stop()
                st.last_stop = now
                st.fsm_state = "IDLE"

            # ---------------- IDLE ----------------
            elif state == "IDLE":
                # เงื่อนไขเริ่มรด (ครบทุกข้อ)
                if _moisture_usable():
                    elapsed_off = ticks_diff(now, st.last_stop)
                    ready = (
                        st.moisture < th["low"]
                        and not st.is_raining
                        and st.float_ok
                        and elapsed_off >= cfg.COOLDOWN_S * 1000
                        and elapsed_off >= cfg.MIN_OFF_S * 1000
                        and not _in_no_water_window()
                    )
                    # re-read float สดอีกครั้งเป็นเงื่อนไขสุดท้ายก่อนสั่งปั๊ม
                    # (กันเริ่มรดด้วยค่าที่เก่าแม้เพียงในรอบเดียวกัน)
                    if ready and _float_ok_now():
                        await act.pump_run()      # ปั๊มทำงาน (เปิดวาล์ว→settle→ปั๊ม)
                        st.water_start = now
                        st.fsm_state = "WATERING"
                        print("[fsm] IDLE→WATERING: ปั๊มทำงาน (moist={:.1f}<{})".format(
                            st.moisture, th["low"]))
                        await _publish_pump_state(True)

            # ---------------- WATERING ----------------
            elif state == "WATERING":
                run_time = ticks_diff(now, st.water_start)
                if not _moisture_usable():
                    # ระหว่างรดแล้ว sensor พัง → ตัดทันที (safety override จับด้านบนแล้ว)
                    await _enter_error("sensor_fault ระหว่างรด")
                elif st.moisture >= th["high"]:
                    # รดสำเร็จ → COOLDOWN, reset pump_timeout
                    act.pump_stop()               # ปั๊มหยุดทำงาน
                    st.last_stop = now
                    st.pump_timeout_count = 0
                    st.cooldown_start = now
                    st.fsm_state = "COOLDOWN"
                    print("[fsm] WATERING→COOLDOWN: สำเร็จ (moist>={})".format(th["high"]))
                    await _publish_pump_state(False)
                elif run_time >= cfg.MAX_RUN_S * 1000:
                    # ชนเพดานเวลา → ตัด + นับ timeout (safety cutoff ชั้นที่ 4)
                    act.pump_stop()               # ปั๊มหยุดทำงาน
                    st.last_stop = now
                    st.pump_timeout_count += 1
                    st.cooldown_start = now
                    await pub(t_sys("last_error"),
                              {"msg": "pump_timeout #%d" % st.pump_timeout_count,
                               "ts": _now_ts()})
                    if st.pump_timeout_count >= cfg.PUMP_TIMEOUT_MAX:
                        # วนซ้ำเกินเกณฑ์ → critical ERROR (ล็อกรอ manual reset)
                        await _enter_error(
                            "pump_timeout วนซ้ำ %d ครั้ง" % st.pump_timeout_count,
                            critical=True)
                    else:
                        st.fsm_state = "COOLDOWN"
                        print("[fsm] WATERING→COOLDOWN: ชนเพดานเวลา (#{})".format(
                            st.pump_timeout_count))
                    await _publish_pump_state(False)

            # ---------------- COOLDOWN ----------------
            elif state == "COOLDOWN":
                # ปั๊มหยุดทำงานอยู่แล้ว — รอครบ cooldown_s แล้วกลับ IDLE
                if ticks_diff(now, st.cooldown_start) >= cfg.COOLDOWN_S * 1000:
                    st.fsm_state = "IDLE"
                    print("[fsm] COOLDOWN→IDLE")

            # ---------------- ERROR ----------------
            elif state == "ERROR":
                # ปั๊มต้องหยุดทำงานเสมอใน ERROR (กันพลาด เรียกซ้ำได้)
                if act.pump_is_on:
                    act.pump_stop()

                # 1) มี manual reset request → ลองปลด
                #    (ปุ่มกายภาพข้ามได้เฉพาะ guard mqtt — flag reset_from_button)
                if st.reset_requested:
                    await _do_manual_reset(skip_mqtt_guard=st.reset_from_button)
                    st.reset_requested = False   # เคลียร์ flag ทุกครั้งหลังประมวลผล
                    st.reset_from_button = False

                # 2) transient error (ไม่ critical): เหตุหายเอง → auto กลับ IDLE
                elif not st.error_critical:
                    recovered = (
                        st.mqtt_connected
                        and st.float_ok
                        and _moisture_usable()
                    )
                    if recovered:
                        st.error_reason = ""
                        st.last_stop = now
                        st.cooldown_start = now
                        st.fsm_state = "IDLE"
                        print("[fsm] ERROR→IDLE: เหตุหายแล้ว (auto recover)")
                        await _publish_pump_state(False)
                # 3) critical → ล็อก รอ manual reset เท่านั้น (ไม่ทำอะไรเพิ่ม)

        except Exception as e:
            # FSM พังเอง = critical → บังคับ SAFE + เข้า ERROR
            act.all_safe()
            st.fsm_state = "ERROR"
            st.error_critical = True
            print("[fsm] task exception:", e)

        # ตอกบัตร heartbeat ท้ายลูปทุกรอบ
        st.hb["irrigation_fsm"] = ticks_ms()
        await asyncio.sleep_ms(cfg.FSM_PERIOD_MS)


# =============================================================================
# ส่วนที่ 5: MQTT — in/out + auto reconnect/resubscribe
# callback แค่ตั้ง flag ไม่สั่งปั๊มตรงๆ
# =============================================================================
def on_message(topic, msg, retained):
    # callback (sync) — ห้ามสั่งปั๊ม / ห้าม block; แค่ parse แล้วตั้ง flag
    try:
        topic = topic.decode() if isinstance(topic, bytes) else topic
        payload = msg.decode() if isinstance(msg, bytes) else msg
        print("[mqtt] rx:", topic, payload)

        if topic == SYSTEM_CMD:
            try:
                data = json.loads(payload)
            except Exception:
                data = {}
            if data.get("action") == "reset":
                # แค่ตั้ง flag — ไม่เคลียร์ ERROR ใน callback
                # reset ทาง MQTT ห้ามข้าม guard mqtt → เคลียร์ flag ปุ่มชัดเจน
                # (กันกรณีปุ่มเพิ่งตั้ง flag ค้างในหน้าต่างเดียวกันแล้วถูกยืมสิทธิ์)
                st.reset_from_button = False
                st.reset_requested = True
                print("[mqtt] รับคำสั่ง reset → ตั้ง flag")
    except Exception as e:
        print("[mqtt] on_message error:", e)


async def on_connect(c):
    # เรียกทุกครั้งที่ (re)connect สำเร็จ — resubscribe + ประกาศ online
    # *** subscribe เฉพาะ topic ที่มี handler ใน on_message เท่านั้น ***
    #     pump ไม่รับคำสั่งผ่าน MQTT โดยตั้งใจ (safe-by-structure):
    #     ปั๊มทำงานได้เฉพาะ state WATERING ที่ FSM คุมตัวเดียว ไม่มี override
    await c.subscribe(SYSTEM_CMD, 1)
    await c.publish(AVAIL_TOPIC, "online", retain=True, qos=1)
    print("[mqtt] connected + subscribed + online")


async def mqtt_task():
    # poll สถานะการเชื่อมต่อ + อัปเดต flag ให้ FSM ใช้เป็น guard
    while True:
        try:
            if client is not None:
                st.mqtt_connected = client.isconnected()
        except Exception as e:
            st.mqtt_connected = False
            print("[mqtt] task error:", e)
        # ตอกบัตร heartbeat ท้ายลูปทุกรอบ
        st.hb["mqtt"] = ticks_ms()
        await asyncio.sleep_ms(cfg.MQTT_PERIOD_MS)


# =============================================================================
# ส่วนที่ 6: WATCHDOG (Software) — feed Hardware WDT เฉพาะเมื่อทุก task สด
# =============================================================================
hw_wdt = None  # Hardware WDT (init ใน main หลังเตรียมทุกอย่างพร้อม)


def _all_tasks_fresh():
    # อ่าน heartbeat ทุก task → True เฉพาะเมื่อทุกตัว age อยู่ในเกณฑ์
    # ใช้ ticks_diff เสมอ (กัน wrap-around)
    now = ticks_ms()
    for name, limit in cfg.HB_LIMIT_MS.items():
        age = ticks_diff(now, st.hb.get(name, 0))
        if age > limit:
            print("[wdog] task '{}' ค้าง (age={}ms > {}ms) → ตั้งใจไม่ feed WDT".format(
                name, age, limit))
            return False
    return True


async def watchdog_task():
    # ตื่นทุก WATCHDOG_PERIOD_MS อ่าน heartbeat ทุก task
    # feed Hardware WDT เฉพาะเมื่อทุก task สด; ถ้าตัวใดค้าง "ตั้งใจไม่ feed" → reset
    while True:
        if _all_tasks_fresh():
            if hw_wdt is not None:
                hw_wdt.feed()  # *** จุดเดียวในระบบที่ feed Hardware WDT ***
        # หมายเหตุ: watchdog_task เองไม่มี heartbeat ใน HB_LIMIT_MS
        # ถ้า task นี้ค้าง → ไม่มีใคร feed → Hardware WDT reset เอง (ถูกต้อง)
        await asyncio.sleep_ms(cfg.WATCHDOG_PERIOD_MS)


# =============================================================================
# ส่วนที่ 7: ปุ่มกายภาพ manual reset (fallback ตอนเน็ตล่ม)
# debounce ~50ms + กดค้าง >2s ตอน ERROR → ตั้ง flag ให้ FSM ประมวลผล
# *** ปุ่มตั้ง flag เท่านั้น — irrigation_fsm_task เป็น task เดียวที่แตะ
#     actuator/fsm_state (single-writer เหมือน MQTT reset) ***
# =============================================================================
reset_btn = Pin(cfg.PIN_RESET_BTN, Pin.IN, Pin.PULL_UP)


async def button_task():
    pressed_since = None
    while True:
        # ปุ่ม pull-up: กด = 0
        is_down = (reset_btn.value() == 0)
        if is_down:
            await asyncio.sleep_ms(cfg.BTN_DEBOUNCE_MS)  # debounce
            if reset_btn.value() == 0:
                if pressed_since is None:
                    pressed_since = ticks_ms()
                held = ticks_diff(ticks_ms(), pressed_since)
                if held >= cfg.BTN_HOLD_MS and st.fsm_state == "ERROR":
                    print("[btn] กดค้างครบ → ตั้ง flag reset (ข้าม guard mqtt)")
                    st.reset_from_button = True
                    st.reset_requested = True
                    pressed_since = None
                    await asyncio.sleep_ms(500)  # กันเด้งซ้ำ
        else:
            pressed_since = None
        await asyncio.sleep_ms(50)


# =============================================================================
# ส่วนที่ 8: TELEMETRY + DIAGNOSTICS
# =============================================================================
async def telemetry_task():
    while True:
        try:
            if st.moisture is not None:
                await pub_json(t_tele("soil", "moisture"), round(st.moisture, 1))
            if st.temp is not None:
                await pub_json(t_tele("air", "temp"), round(st.temp, 1))
                await pub_json(t_tele("air", "humid"), round(st.humid, 1))
            # pH/EC ส่งเป็น telemetry เท่านั้น (ค่าดิบ — map ที่ backend)
            if st.ph is not None:
                await pub_json(t_tele("soil", "ph_raw"), st.ph)
                await pub_json(t_tele("soil", "ec_raw"), st.ec)
        except Exception as e:
            print("[tele] error:", e)
        await asyncio.sleep_ms(cfg.TELEMETRY_PERIOD_MS)


async def diag_task():
    boot = ticks_ms()
    while True:
        try:
            await pub(t_sys("uptime"), {"v": ticks_diff(ticks_ms(), boot) // 1000})
            await pub(t_sys("freemem"), {"v": gc.mem_free()})
            await pub(t_sys("mode"), {"v": st.fsm_state})
            try:
                import network
                sta = network.WLAN(network.STA_IF)
                await pub(t_sys("rssi"), {"v": sta.status('rssi')})
            except Exception:
                pass
        except Exception as e:
            print("[diag] error:", e)
        await asyncio.sleep_ms(cfg.DIAG_PERIOD_MS)


async def gc_task():
    # gc.collect() เป็นระยะ เฝ้า memory leak (freemem ส่งใน diag_task)
    while True:
        gc.collect()
        await asyncio.sleep_ms(cfg.GC_PERIOD_MS)


# =============================================================================
# ส่วนที่ 9: NTP (best-effort — NTP fail ต้องไม่ทำให้ auto mode ล่ม)
# *** ต้อง sync ก่อน TLS handshake แรกเสมอ (I24): Pico W ไม่มี battery-backed RTC
#     บูตมาเวลาเริ่มที่ default (~ปี 2021) ถ้า broker ตรวจ cert validity period
#     ด้วยนาฬิกาที่ยังไม่ sync อาจทำให้ TLS handshake fail ผิดปกติ ***
# =============================================================================
async def _wifi_connect():
    # ต่อ WiFi ล้วน ๆ (ไม่มี TLS) ให้ขึ้นก่อน เพื่อเปิดทางให้ NTP sync (UDP)
    # ได้ก่อน mqtt_as ทำ TLS handshake (I24)
    # *** หมายเหตุ: mqtt_as (lib ภายนอก ไม่ได้ vendor ในนี้) อาจ re-associate WiFi
    #     ซ้ำตอน client.connect() แม้ isconnected() แล้ว — ยังไม่ยืนยันจาก source
    #     จริง ต้อง bench-test ว่า (re-assoc + TLS) รวมกันยังอยู่ใต้
    #     MQTT_CONNECT_TIMEOUT_MS ไม่งั้นเสี่ยง reset วนตอนเน็ตช้า ***
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(cfg.WIFI_SSID, cfg.WIFI_PASS)
        while not wlan.isconnected():
            await asyncio.sleep_ms(100)
    return wlan


async def ntp_sync():
    # *** ntptime.settime() เป็น blocking ล้วน (ไม่มี await ภายใน) — เอา
    #     asyncio.wait_for_ms มาครอบจะ timeout ไม่ติดจริง เพราะ event loop
    #     ถูก block จนกว่า syscall จะคืนค่า จึงต้องตัดที่ระดับ socket แทน:
    #     ntptime ของ MicroPython มีตัวแปร module `timeout` (วินาที) ที่ถูกใช้
    #     settimeout() บน UDP socket — เป็นจุดตัดที่ทำงานจริง
    #     (จุดนี้รันก่อนเปิด Hardware WDT — ถ้าค้างไม่มีตัวกู้ ต้อง bound ในตัว;
    #      ส่วน DNS/getaddrinfo มี timeout ภายในของ lwIP เอง — bound แต่ต้อง
    #      ยืนยันบนฮาร์ดแวร์จริงว่าไม่เกินหลักวินาที)
    try:
        import ntptime
        ntptime.host = cfg.NTP_HOST
        # ntptime รุ่นเก่ามากอาจไม่มี timeout — การตั้ง attribute ไม่ error
        # แค่ไม่ถูกใช้ (behaviour เท่าของเดิม ไม่แย่ลง)
        ntptime.timeout = max(1, cfg.NTP_TIMEOUT_MS // 1000)
        ntptime.settime()
        st.ntp_ok = True
        print("[ntp] sync สำเร็จ")
    except Exception as e:
        st.ntp_ok = False
        print("[ntp] sync ล้มเหลว (ใช้ ticks ต่อ, auto mode ไม่กระทบ):", e)


# =============================================================================
# ส่วนที่ 10: GLOBAL EXCEPTION HANDLER — critical task ตาย → บังคับ SAFE + reset
# =============================================================================
def _exception_handler(loop, context):
    print("[fatal] uncaught exception ใน task:", context.get("exception"))
    # บังคับทุก actuator SAFE ทันที (ปั๊มหยุดทำงาน)
    try:
        act.all_safe()
    except Exception:
        pass
    # ไม่ feed WDT → Hardware WDT จะ reset เองภายใน ~8s (fail-safe)
    # หรือ reset ทันทีเพื่อกู้สถานะ
    machine.reset()


# =============================================================================
# ส่วนที่ 11: BOOT (sync) + MAIN (async)
# =============================================================================
def decode_reset_cause():
    # อ่านสาเหตุ reboot รอบนี้ (debug reboot loop: WDT / brown-out / power-on)
    rc = reset_cause()
    mapping = {
        getattr(machine, "PWRON_RESET", -1): "power_on",
        getattr(machine, "WDT_RESET", -2): "watchdog",
        getattr(machine, "HARD_RESET", -3): "hard",
        getattr(machine, "SOFT_RESET", -4): "soft",
        getattr(machine, "BROWNOUT_RESET", -5): "brownout",
    }
    return mapping.get(rc, "unknown(%s)" % rc)


async def main():
    global client, hw_wdt

    # อ่าน reset_cause (เก็บไว้ publish หลัง MQTT พร้อม)
    st.last_reset_cause = decode_reset_cause()
    print("[boot] reset_cause =", st.last_reset_cause)

    # ตั้ง global exception handler ให้ event loop
    asyncio.get_event_loop().set_exception_handler(_exception_handler)

    # --- WiFi ก่อน (ไม่มี TLS) แล้ว NTP sync ก่อน TLS handshake แรกเสมอ (I24) ---
    try:
        await asyncio.wait_for_ms(_wifi_connect(), cfg.WIFI_CONNECT_TIMEOUT_MS)
    except Exception as e:
        print("[wifi] connect ล้มเหลว/timeout:", e)
        await asyncio.sleep_ms(2000)
        machine.reset()
    await ntp_sync()

    # --- ตั้งค่า mqtt_as ---
    if MQTTClient is not None:
        mqtt_base["client_id"] = cfg.NODE_ID
        mqtt_base["server"] = cfg.MQTT_HOST
        mqtt_base["port"] = cfg.MQTT_PORT
        mqtt_base["user"] = cfg.MQTT_USER
        mqtt_base["password"] = cfg.MQTT_PASS
        mqtt_base["keepalive"] = cfg.MQTT_KEEPALIVE_S
        mqtt_base["ssid"] = cfg.WIFI_SSID
        mqtt_base["wifi_pw"] = cfg.WIFI_PASS
        mqtt_base["ssl"] = cfg.MQTT_USE_TLS
        if cfg.MQTT_USE_TLS:
            params = {"server_hostname": cfg.MQTT_HOST}
            if cfg.MQTT_SSL_CA is not None:
                params["cadata"] = cfg.MQTT_SSL_CA
            mqtt_base["ssl_params"] = params
        # LWT (retained): broker จะ publish ให้เองเมื่อ node หาย
        mqtt_base["will"] = (AVAIL_TOPIC, "offline", True, 1)
        mqtt_base["subs_cb"] = on_message
        mqtt_base["connect_coro"] = on_connect
        mqtt_base["keepalive"] = cfg.MQTT_KEEPALIVE_S
        MQTTClient.DEBUG = True
        client = MQTTClient(mqtt_base)

        # --- connect (TLS handshake เป็น blocking ~2-4s) ---
        # *** option 1: ครอบ connect timeout ขอบเขตชัด ***
        # เกิน MQTT_CONNECT_TIMEOUT_MS = เน็ตมีปัญหาจริง → ปล่อยให้ reset เป็นพฤติกรรมที่ถูก
        # (Hardware WDT ยังไม่เปิดตอนนี้ จึงไม่เกิด reboot loop ระหว่าง handshake;
        #  ถ้าเจอ reboot loop จริงค่อยเพิ่ม feed ระหว่าง connect phase ตาม hook ในสเปค)
        try:
            await asyncio.wait_for_ms(client.connect(), cfg.MQTT_CONNECT_TIMEOUT_MS)
            st.mqtt_connected = True
        except Exception as e:
            print("[mqtt] connect ล้มเหลว/timeout:", e)
            st.mqtt_connected = False
            # connect ไม่ได้ → reset เพื่อเริ่มใหม่ (mqtt_as backoff จัดการรอบถัดไป)
            await asyncio.sleep_ms(2000)
            machine.reset()

    # --- publish ข้อมูลบูต ---
    await pub(t_sys("last_reset"), {"v": st.last_reset_cause, "ts": _now_ts()}, retain=True)
    await _publish_pump_state(False)  # ยืนยัน state = ปั๊มหยุดทำงาน

    # --- เปิด Hardware WDT หลังทุกอย่างพร้อม (เลี่ยง reboot loop ตอน connect) ---
    hw_wdt = WDT(timeout=cfg.HW_WDT_TIMEOUT_MS)
    print("[boot] Hardware WDT เปิด timeout =", cfg.HW_WDT_TIMEOUT_MS, "ms")

    # INIT → IDLE: FSM เริ่มจาก INIT, จะเลื่อนเป็น IDLE เองในลูปแรก
    st.fsm_state = "INIT"

    # --- สร้าง 4 task หลัก + task เสริม ---
    tasks = [
        asyncio.create_task(sensor_task()),
        asyncio.create_task(irrigation_fsm_task()),
        asyncio.create_task(mqtt_task()),
        asyncio.create_task(watchdog_task()),
        # task เสริม (ไม่อยู่ใน critical heartbeat set)
        asyncio.create_task(button_task()),
        asyncio.create_task(telemetry_task()),
        asyncio.create_task(diag_task()),
        asyncio.create_task(gc_task()),
    ]
    print("[boot] ทุก task เริ่มทำงาน — เข้าสู่ loop หลัก")
    await asyncio.gather(*tasks)


# =============================================================================
# ENTRY POINT (sync)
# *** default off ตอนบูต: ตั้ง actuator = SAFE ก่อนเรียก asyncio.run() ***
# =============================================================================
# บังคับ actuator เข้า SAFE ทันทีตั้งแต่ก่อนเริ่ม event loop
# (Actuators.__init__ ตั้ง OFF ไว้แล้ว เรียก all_safe() ย้ำอีกชั้นให้ชัวร์)
act.all_safe()
print("[boot] actuator ตั้งค่า SAFE แล้ว (ปั๊มหยุดทำงาน + วาล์วปิด)")

try:
    asyncio.run(main())
except Exception as e:
    # หลุดจาก event loop ด้วยเหตุใดก็ตาม → SAFE + reset (fail-safe สุดท้าย)
    print("[fatal] หลุดจาก main loop:", e)
    try:
        act.all_safe()
    except Exception:
        pass
    machine.reset()
finally:
    # กันพลาด: ถ้ามาถึงตรงนี้โดยไม่ reset ก็ยังต้อง SAFE ไว้
    act.all_safe()
