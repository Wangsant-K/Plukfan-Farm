# =============================================================================
# esp32s3_gateway.py — MicroPython firmware สำหรับ ESP32-S3 gateway
# ระบบ IoT Smart Farm "สวนปลูกฝัน" (prefix: plukfan) — node: esp32s3-01
# -----------------------------------------------------------------------------
# สโคปเฟส 1 = "Connectivity + Health Skeleton" เท่านั้น:
#   ✅ WiFi + MQTT (TLS) + sync NTP + ประกาศตัว (online/LWT) + diagnostics + dual watchdog
#   ❌ ไม่มีกล้อง (วาง stub เปล่า + TODO — ห้าม import camera ในเฟสนี้)
#   ❌ ไม่มี actuator (gateway ไม่มี control authority เหนือปั๊ม/วาล์ว — ห้ามขับ GPIO output)
#   ❌ ไม่มี farm sensor (อ่านดิน/อากาศเป็นงานของ Pico W)
#
# ปรัชญาออกแบบ: SAFETY ก่อน FEATURE เสมอ
#   - gateway ล่ม "ต้องไม่กระทบระบบรดน้ำ" (Pico W รัน irrigation logic ฉุกเฉินเองได้)
#     → firmware นี้ห้าม publish อะไรที่ node อื่นต้องรอ + ห้ามถือ state ที่ระบบรดน้ำพึ่งพา
#   - network หลุด → reconnect แบบ backoff (mqtt_as จัดการ) ห้าม busy-loop
#
# อักษรย่อ (กางครั้งแรก):
#   NTP  = Network Time Protocol            (sync เวลา — ต้องทำก่อน TLS handshake)
#   TLS  = Transport Layer Security         (เข้ารหัส MQTT + verify broker)
#   RSSI = Received Signal Strength Indicator (ความแรงสัญญาณ WiFi เป็น dBm)
#   LWT  = Last Will and Testament          (broker ยิงแทนเมื่อ node หลุดกะทันหัน)
#   WDT  = Watchdog Timer                   (Hardware = วงจรในชิป / Software = coroutine)
#   PSRAM= Pseudo-Static RAM                (หน่วยความจำเสริม — ใช้ในเฟสกล้อง)
#
# Extension point: capture_stub() = จุดเสียบ camera capture เฟสหน้า (ดูส่วนที่ 6)
# =============================================================================

import sys
import gc
import json
import machine
from machine import WDT, reset_cause
import network
import uasyncio as asyncio
from utime import ticks_ms, ticks_diff, time

import gateway_config as cfg

# esp32 module: ใช้ mcu_temperature() (best-effort — บาง build ไม่มี → ข้าม metric temp)
try:
    import esp32 as _esp32
except ImportError:
    _esp32 = None

try:
    # mqtt_as = async MQTT ของ Peter Hinch (จัดการ WiFi/MQTT reconnect + backoff + keepalive ให้)
    from mqtt_as import MQTTClient, config as mqtt_base
except ImportError:
    MQTTClient = None
    mqtt_base = {}


# =============================================================================
# ส่วนที่ 1: SHARED STATE — task ทุกตัวคุยกันผ่าน object นี้ (ไม่เรียกข้ามกันตรงๆ)
# *** gateway ไม่มี actuator/sensor/FSM จึงไม่มี state ใดที่ระบบรดน้ำพึ่งพา ***
# =============================================================================
class State:
    def __init__(self):
        # --- heartbeat ("ตอกบัตร"): แต่ละ task เขียน ticks_ms() ท้ายลูปทุกรอบ ---
        # Software Watchdog อ่านค่านี้เพื่อตัดสินใจ feed Hardware WDT
        self.hb = {
            "mqtt": ticks_ms(),
            "health": ticks_ms(),
        }

        # --- สถานะระบบ ---
        self.mqtt_connected = False   # สถานะการเชื่อมต่อ MQTT/WiFi
        self.ntp_ok = False           # NTP sync สำเร็จไหม (มีผลแค่ความถูกต้องของ ts)
        self.uptime_ms = 0            # counter สะสมเอง (ทน wrap-around ของ ticks_ms)
        self.pending_cmd = None       # คำสั่งที่ callback ตั้งไว้ให้ health_task ประมวลผล
        self.last_reset_cause = ""    # สาเหตุ reboot รอบนี้ (debug reboot loop)


