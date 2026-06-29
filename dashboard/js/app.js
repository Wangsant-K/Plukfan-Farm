/* =============================================================================
   app.js — render dashboard + จัดการ interaction (touch-first)
   ผูกกับ PlukfanMock (mock-data.js). เปลี่ยนเป็น MQTT-over-WebSocket จริงได้
   โดยแทน source ที่ฟัง onUpdate (ดู README)
   ========================================================================== */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const GAUGE_LEN = 282.7;   // ความยาว arc ของ gauge (คำนวณจาก r=90, ครึ่งวงกลม)

  // ── ป้ายกำกับ + คำอธิบายของแต่ละสถานะ FSM ──
  const FSM_INFO = {
    INIT:     { label: "กำลังเริ่มระบบ", desc: "ตั้งค่าอุปกรณ์ให้ปลอดภัยก่อนเริ่มทำงาน", color: "#8fae9d" },
    IDLE:     { label: "พร้อมทำงาน",     desc: "เฝ้าดูความชื้น รอเงื่อนไขเริ่มรดน้ำ",       color: "#38d27a" },
    WATERING: { label: "กำลังรดน้ำ",     desc: "ปั๊มทำงาน เปิดวาล์วน้ำหยด รดจนถึงเกณฑ์หยุด", color: "#4fb6f0" },
    COOLDOWN: { label: "พักหลังรด",      desc: "พักระบบป้องกันรดถี่เกินไป",                  color: "#f5b53d" },
    ERROR:    { label: "พบข้อผิดพลาด",   desc: "ปั๊มหยุดทำงานเพื่อความปลอดภัย รอการแก้ไข",    color: "#f06464" },
  };

  let activeZone = "veggie";

  // ── การแปลงค่า pH ──────────────────────────────────────────────
  // firmware ส่ง pH เป็นค่าดิบ 12-bit (0–4095) แล้ว "map ที่ backend" (ดู picow_node.py)
  // ที่นี่ใช้ calibration เชิงสาธิตแบบเชิงเส้น: ค่าดิบเต็มสเกล → pH 0–14
  // (ของจริงควร calibrate 2 จุดด้วยน้ำยาบัฟเฟอร์ pH 4.0 / 7.0)
  const PH_ADC_MAX = 4095;
  function rawToPh(raw) {
    return Math.max(0, Math.min(14, (raw / PH_ADC_MAX) * 14));
  }
  // ป้ายบอกความเป็นกรด–ด่าง (อิงเกณฑ์ดินเพาะปลูกทั่วไป)
  function phLabel(ph) {
    if (ph < 5.5)  return { text: "กรดจัด",   cls: "acid-strong" };
    if (ph < 6.5)  return { text: "กรดอ่อน",  cls: "acid" };
    if (ph <= 7.3) return { text: "เป็นกลาง", cls: "neutral" };
    if (ph <= 8.3) return { text: "ด่างอ่อน", cls: "base" };
    return { text: "ด่างจัด", cls: "base-strong" };
  }

  // ───────────────────────── Zone tabs ─────────────────────────
  function buildZoneTabs() {
    const wrap = $("zoneTabs");
    wrap.innerHTML = "";
    Object.values(PlukfanMock.ZONES).forEach((z) => {
      const btn = document.createElement("button");
      btn.className = "zone-tab" + (z.id === activeZone ? " is-active" : "");
      btn.type = "button";
      btn.dataset.zone = z.id;
      btn.innerHTML = `<span class="zone-tab__dot"></span>${z.label} · ${z.crop}`;
      btn.addEventListener("click", () => {
        activeZone = z.id;
        document.querySelectorAll(".zone-tab").forEach((t) =>
          t.classList.toggle("is-active", t.dataset.zone === activeZone));
        render(PlukfanMock.getAll());
      });
      wrap.appendChild(btn);
    });
  }

  // ───────────────────────── Helpers ─────────────────────────
  function fmtUptime(sec) {
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m ${sec % 60}s`;
  }

  function fmtAgo(ms) {
    const s = Math.round((Date.now() - ms) / 1000);
    if (s < 2) return "เมื่อกี้";
    if (s < 60) return `${s} วินาทีก่อน`;
    return `${Math.floor(s / 60)} นาทีก่อน`;
  }

  // วาง band ของ gauge (ช่วง low–high) ด้วย stroke-dash
  function setGaugeBands(z) {
    const lowOff  = GAUGE_LEN * (1 - z.low / 100);
    const highOff = GAUGE_LEN * (1 - z.high / 100);
    const bandLow = $("bandLow"), bandHigh = $("bandHigh");
    // band low: 0→low (amber), band high: high→100 (green)
    bandLow.style.strokeDasharray  = `${GAUGE_LEN}`;
    bandLow.style.strokeDashoffset = `${lowOff}`;
    bandHigh.style.strokeDasharray = `${GAUGE_LEN * z.high / 100} ${GAUGE_LEN}`;
    bandHigh.style.strokeDashoffset = "0";
    // ทำให้ band high แสดงเฉพาะส่วน high→100 โดยซ้อนทับ track
    bandHigh.style.strokeDasharray = `${GAUGE_LEN}`;
    bandHigh.style.strokeDashoffset = `${highOff}`;
    bandHigh.style.opacity = ".0";  // ใช้ band low เป็นโซนเตือนหลัก ก็พอ
  }

  // ───────────────────────── Render ─────────────────────────
  function render(all) {
    const z = PlukfanMock.getZone(activeZone);
    const s = all[activeZone];
    if (!s) return;

    // connection
    const conn = $("conn");
    conn.classList.toggle("conn--online", s.mqttConnected);
    conn.classList.toggle("conn--offline", !s.mqttConnected);
    $("connLabel").textContent = s.mqttConnected ? "ออนไลน์" : "ออฟไลน์";

    // hero / fsm
    const info = FSM_INFO[s.fsm] || FSM_INFO.INIT;
    $("fsmState").textContent = info.label;
    $("fsmState").style.color = info.color;
    $("fsmDesc").textContent = info.desc;
    $("fsmBadge").style.color = info.color;
    $("heroCard").dataset.state = s.fsm;

    // pump
    const pumpViz = $("pumpViz");
    pumpViz.classList.toggle("is-on", s.pumpOn);
    $("pumpState").textContent = s.pumpOn ? "กำลังทำงาน" : "หยุดทำงาน";
    $("pumpState").style.color = s.pumpOn ? "#4fb6f0" : "var(--text)";
    $("pumpMeta").textContent = s.pumpOn
      ? "เปิดวาล์วน้ำหยด · รดจนถึง " + z.high + "%"
      : (s.fsm === "COOLDOWN" ? "อยู่ในช่วงพักหลังรด" : "");

    // reset button — โผล่เฉพาะตอน ERROR
    $("resetBtn").hidden = s.fsm !== "ERROR";

    // moisture gauge
    const m = s.sensorFault ? null : s.moisture;
    const fill = $("gaugeFill");
    if (m == null) {
      $("moistValue").textContent = "--";
      fill.style.strokeDashoffset = GAUGE_LEN;
    } else {
      $("moistValue").textContent = Math.round(m);
      fill.style.strokeDasharray = `${GAUGE_LEN}`;
      fill.style.strokeDashoffset = `${GAUGE_LEN * (1 - m / 100)}`;
      // สีตามเกณฑ์: ต่ำกว่า low = amber, ระหว่าง = เขียวอ่อน, ถึง high = เขียว
      fill.style.stroke = m < z.low ? "#f5b53d" : (m >= z.high ? "#38d27a" : "#7fd99f");
    }
    $("thLow").textContent = z.low;
    $("thHigh").textContent = z.high;
    setGaugeBands(z);

    // sensor stats (เซนเซอร์ fault → แสดง stale)
    setStat("vTemp", s.sensorFault ? null : s.temp, 1, "statTemp");
    setStat("vHumid", s.sensorFault ? null : s.humid, 0, "statHumid");
    renderPh(s.sensorFault ? null : s.ph);
    setStat("vEc", s.sensorFault ? null : s.ec, 0, "statEc");

    // guards
    setGuard("gFloatVal", s.floatOk ? "พอ" : "น้ำหมด", s.floatOk ? "ok" : "bad");
    setGuard("gRainVal", s.isRaining ? "ใช่" : "ไม่", s.isRaining ? "warn" : "idle");
    setGuard("gSensorVal", s.sensorFault ? "ผิดปกติ" : "ปกติ", s.sensorFault ? "bad" : "ok");
    setGuard("gWindowVal", s.inNoWaterWindow ? "ห้ามรด" : "รดได้", s.inNoWaterWindow ? "warn" : "idle");

    // diagnostics
    $("dNode").textContent = z.node;
    $("dUptime").textContent = fmtUptime(s.uptimeS);
    $("dMem").textContent = (s.freemem / 1024).toFixed(0) + " KB";
    $("dRssi").textContent = s.rssi + " dBm";
    $("dReset").textContent = s.lastReset;
    $("dUpdated").textContent = fmtAgo(s.updatedMs);

    const err = $("lastErr");
    if (s.lastError) { err.hidden = false; err.textContent = "⚠️ " + s.lastError; }
    else { err.hidden = true; }

    renderGateway();
  }

  // ───────────────────────── Gateway render ─────────────────────────
  function renderGateway() {
    const g = PlukfanMock.getGateway();
    const GW = PlukfanMock.GW;

    $("gwId").textContent = g.id;

    const badge = $("gwBadge");
    const card = $("gatewayCard");
    if (g.rebooting) {
      badge.textContent = "กำลังบูต…";
      badge.className = "gw-badge gw-badge--boot";
      card.dataset.state = "boot";
    } else if (g.online) {
      badge.textContent = "ออนไลน์";
      badge.className = "gw-badge gw-badge--online";
      card.dataset.state = "online";
    } else {
      badge.textContent = "ออฟไลน์";
      badge.className = "gw-badge gw-badge--offline";
      card.dataset.state = "offline";
    }

    $("gwUptime").textContent  = fmtUptime(g.uptimeS);
    $("gwMem").textContent     = (g.freemem / 1024).toFixed(0) + " KB";
    $("gwRssi").textContent    = g.rssi + " dBm";
    $("gwTemp").textContent    = g.temp.toFixed(1) + " °C";
    $("gwReset").textContent   = g.lastReset;
    $("gwUpdated").textContent = fmtAgo(g.updatedMs);

    // Dual watchdog — HW เลี้ยงปกติ, SW เทียบ heartbeat กับเกณฑ์ staleness
    const live = g.online && !g.rebooting;
    setGwStat("gwWdtHwVal", live ? `เลี้ยงปกติ (${GW.hwWdtMs / 1000}s)` : "—", live ? "ok" : "idle");
    const mqttFresh = live && g.hbMqttMs < GW.staleMqtt;
    const healthFresh = live && g.hbHealthMs < GW.staleHealth;
    setGwStat("gwWdtMqttVal", live ? `สด · ${g.hbMqttMs}ms` : "—", mqttFresh ? "ok" : (live ? "bad" : "idle"));
    setGwStat("gwWdtHealthVal", live ? `สด · ${g.hbHealthMs}ms` : "—", healthFresh ? "ok" : (live ? "bad" : "idle"));

    // กล้อง = stub
    setGwStat("gwCamVal", g.cameraReady ? "พร้อม" : "stub (ยังไม่รองรับ)", g.cameraReady ? "ok" : "idle");
  }

  function setGwStat(id, text, cls) {
    const el = $(id);
    el.textContent = text;
    el.className = "gw-stat__val " + cls;
  }

  // pH: แปลงค่าดิบ → pH (0–14) + ป้ายกรด/ด่าง พร้อมสี
  function renderPh(raw) {
    const card = $("statPh");
    const b = $("vPh");
    const tag = $("vPhTag");
    if (raw == null) {
      b.textContent = "--";
      tag.textContent = "";
      tag.className = "ph-tag";
      card.classList.add("is-stale");
      return;
    }
    card.classList.remove("is-stale");
    const ph = rawToPh(raw);
    b.textContent = ph.toFixed(1);
    const info = phLabel(ph);
    tag.textContent = info.text;
    tag.className = "ph-tag " + info.cls;
  }

  function setStat(id, val, digits, cardId) {
    const el = $(id);
    const card = $(cardId);
    if (val == null) { el.textContent = "--"; card.classList.add("is-stale"); }
    else { el.textContent = Number(val).toFixed(digits); card.classList.remove("is-stale"); }
  }

  function setGuard(id, text, cls) {
    const el = $(id);
    el.textContent = text;
    el.className = "guard__val " + cls;
  }

  // ───────────────────────── Toast ─────────────────────────
  let toastTimer = null;
  function toast(msg, ok = true) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    t.style.borderColor = ok ? "var(--green-soft)" : "#f0646488";
    requestAnimationFrame(() => t.classList.add("show"));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => { t.hidden = true; }, 260);
    }, 2600);
  }

  // ───────────────────────── Interactions ─────────────────────────
  function wireControls() {
    $("resetBtn").addEventListener("click", () => {
      const r = PlukfanMock.manualReset(activeZone);
      toast(r.msg, r.ok);
      render(PlukfanMock.getAll());
    });

    $("simToggle").addEventListener("click", () => {
      const next = !PlukfanMock.isRunning();
      PlukfanMock.setRunning(next);
      $("simIcon").textContent = next ? "⏸" : "▶";
      toast(next ? "เริ่มการจำลองข้อมูล" : "หยุดการจำลองข้อมูลชั่วคราว");
    });

    // แตะที่การ์ด "น้ำในถัง" / "ฝน" เพื่อกระตุ้นสถานการณ์ทดสอบ (สาธิต fail-safe)
    $("gFloat").addEventListener("click", () => {
      const ok = PlukfanMock.toggleTankEmpty(activeZone);
      toast(ok ? "จำลอง: เติมน้ำในถังแล้ว" : "จำลอง: น้ำในถังหมด → ระบบจะเข้า ERROR", ok);
    });
    $("gRain").addEventListener("click", () => {
      PlukfanMock.triggerRain(activeZone);
      toast("จำลอง: ฝนตก → งดรดน้ำชั่วคราว");
    });
    $("gSensor").addEventListener("click", () => {
      const fault = PlukfanMock.toggleSensorFault(activeZone);
      toast(fault ? "จำลอง: เซนเซอร์ผิดปกติ → ระบบจะเข้า ERROR"
                  : "จำลอง: เซนเซอร์กลับมาปกติแล้ว", !fault);
    });

    // ── คำสั่ง gateway (สาธิตการตอบกลับของ cmd) ──
    $("gwPing").addEventListener("click", () => {
      const r = PlukfanMock.gatewayCmd("ping");
      toast(r.msg, r.ok);
    });
    $("gwCapture").addEventListener("click", () => {
      const r = PlukfanMock.gatewayCmd("capture");
      toast(r.msg, r.ok);
      const err = $("gwErr");
      err.hidden = false;
      err.textContent = "ℹ️ " + r.msg;
    });
    $("gwReboot").addEventListener("click", () => {
      const r = PlukfanMock.gatewayCmd("reboot");
      toast(r.msg, r.ok);
      renderGateway();
    });
  }

  // ───────────────────────── Boot ─────────────────────────
  function init() {
    buildZoneTabs();
    wireControls();
    PlukfanMock.onUpdate(render);
    PlukfanMock.start();
    // อัปเดต "อัปเดตเมื่อ" ให้เดินแม้ sim หยุด
    setInterval(() => {
      const s = PlukfanMock.getState(activeZone);
      if (s) $("dUpdated").textContent = fmtAgo(s.updatedMs);
      const g = PlukfanMock.getGateway();
      if (g) $("gwUpdated").textContent = fmtAgo(g.updatedMs);
    }, 1000);
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
