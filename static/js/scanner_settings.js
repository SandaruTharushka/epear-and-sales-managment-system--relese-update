/**
 * scanner_settings.js
 * -------------------
 * Drives the Scanner Settings page:
 *   - Detect HID keyboard devices via /api/scanner/devices
 *   - Load/save config via /api/scanner/config
 *   - Poll bridge status via /api/scanner/status
 *   - Test scan injection via /api/scanner/test-scan
 *   - Duplicate-scan protection: ignore same barcode within 700 ms (frontend)
 */

(function () {
  "use strict";

  // ── duplicate-scan protection (frontend mirror of backend) ─────────────────
  const DEBOUNCE_MS = 700;
  const _lastScan = {};   // barcode → timestamp

  function isDuplicate(barcode) {
    const now  = Date.now();
    const last = _lastScan[barcode] || 0;
    if (now - last < DEBOUNCE_MS) return true;
    _lastScan[barcode] = now;
    return false;
  }

  // ── toast notification ─────────────────────────────────────────────────────
  let _toastTimer = null;
  function toast(msg, type = "ok") {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className   = `show ${type}`;
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.className = ""; }, 3000);
  }

  // ── API helpers ────────────────────────────────────────────────────────────
  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    return res.json();
  }

  // ── load initial state ─────────────────────────────────────────────────────
  async function init() {
    await loadConfig();
    await detectDevices();
    await refreshStatus();
    startStatusPoll();
    startLastScanPoll();
  }

  // ── status ─────────────────────────────────────────────────────────────────
  async function refreshStatus() {
    try {
      const data = await api("/api/scanner/status");
      setStatus("bridge",   data.bridge_running,    "Bridge running", "Bridge stopped");
      setStatus("sales",    data.sales_configured,  "Sales scanner assigned", "Sales scanner: not assigned");
      setStatus("workshop", data.workshop_configured,"Workshop scanner assigned", "Workshop scanner: not assigned");
    } catch {
      setDot("bridge",   "error");
      document.getElementById("lbl-bridge").textContent = "Cannot reach server";
    }
  }

  function setStatus(key, ok, msgOk, msgWarn) {
    setDot(key, ok ? "ok" : "warn");
    document.getElementById(`lbl-${key}`).textContent = ok ? msgOk : msgWarn;
  }

  function setDot(key, cls) {
    const d = document.getElementById(`dot-${key}`);
    d.className = `status-dot ${cls}`;
  }

  let _statusPoll = null;
  function startStatusPoll() {
    _statusPoll = setInterval(refreshStatus, 10_000);
  }

  // ── load config ────────────────────────────────────────────────────────────
  async function loadConfig() {
    try {
      const data = await api("/api/scanner/config");
      if (!data.ok) return;
      const cfg = data.config;
      document.getElementById("inp-api").value      = cfg.api_url      || "";
      document.getElementById("inp-debounce").value  = cfg.debounce_ms  || 700;
      window._currentSalesId    = cfg.sales_scanner_device_id    || "";
      window._currentWorkshopId = cfg.workshop_scanner_device_id || "";
    } catch (e) {
      toast("Could not load config: " + e.message, "err");
    }
  }

  // ── detect devices ─────────────────────────────────────────────────────────
  async function detectDevices() {
    try {
      const data = await api("/api/scanner/devices");
      renderDeviceList(data.devices || []);
      populateSelects(data.devices || []);
    } catch (e) {
      toast("Device detection failed: " + e.message, "warn");
    }
  }

  function renderDeviceList(devices) {
    const ul   = document.getElementById("device-list");
    const none = document.getElementById("no-devices-msg");
    ul.innerHTML = "";

    if (!devices.length) {
      const li = document.createElement("li");
      li.className = "device-item";
      li.innerHTML = `<span class="device-name">No HID keyboard devices detected (Windows only)</span>`;
      ul.appendChild(li);
      return;
    }

    const salesId    = window._currentSalesId    || "";
    const workshopId = window._currentWorkshopId || "";

    for (const dev of devices) {
      const role = dev.device_id === salesId    ? "sales"
                 : dev.device_id === workshopId ? "workshop"
                 : null;
      const badgeCls  = role === "sales"    ? "badge-sales"
                      : role === "workshop" ? "badge-workshop"
                      : "badge-unassigned";
      const badgeTxt  = role === "sales"    ? "Sales"
                      : role === "workshop" ? "Workshop"
                      : "Unassigned";
      const li = document.createElement("li");
      li.className = "device-item";
      li.innerHTML = `
        <span class="badge ${badgeCls}">${badgeTxt}</span>
        <span class="device-name" title="${esc(dev.friendly_name)}">${esc(dev.friendly_name)}</span>
        <span class="device-id"  title="${esc(dev.device_id)}">${esc(dev.device_id)}</span>
      `;
      ul.appendChild(li);
    }
  }

  function populateSelects(devices) {
    const salesId    = window._currentSalesId    || "";
    const workshopId = window._currentWorkshopId || "";

    for (const selId of ["sel-sales", "sel-workshop"]) {
      const sel  = document.getElementById(selId);
      const cur  = selId === "sel-sales" ? salesId : workshopId;
      sel.innerHTML = `<option value="">— Not assigned —</option>`;
      for (const dev of devices) {
        const opt = document.createElement("option");
        opt.value = dev.device_id;
        opt.textContent = dev.friendly_name;
        if (dev.device_id === cur) opt.selected = true;
        sel.appendChild(opt);
      }
    }
  }

  // ── save config ────────────────────────────────────────────────────────────
  async function saveConfig() {
    const salesId    = document.getElementById("sel-sales").value;
    const workshopId = document.getElementById("sel-workshop").value;
    const apiUrl     = document.getElementById("inp-api").value.trim();
    const debounce   = parseInt(document.getElementById("inp-debounce").value) || 700;

    if (salesId && salesId === workshopId) {
      toast("Sales and Workshop scanners must be different devices.", "warn");
      return;
    }

    try {
      const data = await api("/api/scanner/config", {
        method: "POST",
        body: JSON.stringify({
          sales_scanner_device_id:    salesId    || null,
          workshop_scanner_device_id: workshopId || null,
          api_url:     apiUrl,
          debounce_ms: debounce,
        }),
      });
      if (data.ok) {
        window._currentSalesId    = salesId;
        window._currentWorkshopId = workshopId;
        toast("Settings saved successfully.", "ok");
        await detectDevices();
        await refreshStatus();
      } else {
        toast("Save failed: " + (data.error || "unknown error"), "err");
      }
    } catch (e) {
      toast("Save error: " + e.message, "err");
    }
  }

  // ── test scan ──────────────────────────────────────────────────────────────
  async function sendTestScan() {
    const barcode = "TEST-" + Math.floor(Math.random() * 9000 + 1000);
    try {
      const data = await api("/api/scanner/test-scan", {
        method: "POST",
        body: JSON.stringify({ barcode }),
      });
      if (data.ok) {
        showLastScan(barcode + " (test)");
        toast("Test scan sent: " + barcode, "ok");
      }
    } catch (e) {
      toast("Test scan failed: " + e.message, "err");
    }
  }

  // ── last scan display ──────────────────────────────────────────────────────
  function showLastScan(barcode) {
    if (isDuplicate(barcode)) return;
    const el = document.getElementById("last-scan");
    el.textContent = barcode;
    el.classList.remove("flash");
    void el.offsetWidth;   // reflow to restart animation
    el.classList.add("flash");
  }

  function clearLastScan() {
    document.getElementById("last-scan").textContent = "—";
  }

  // ── poll last workshop scan from status endpoint ───────────────────────────
  let _prevLastScan = "";
  async function pollLastScan() {
    try {
      const data = await api("/api/scanner/status");
      if (data.last_workshop_scan && data.last_workshop_scan !== _prevLastScan) {
        _prevLastScan = data.last_workshop_scan;
        showLastScan(data.last_workshop_scan);
      }
    } catch { /* silent */ }
  }

  function startLastScanPoll() {
    setInterval(pollLastScan, 2000);
  }

  // ── utils ──────────────────────────────────────────────────────────────────
  function esc(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── expose to onclick attrs ────────────────────────────────────────────────
  window.refreshStatus  = refreshStatus;
  window.detectDevices  = detectDevices;
  window.saveConfig     = saveConfig;
  window.sendTestScan   = sendTestScan;
  window.clearLastScan  = clearLastScan;

  // ── init ───────────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", init);
})();