st = State()
client = None  # MQTTClient (สร้างใน main)
hw_wdt = None  # Hardware WDT (init ใน main ทันทีหลังเข้า main)


# =============================================================================
# ส่วนที่ 2: MQTT topic helper + publish helper
# =============================================================================
def t_sys(metric):
    # diagnostics: plukfan/node/esp32s3-01/sys/<metric>
    return "{}/node/{}/sys/{}".format(cfg.TOPIC_PREFIX, cfg.NODE_ID, metric)


# availability / LWT — retained, payload เป็น plain string "online"/"offline"
# (LWT ตั้ง dynamic ts ไม่ได้ จึงเป็น exception ของกฎ "ทุก payload เป็น JSON มี ts";
#  diagnostics ทุกตัวยังเป็น JSON มี ts ตามสเปค)
AVAIL_TOPIC = "{}/node/{}/availability".format(cfg.TOPIC_PREFIX, cfg.NODE_ID)

# node-level command (ห้าม retained)
# *** TODO: topic plukfan/node/<id>/cmd เป็น node-level — ถ้ายังไม่อยู่ในตาราง
#     convention ของสเปก ให้ "เสนอเพิ่ม" ไม่ใช่ตัดสินเอง ***
CMD_TOPIC = "{}/node/{}/cmd".format(cfg.TOPIC_PREFIX, cfg.NODE_ID)


def _now_ts():
    # timestamp สำหรับ payload — ใช้ wall-clock ถ้า NTP sync แล้ว ไม่งั้น fallback เป็น ticks
    # *** ใช้เฉพาะใน field ts ของ payload เท่านั้น — ห้ามเอาไปทำ timer ***
    if st.ntp_ok:
        try:
            return time()
        except Exception:
            pass
    return ticks_ms()


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


async def pub_metric(metric, value, retain=False):
    # ห่อ value + ts เป็น JSON ตาม convention (ทุก payload diagnostics มี field ts)
    await pub(t_sys(metric), {"v": value, "ts": _now_ts()}, retain=retain)


# =============================================================================
# ส่วนที่ 3: DIAGNOSTICS — เก็บค่าสุขภาพตัวเอง (best-effort ต่อ metric)
# metric ใด collect ไม่ได้ → ข้าม metric นั้น ไม่ล้มทั้ง task
# =============================================================================
def read_freemem():
    # freemem ← gc.mem_free() (เฝ้า memory leak; health_task เรียก gc.collect เป็นระยะ)
    return gc.mem_free()


def read_rssi():
    # rssi ← network.WLAN(STA_IF).status('rssi') — ความแรงสัญญาณ WiFi (dBm)
    try:
        sta = network.WLAN(network.STA_IF)
        return sta.status('rssi')
    except Exception as e:
        print("[diag] rssi อ่านไม่ได้ (ข้าม):", e)
        return None


def read_temp():
    # temp ← อุณหภูมิชิป (best-effort — บาง build ไม่มี esp32.mcu_temperature → ข้าม ไม่ crash)
    if _esp32 is None:
        return None
    try:
        return _esp32.mcu_temperature()
    except Exception as e:
        print("[diag] temp อ่านไม่ได้ (ข้าม):", e)
        return None


# =============================================================================
# ส่วนที่ 4: TASKS (3 ตัวตามสเปค) — mqtt_task / health_task / watchdog_task
# =============================================================================
async def mqtt_task():
    # poll สถานะการเชื่อมต่อ → อัปเดต flag + ตอก heartbeat ขณะ connection up
    # (mqtt_as จัดการ reconnect/resubscribe/keepalive เองเบื้องหลัง)
    # staleness = cfg.MQTT_STALE_MS (5000 ms)
    while True:
        try:
            if client is not None:
                st.mqtt_connected = client.isconnected()
        except Exception as e:
            st.mqtt_connected = False
            print("[mqtt] task error:", e)
        # ตอกบัตร heartbeat ท้ายลูปทุกรอบ
        st.hb["mqtt"] = ticks_ms()
        await asyncio.sleep_ms(cfg.MQTT_POLL_MS)


