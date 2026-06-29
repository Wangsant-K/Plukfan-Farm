/* =============================================================================
   mock-data.js — ตัวจำลองข้อมูล node "สวนปลูกฝัน" (ไม่ต้องต่อ MQTT/ฮาร์ดแวร์จริง)

   จำลองให้ตรงกับ firmware (picow_node.py):
     - FSM 5 สถานะ: INIT → IDLE → WATERING → COOLDOWN → ERROR
     - ปั๊มทำงานเฉพาะ WATERING เท่านั้น (safe-by-structure)
     - ความชื้นลดลงเรื่อย ๆ ตอนไม่รด, เพิ่มขึ้นตอนปั๊มทำงาน
     - threshold ตามโซน (veggie/banana) เหมือน config.py
     - guard: float switch (น้ำในถัง), rain detection, sensor fault, no-water window
   หมายเหตุ: เวลาในโหมดสาธิตถูกเร่งให้เห็นการเปลี่ยนสถานะภายในไม่กี่วินาที
   ========================================================================== */

const PlukfanMock = (() => {
  "use strict";

  // เกณฑ์ความชื้นตามโซน — ตรงกับ THRESHOLDS_ZONE ใน config.py
  const ZONES = {
    veggie: { id: "veggie", label: "ผักสวนครัว", crop: "กะเพรา", node: "picow-01",
              low: 60, high: 70 },
    banana: { id: "banana", label: "กล้วย",       crop: "กล้วยน้ำว้า", node: "picow-02",
              low: 60, high: 75 },
  };

  // สร้าง state เริ่มต้นของแต่ละโซน (เลียนแบบ class State ใน firmware)
  function freshState(z) {
    return {
      zone: z.id,
      fsm: "INIT",
      moisture: 64 + Math.random() * 6,   // %
      temp: 30 + Math.random() * 3,
      humid: 62 + Math.random() * 8,
      ph: 1700 + ((Math.random() * 450) | 0),   // ค่าดิบ 12-bit (map เป็น pH ที่ฝั่งแสดงผล)
      ec: 1200 + ((Math.random() * 200) | 0),
      pumpOn: false,
      floatOk: true,         // น้ำในถังพอ
      isRaining: false,
      sensorFault: false,
      mqttConnected: false,
      inNoWaterWindow: false,
      pumpTimeoutCount: 0,
      lastError: "",
      waterStartMs: 0,
      cooldownStartMs: 0,
      lastStopMs: Date.now(),
      uptimeS: (Math.random() * 4000) | 0,
      freemem: 110000 + ((Math.random() * 20000) | 0),
      rssi: -52 - ((Math.random() * 18) | 0),
      lastReset: "power_on",
      updatedMs: Date.now(),
    };
  }

  const states = {};
  Object.values(ZONES).forEach((z) => { states[z.id] = freshState(z); });

  // ── Gateway (ESP32-S3 "esp32s3-01") — คนละชนิด node กับ Pico W ──────────────
  //   สะท้อน esp32s3_gateway.py / gateway_config.py:
  //     - availability online/offline (retained + LWT)
  //     - diagnostics sys/{freemem,uptime,rssi,temp} ทุก ~15s
  //     - dual watchdog: Hardware WDT (8000ms) + Software WDT (heartbeat ราย task)
  //     - กล้อง = stub (capture → not_implemented)
  //     - cmd: ping → pong / reboot / capture
  const GW = {
    id: "esp32s3-01",
    prefix: "plukfan",
    hwWdtMs: 8000,                 // HW_WDT_MS
    staleMqtt: 5000,               // HB_LIMIT_MS.mqtt
    staleHealth: 3000,             // HB_LIMIT_MS.health
    publishS: 15,                  // HEALTH_PUBLISH_S
  };

  function freshGateway() {
    return {
      id: GW.id,
      online: false,
      uptimeS: (Math.random() * 6000) | 0,
      freemem: 180000 + ((Math.random() * 40000) | 0),  // ESP32-S3 มี RAM เยอะกว่า Pico W
      rssi: -48 - ((Math.random() * 16) | 0),
      temp: 41 + Math.random() * 4,        // อุณหภูมิชิป (°C)
      hbMqttMs: 200,                       // อายุ heartbeat task mqtt (ms)
      hbHealthMs: 150,                     // อายุ heartbeat task health (ms)
      cameraReady: false,                  // stub — ยังไม่รองรับ
      lastReset: "power_on",
      lastDiagS: 0,                        // นับถอยหลังคาบ publish diagnostics
      rebooting: false,
      lastError: "",
      updatedMs: Date.now(),
    };
  }

  const gateway = freshGateway();

  // ── พารามิเตอร์จำลอง (เร่งเวลาเทียบ config จริงเพื่อให้เห็นผลเร็ว) ──
  const TICK_MS = 1200;            // คาบ simulation
  const DRY_RATE = 1.4;            // ความชื้นลดต่อ tick ตอนไม่รด (%)
  const WET_RATE = 4.2;            // ความชื้นเพิ่มต่อ tick ตอนปั๊มทำงาน (%)
  const COOLDOWN_TICKS = 6;        // จำนวน tick ใน COOLDOWN
  const MAX_RUN_TICKS = 14;        // เพดานเวลา WATERING (จำลอง MAX_RUN_S)

  let running = true;
  let timer = null;
  const listeners = new Set();

  function emit() {
    const snapshot = JSON.parse(JSON.stringify(states));
    listeners.forEach((fn) => fn(snapshot));
  }

  // ── หนึ่ง tick ของ FSM ต่อหนึ่งโซน (ตรรกะสะท้อน irrigation_fsm_task) ──
  function stepZone(z) {
    const s = states[z.id];
    const now = Date.now();
    s.updatedMs = now;
    s.uptimeS += Math.round(TICK_MS / 1000);

    // diagnostics ขยับเล็กน้อยให้ดูมีชีวิต
    s.freemem += (Math.random() * 1600 - 800) | 0;
    s.rssi = Math.max(-86, Math.min(-44, s.rssi + ((Math.random() * 4 - 2) | 0)));
    s.temp += Math.random() * 0.4 - 0.2;
    s.humid = Math.max(40, Math.min(95, s.humid + (Math.random() * 2 - 1)));
    // pH/EC (ค่าดิบ) ขยับช้า ๆ ให้ดูมีชีวิต — การ map เป็น pH ทำที่ฝั่งแสดงผล
    s.ph = Math.max(1400, Math.min(2400, s.ph + (Math.random() * 30 - 15)));
    s.ec = Math.max(800, Math.min(1800, s.ec + (Math.random() * 40 - 20)));

    // เน็ตขึ้นหลัง 1 รอบแรก (เลียนแบบ connect phase)
    if (!s.mqttConnected) { s.mqttConnected = true; }

    // ── safety overrides (เหมือน firmware: เช็คก่อนทุกอย่าง) ──
    if (["IDLE", "WATERING", "COOLDOWN"].includes(s.fsm)) {
      if (!s.mqttConnected)      return enterError(s, "network_loss (WiFi/MQTT หลุด)");
      if (!s.floatOk)            return enterError(s, "tank_empty (float switch)");
      if (s.sensorFault)         return enterError(s, "sensor_fault");
    }

    switch (s.fsm) {
      case "INIT":
        s.pumpOn = false;
        s.lastStopMs = now;
        s.fsm = "IDLE";
        break;

      case "IDLE": {
        // ความชื้นค่อย ๆ ลด
        s.moisture = Math.max(0, s.moisture - DRY_RATE * (0.8 + Math.random() * 0.4));
        const ready =
          !s.sensorFault &&
          s.moisture < z.low &&
          !s.isRaining &&
          s.floatOk &&
          !s.inNoWaterWindow;
        if (ready) {
          s.pumpOn = true;
          s.waterStartMs = now;
          s._runTicks = 0;
          s.fsm = "WATERING";
        }
        break;
      }

      case "WATERING": {
        s._runTicks = (s._runTicks || 0) + 1;
        s.moisture = Math.min(100, s.moisture + WET_RATE * (0.85 + Math.random() * 0.3));
        if (s.moisture >= z.high) {
          // รดสำเร็จ → COOLDOWN
          s.pumpOn = false;
          s.pumpTimeoutCount = 0;
          s.cooldownStartMs = now;
          s._coolTicks = 0;
          s.fsm = "COOLDOWN";
        } else if (s._runTicks >= MAX_RUN_TICKS) {
          // ชนเพดานเวลา → นับ timeout
          s.pumpOn = false;
          s.pumpTimeoutCount += 1;
          s.cooldownStartMs = now;
          s._coolTicks = 0;
          if (s.pumpTimeoutCount >= 3) {
            enterError(s, `pump_timeout วนซ้ำ ${s.pumpTimeoutCount} ครั้ง`, true);
          } else {
            s.fsm = "COOLDOWN";
          }
        }
        break;
      }

      case "COOLDOWN":
        s.pumpOn = false;
        s._coolTicks = (s._coolTicks || 0) + 1;
        // ระหว่าง cooldown ความชื้นยังลดช้า ๆ
        s.moisture = Math.max(0, s.moisture - DRY_RATE * 0.3);
        if (s._coolTicks >= COOLDOWN_TICKS) s.fsm = "IDLE";
        break;

      case "ERROR":
        s.pumpOn = false;
        // transient (ไม่ critical) → เหตุหายเอง → auto กลับ IDLE
        if (!s._critical && s.mqttConnected && s.floatOk && !s.sensorFault) {
          s.lastError = "";
          s.lastStopMs = now;
          s.cooldownStartMs = now;
          s.fsm = "IDLE";
        }
        break;
    }
  }

  function enterError(s, reason, critical = false) {
    s.pumpOn = false;
    s.fsm = "ERROR";
    s.lastError = (critical ? "critical " : "") + "ERROR: " + reason;
    s._critical = critical;
    s.lastStopMs = Date.now();
  }

  // ── หนึ่ง tick ของ gateway (diagnostics + watchdog heartbeat) ──
  function stepGateway() {
    const g = gateway;
    const now = Date.now();
    g.updatedMs = now;

    if (g.rebooting) return;   // ระหว่าง reboot: เงียบ รอ timer ปลุกกลับ online

    // ต่อเน็ตหลัง tick แรก (เลียนแบบ connect phase ของ firmware)
    if (!g.online) { g.online = true; }

    g.uptimeS += Math.round(TICK_MS / 1000);

    // diagnostics ขยับเล็กน้อยให้ดูมีชีวิต
    g.freemem += (Math.random() * 2400 - 1200) | 0;
    g.freemem = Math.max(120000, Math.min(230000, g.freemem));
    g.rssi = Math.max(-82, Math.min(-40, g.rssi + ((Math.random() * 4 - 2) | 0)));
    g.temp = Math.max(35, Math.min(58, g.temp + (Math.random() * 0.6 - 0.3)));

    // Software Watchdog: task สด → อายุ heartbeat ต่ำ (ตอกใหม่ทุกรอบ)
    g.hbMqttMs = (Math.random() * 400) | 0;     // < staleMqtt (5000) = สด
    g.hbHealthMs = (Math.random() * 300) | 0;   // < staleHealth (3000) = สด

    // คาบ publish diagnostics (HEALTH_PUBLISH_S)
    g.lastDiagS = (g.lastDiagS + Math.round(TICK_MS / 1000)) % GW.publishS;
  }

  function tick() {
    if (!running) return;
    Object.values(ZONES).forEach(stepZone);
    stepGateway();
    emit();
  }

  // ── public API ──
  return {
    ZONES,
    GW,
    getZone: (id) => ZONES[id],
    getState: (id) => JSON.parse(JSON.stringify(states[id])),
    getAll:   () => JSON.parse(JSON.stringify(states)),
    getGateway: () => JSON.parse(JSON.stringify(gateway)),

    onUpdate(fn) { listeners.add(fn); return () => listeners.delete(fn); },

    start() {
      if (timer) clearInterval(timer);
      running = true;
      tick();
      timer = setInterval(tick, TICK_MS);
    },
    setRunning(v) { running = v; },
    isRunning() { return running; },

    // ── การกระทำจำลอง (ปุ่มบน UI) ──
    manualReset(id) {
      const s = states[id];
      if (s.fsm !== "ERROR") return { ok: false, msg: "ไม่ได้อยู่ในสถานะ ERROR" };
      if (!s.floatOk)      return { ok: false, msg: "ปฏิเสธ: น้ำในถังยังไม่พอ" };
      if (s.sensorFault)   return { ok: false, msg: "ปฏิเสธ: เซนเซอร์ยังอ่านค่าไม่ปกติ" };
      if (s.pumpTimeoutCount >= 3 && s._critical) {
        // critical จาก timeout วนซ้ำ — เคลียร์ตัวนับให้ reset ผ่านในโหมดสาธิต
        s.pumpTimeoutCount = 0;
      }
      s.fsm = "IDLE";
      s._critical = false;
      s.lastError = "";
      s.lastStopMs = Date.now();
      s.cooldownStartMs = Date.now();
      emit();
      return { ok: true, msg: "Manual reset สำเร็จ → กลับสู่ IDLE" };
    },

    // ── ตัวกระตุ้นสถานการณ์ (ไว้สาธิต/ทดสอบ UI) ──
    toggleTankEmpty(id) {
      const s = states[id]; s.floatOk = !s.floatOk; emit();
      return s.floatOk;
    },
    triggerRain(id) {
      const s = states[id];
      s.isRaining = true;
      s.moisture = Math.min(100, s.moisture + 12);
      setTimeout(() => { s.isRaining = false; emit(); }, 9000);
      emit();
    },

    // ── คำสั่งไปยัง gateway (plukfan/node/esp32s3-01/cmd) — จำลองการตอบกลับ ──
    gatewayCmd(cmd) {
      const g = gateway;
      if (!g.online && cmd !== "reboot") {
        return { ok: false, msg: "gateway ออฟไลน์ — ส่งคำสั่งไม่ได้" };
      }
      switch (cmd) {
        case "ping": {
          const rtt = 8 + ((Math.random() * 40) | 0);
          return { ok: true, msg: `pong จาก ${g.id} (${rtt} ms)` };
        }
        case "capture":
          // กล้องเป็น stub ใน firmware → ตอบ not_implemented
          return { ok: false, msg: "capture → not_implemented (กล้องยังเป็น stub)" };
        case "reboot": {
          g.rebooting = true;
          g.online = false;
          g.lastError = "";
          emit();
          // จำลอง machine.reset() → ชิปบูตใหม่ → uptime=0 → กลับ online
          setTimeout(() => {
            g.rebooting = false;
            g.uptimeS = 0;
            g.lastReset = "software_reset";
            g.online = true;
            g.lastDiagS = 0;
            emit();
          }, 3000);
          return { ok: true, msg: "ส่ง reboot แล้ว — gateway กำลังบูตใหม่…" };
        }
        default:
          return { ok: false, msg: "คำสั่งไม่รู้จัก" };
      }
    },
  };
})();