async def health_task():
    # ทุก tick (HEALTH_TICK_MS): สะสม uptime + ตอก heartbeat + ประมวลผล pending_cmd
    # ทุก HEALTH_PUBLISH_S: publish sys/{freemem,uptime,rssi,temp} + gc.collect เป็นระยะ
    # staleness = cfg.HEALTH_STALE_MS (3000 ms)
    last_tick = ticks_ms()
    last_publish = ticks_ms()
    last_gc = ticks_ms()
    publish_interval_ms = cfg.HEALTH_PUBLISH_S * 1000

    while True:
        try:
            now = ticks_ms()

            # --- uptime: สะสมจาก ticks_diff (ทน wrap-around ~12.4 วันของ ticks_ms) ---
            # *** ห้ามใช้ + / < ดิบบนค่า tick — ต้อง ticks_diff เสมอ ***
            elapsed = ticks_diff(now, last_tick)
            if elapsed > 0:
                st.uptime_ms += elapsed
            last_tick = now

            # --- ประมวลผลคำสั่งที่ callback ตั้งไว้ (callback ตั้ง flag เท่านั้น) ---
            if st.pending_cmd is not None:
                cmd = st.pending_cmd
                st.pending_cmd = None
                await handle_cmd(cmd)

            # --- publish diagnostics ทุก HEALTH_PUBLISH_S ---
            if ticks_diff(now, last_publish) >= publish_interval_ms:
                last_publish = now
                await publish_diagnostics()

            # --- gc.collect เป็นระยะ (เฝ้า memory leak; freemem ส่งใน publish_diagnostics) ---
            if ticks_diff(now, last_gc) >= cfg.GC_PERIOD_MS:
                last_gc = now
                gc.collect()

        except Exception as e:
            # health task พังเอง → log; ไม่ตอก heartbeat รอบนี้ → ถ้าค้างจริง Software WDT จับ
            print("[health] task error:", e)

        # ตอกบัตร heartbeat ท้ายลูปทุกรอบ
        st.hb["health"] = ticks_ms()
        await asyncio.sleep_ms(cfg.HEALTH_TICK_MS)


async def publish_diagnostics():
    # ส่ง sys/{freemem,uptime,rssi,temp} — แต่ละ metric best-effort (collect ไม่ได้ → ข้าม)
    await pub_metric("freemem", read_freemem())
    await pub_metric("uptime", st.uptime_ms // 1000)   # วินาที (จาก counter สะสม)

    rssi = read_rssi()
    if rssi is not None:
        await pub_metric("rssi", rssi)

    temp = read_temp()
    if temp is not None:
        await pub_metric("temp", round(temp, 1))


# --- Software Watchdog --------------------------------------------------------
def _all_tasks_fresh():
    # อ่าน heartbeat ทุก task → True เฉพาะเมื่อทุกตัว age อยู่ในเกณฑ์ (ticks_diff กัน wrap-around)
    now = ticks_ms()
    for name, limit in cfg.HB_LIMIT_MS.items():
        age = ticks_diff(now, st.hb.get(name, 0))
        if age > limit:
            print("[wdog] task '{}' ค้าง (age={}ms > {}ms) → ตั้งใจไม่ feed Hardware WDT".format(
                name, age, limit))
            return False
    return True


async def watchdog_task():
    # *** Software Watchdog *** — ตื่นทุก WATCHDOG_WAKE_MS อ่าน heartbeat ทุก task
    # feed Hardware WDT "เฉพาะเมื่อทุก task สด"; ถ้าตัวใดค้าง → ตั้งใจไม่ feed → Hardware WDT reset
    # (wdt.feed() แค่รีเซ็ตตัวนับถอยหลัง ไม่ส่งข้อมูลใดๆ; heartbeat อยู่ใน RAM เท่านั้น)
    while True:
        if _all_tasks_fresh():
            if hw_wdt is not None:
                hw_wdt.feed()  # *** จุดเดียวในระบบที่ feed Hardware WDT ***
        # หมายเหตุ: watchdog_task เองไม่มี heartbeat ใน HB_LIMIT_MS
        # ถ้า task นี้ค้าง → ไม่มีใคร feed → Hardware WDT reset เอง (ถูกต้องตามดีไซน์)
        await asyncio.sleep_ms(cfg.WATCHDOG_WAKE_MS)


# =============================================================================
# ส่วนที่ 5: MQTT — callback (ตั้ง flag เท่านั้น) + on_connect (resubscribe + online)
# =============================================================================
def on_message(topic, msg, retained):
    # callback (sync) — ห้าม block / ห้ามทำงานหนัก; แค่ parse แล้วตั้ง flag ให้ health_task
    try:
        topic = topic.decode() if isinstance(topic, bytes) else topic
        payload = msg.decode() if isinstance(msg, bytes) else msg
        print("[mqtt] rx:", topic, payload, "retained=", retained)

        if topic == CMD_TOPIC:
            if retained:
                # cmd ห้าม retained — ถ้าเจอ retained = config ผิดที่ broker → log เตือน ไม่ทำตาม
                print("[mqtt] เตือน: cmd มาแบบ retained → ข้าม (cmd ต้องไม่ retained)")
                return
            cmd = _parse_cmd(payload)
            if cmd:
                st.pending_cmd = cmd   # ตั้ง flag เท่านั้น — งานจริงทำใน health_task
                print("[mqtt] รับคำสั่ง:", cmd, "→ ตั้ง flag")
    except Exception as e:
        print("[mqtt] on_message error:", e)


def _parse_cmd(payload):
    # รองรับทั้ง JSON {"cmd": "ping"} และ plain string "ping"
    payload = payload.strip()
    if payload.startswith("{"):
        try:
            data = json.loads(payload)
            return data.get("cmd")
        except Exception:
            return None
    return payload or None


async def on_connect(c):
    # เรียกทุกครั้งที่ (re)connect สำเร็จ — resubscribe + ประกาศ online (retained)
    await c.subscribe(CMD_TOPIC, 1)
    await c.publish(AVAIL_TOPIC, "online", retain=True, qos=1)
    print("[mqtt] connected + subscribed cmd + online (retained)")


async def handle_cmd(cmd):
    # ประมวลผลคำสั่ง node-level (เรียกจาก health_task หลัง callback ตั้ง flag)
    print("[cmd] ประมวลผล:", cmd)
    if cmd == "ping":
        # ตอบ ping ด้วยสัญญาณสุขภาพ (ไม่มี node อื่นต้องรอ — แค่ probe)
        await pub_metric("pong", st.uptime_ms // 1000)
    elif cmd == "reboot":
        # reboot อย่างมีระเบียบ (gateway ไม่มี actuator → ไม่มีอะไรต้อง set SAFE)
        print("[cmd] reboot ตามคำสั่ง → machine.reset()")
        await asyncio.sleep_ms(200)   # ให้ publish ค้างท่อได้ระบายก่อน
        machine.reset()
    elif cmd == "capture":
        # เฟสกล้อง: map ไป stub — จับ NotImplementedError ไม่ให้ crash ทั้ง task
        try:
            await capture_stub()
        except NotImplementedError as e:
            print("[cmd] capture ยังไม่ทำ:", e)
            await pub_metric("capture", "not_implemented")
    else:
        print("[cmd] ไม่รู้จักคำสั่ง:", cmd)


# =============================================================================
# ส่วนที่ 6: EXTENSION POINT กล้อง (เฟสหน้า — stub เปล่าเท่านั้น)
# *** ห้ามเขียน logic กล้องจริง / ห้าม import โมดูล camera ในเฟสนี้ ***
# =============================================================================
async def capture_stub():
    # TODO(phase-camera): จุดเสียบ camera capture ของเฟสหน้า
    #   - MicroPython official ไม่มีโมดูล `camera` ในตัว → ต้อง custom build ที่ผูก
    #     driver esp32-camera ของ Espressif (เช่น lemariva/micropython-camera-driver หรือ mp_camera)
    #   - PSRAM (Pseudo-Static RAM) ใช้พักเฟรมภาพ/โมเดล AI → flash variant ที่เปิด SPIRAM
    #   - ที่นี่จะ: init กล้อง → จับเฟรม → ส่งออก (MQTT/HTTP) — ยังไม่ทำในเฟส skeleton
    raise NotImplementedError("camera capture ยังไม่ทำในเฟสนี้ (skeleton)")


# =============================================================================
# ส่วนที่ 7: NTP (Network Time Protocol) — ต้อง sync "ก่อน" TLS handshake
# gotcha: ESP32-S3 ไม่มี battery-backed RTC → เวลาเพี้ยน → CERT_REQUIRED ตรวจ
#         cert validity (วันที่) แล้ว fail. ต้องตั้งเวลาให้ถูกก่อน connect
# =============================================================================
async def _run_blocking(fn):
    # helper: ห่อ sync function ให้ await ได้ (รันทันที — ใช้คู่ wait_for_ms)
    fn()


async def ntp_sync():
    # ครอบ timeout กัน DNS/UDP ค้าง (blocking call); retry สั้นๆ ตาม NTP_RETRIES
    import ntptime
    ntptime.host = cfg.NTP_HOST
    for attempt in range(cfg.NTP_RETRIES):
        try:
            await asyncio.wait_for_ms(_run_blocking(ntptime.settime), cfg.NTP_TIMEOUT_MS)
            st.ntp_ok = True
            print("[ntp] sync สำเร็จ (attempt", attempt + 1, ")")
            return
        except Exception as e:
            print("[ntp] sync ล้มเหลว (attempt {}):".format(attempt + 1), e)
            if hw_wdt is not None:
                hw_wdt.feed()  # feed ระหว่าง retry boot phase กัน Hardware WDT ตัดกลางคัน
            await asyncio.sleep_ms(500)
    st.ntp_ok = False
    print("[ntp] sync ไม่สำเร็จครบทุก retry — เดินหน้าต่อ (handshake อาจ fail → reset → ลองใหม่)")


# =============================================================================
# ส่วนที่ 8: WiFi connect (เอง) — ต้องขึ้นก่อน NTP + TLS handshake
# mqtt_as ผูก WiFi+MQTT ใน connect() เดียว จึงยก WiFi ขึ้นเองก่อน เพื่อให้ NTP ทำงานได้
# จากนั้นปล่อยให้ mqtt_as จัดการ steady-state reconnect (backoff) เอง
# =============================================================================
async def wifi_connect():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        print("[wifi] เชื่อมต่ออยู่แล้ว:", sta.ifconfig()[0])
        return True

    backoff = cfg.BACKOFF_BASE_MS
    while True:
        print("[wifi] กำลังต่อ SSID:", cfg.WIFI_SSID)
        try:
            sta.connect(cfg.WIFI_SSID, cfg.WIFI_PASS)
        except Exception as e:
            print("[wifi] connect error:", e)

        # รอ associate แบบ bounded (ใช้ asyncio.sleep — ห้าม time.sleep)
        waited = 0
        while not sta.isconnected() and waited < cfg.WIFI_CONNECT_TIMEOUT_MS:
            await asyncio.sleep_ms(250)
            waited += 250
            if hw_wdt is not None:
                hw_wdt.feed()  # feed ระหว่างรอ WiFi (boot phase) กัน Hardware WDT ตัดกลางคัน

        if sta.isconnected():
            print("[wifi] ต่อสำเร็จ:", sta.ifconfig()[0])
            return True

        # ยังไม่ติด → backoff แล้วลองใหม่ (ไม่ busy-loop); เพดานที่ BACKOFF_MAX_MS
        print("[wifi] ต่อไม่ติดใน {}ms → backoff {}ms".format(cfg.WIFI_CONNECT_TIMEOUT_MS, backoff))
        await asyncio.sleep_ms(backoff)
        if hw_wdt is not None:
            hw_wdt.feed()
        backoff = min(backoff * 2, cfg.BACKOFF_MAX_MS)


# =============================================================================
# ส่วนที่ 9: GLOBAL EXCEPTION HANDLER — critical task ตาย → reset อย่างมีระเบียบ
# *** gateway ไม่มี actuator จึงไม่มีอะไรต้อง set SAFE — แค่ reset เพื่อกู้สถานะ ***
# =============================================================================
def _exception_handler(loop, context):
    print("[fatal] uncaught exception ใน task:", context.get("exception"))
    # ไม่ feed WDT → Hardware WDT จะ reset เองภายใน ~8s; reset ทันทีเพื่อกู้เร็วกว่า
    machine.reset()


# =============================================================================
# ส่วนที่ 10: BOOT (sync helper) + MAIN (async) — ลำดับบูตตามสเปค section 5
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

    # (1) boot — *** gateway ไม่มี actuator → ไม่ขับ GPIO output ที่กระทบปั๊ม/วาล์วเลย ***
    st.last_reset_cause = decode_reset_cause()
    print("[boot] reset_cause =", st.last_reset_cause)

    # ตั้ง global exception handler ให้ event loop
    asyncio.get_event_loop().set_exception_handler(_exception_handler)

    # (2) เปิด Hardware WDT (Watchdog Timer) ให้เร็วที่สุดหลังเข้า main
    #     *** Hardware Watchdog = วงจร WDT ในชิป ESP32-S3 — นับถอยหลังอิสระจาก CPU ***
    #     ระหว่างบูต (WiFi/NTP/TLS) จะ feed เป็นจุดๆ ใน wifi_connect/ntp_sync เพื่อกัน reboot loop
    hw_wdt = WDT(timeout=cfg.HW_WDT_MS)
    print("[boot] Hardware WDT เปิด timeout =", cfg.HW_WDT_MS, "ms")

    # (3) ต่อ WiFi เอง (ต้องขึ้นก่อน NTP)
    await wifi_connect()
    hw_wdt.feed()

    # (4) sync NTP "ก่อน" TLS handshake (gotcha cert validity)
    await ntp_sync()
    hw_wdt.feed()

    # (5) MQTT connect (TLS, CERT_REQUIRED, CA cert จาก secrets) + ตั้ง LWT ตอน config
    if MQTTClient is None:
        print("[boot] ไม่พบ mqtt_as — คัดลอก mqtt_as.py ลงบอร์ดก่อน")
        await asyncio.sleep_ms(2000)
        machine.reset()

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
        # TLS params: บังคับ verify broker ด้วย CERT_REQUIRED + CA cert (private CA / self-signed)
        import ssl
        params = {
            "server_hostname": cfg.MQTT_HOST,
            "cert_reqs": ssl.CERT_REQUIRED,
        }
        if cfg.MQTT_SSL_CA is not None:
            params["cadata"] = cfg.MQTT_SSL_CA
        else:
            print("[boot] เตือน: ไม่มี MQTT_SSL_CA ใน secrets.py — CERT_REQUIRED จะ verify ไม่ผ่าน")
        mqtt_base["ssl_params"] = params
    # LWT (Last Will and Testament, retained): broker จะ publish offline ให้เองเมื่อ gateway หาย
    mqtt_base["will"] = (AVAIL_TOPIC, "offline", True, 1)
    mqtt_base["subs_cb"] = on_message
    mqtt_base["connect_coro"] = on_connect
    MQTTClient.DEBUG = True
    client = MQTTClient(mqtt_base)

    # connect ครอบ timeout (< HW_WDT_MS เพื่อ fit ใน 1 รอบ Hardware WDT)
    # เกิน = เน็ตมีปัญหาจริง → log → reset แล้วลองใหม่ (mqtt_as จัดการ backoff รอบถัดไป)
    try:
        await asyncio.wait_for_ms(client.connect(), cfg.MQTT_CONNECT_TIMEOUT_MS)
        st.mqtt_connected = True
        print("[boot] MQTT connect สำเร็จ (TLS, CERT_REQUIRED)")
    except Exception as e:
        print("[boot] MQTT connect ล้มเหลว/timeout:", e)
        st.mqtt_connected = False
        await asyncio.sleep_ms(2000)
        machine.reset()

    # publish ข้อมูลบูต (online ถูกประกาศใน on_connect แล้ว — ส่ง last_reset เพิ่มไว้ debug)
    await pub_metric("last_reset", st.last_reset_cause, retain=True)

    # (6) spawn 3 task → asyncio.gather
    tasks = [
        asyncio.create_task(mqtt_task()),
        asyncio.create_task(health_task()),
        asyncio.create_task(watchdog_task()),
    ]
    print("[boot] 3 task เริ่มทำงาน (mqtt/health/watchdog) — เข้าสู่ loop หลัก")
    await asyncio.gather(*tasks)


# =============================================================================
# ENTRY POINT (sync)
# *** gateway ไม่มี actuator → ไม่มี GPIO ที่ต้องตั้ง SAFE ก่อนเริ่ม event loop ***
# =============================================================================
try:
    asyncio.run(main())
except Exception as e:
    # หลุดจาก event loop ด้วยเหตุใดก็ตาม → reset (fail-safe สุดท้าย; ไม่มี actuator ต้อง safe)
    print("[fatal] หลุดจาก main loop:", e)
    sys.print_exception(e)
    machine.reset()
