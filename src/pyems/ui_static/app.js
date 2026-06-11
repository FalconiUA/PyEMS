let site = null;
let sitePath = "";
let siteChoices = [];
let simSitePath = "";
let profiles = [];
let profileRequirements = [];
let liveRows = [];
let errorLog = [];
let currentProfile = null;
let liveTimer = null;
const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
}
function setStatus(message, kind = "") {
  const el = $("status");
  el.textContent = message;
  el.className = "status" + (kind ? " " + kind : "");
}
function rememberErrorEntry(entry) {
  if (!entry) return false;
  const exists = errorLog.some((item) => item.id === entry.id && item.source === entry.source);
  if (!exists) errorLog = [entry, ...errorLog].slice(0, 100);
  renderLogs();
  return true;
}
function rememberClientError(message) {
  return rememberErrorEntry({
    id: `browser-${Date.now()}`,
    logged_at: new Date().toLocaleString(),
    level: "error",
    source: "browser",
    message,
  });
}
function handleError(error) {
  const message = error && error.message ? error.message : String(error);
  if (!error || !error.logged) rememberClientError(message);
  setStatus(message, "error");
}
function parseNum(value) {
  const normalized = String(value ?? "").replace(",", ".").trim();
  return Number(normalized || 0);
}
function setValue(id, value) {
  const el = $(id);
  if (el) el.value = value ?? "";
}
function scenarioCfg() {
  site.scenario ||= {};
  return site.scenario;
}
function allocationCfg() {
  site.allocation ||= { channels: [] };
  if (!site.allocation.channels.length) {
    site.allocation.channels.push({ setpoint_channel: "pv.WSet", p_min_w: 0, p_max_w: 100000, default_w: 100000, ramp_rate_w_per_s: 5000, deadband_w: 200 });
  }
  return site.allocation.channels[0];
}
function headroomCfg() {
  site.setpoint_headroom ||= {};
  return site.setpoint_headroom;
}
function deviceOptions(selected, filter = null) {
  return site.devices
    .filter((device) => !filter || filter(device))
    .map((device) => `<option value="${esc(device.id)}"${device.id === selected ? " selected" : ""}>${esc(device.id)} (${esc(device.profile)})</option>`)
    .join("");
}
function profileOptions(selected) {
  return profiles.map((profile) => `<option value="${esc(profile)}"${profile === selected ? " selected" : ""}>${esc(profile)}</option>`).join("");
}
function syncScenarioForm() {
  const scen = scenarioCfg();
  const alloc = allocationCfg();
  if ($("scenario.control_mode")) scen.control_mode = $("scenario.control_mode").value || "export_limit";
  if ($("scenario.active_power_limit_w")) scen.active_power_limit_w = parseNum($("scenario.active_power_limit_w").value);
  if ($("scenario.connection_point_device_id")) scen.connection_point_device_id = $("scenario.connection_point_device_id").value;
  if ($("scenario.unit_device_id")) scen.unit_device_id = $("scenario.unit_device_id").value;
  if ($("scenario.pid_tuning")) scen.pid_tuning = $("scenario.pid_tuning").value || "auto";
  if ($("scenario.export_priority")) scen.export_priority = parseNum($("scenario.export_priority").value);
  if ($("scenario.regulation_priority")) scen.regulation_priority = parseNum($("scenario.regulation_priority").value);
  if ($("allocation.p_min_w")) alloc.p_min_w = parseNum($("allocation.p_min_w").value);
  if ($("allocation.p_max_w")) alloc.p_max_w = parseNum($("allocation.p_max_w").value);
  if ($("allocation.default_w")) alloc.default_w = parseNum($("allocation.default_w").value);
  if ($("allocation.ramp_rate_w_per_s")) alloc.ramp_rate_w_per_s = parseNum($("allocation.ramp_rate_w_per_s").value);
  if ($("allocation.ramp_down_w_per_s")) {
    const down = $("allocation.ramp_down_w_per_s").value;
    if (down === "") delete alloc.ramp_down_w_per_s;
    else alloc.ramp_down_w_per_s = parseNum(down);
  }
  if ($("allocation.deadband_w")) alloc.deadband_w = parseNum($("allocation.deadband_w").value);
  const headroom = headroomCfg();
  if ($("setpoint_headroom.headroom_w")) headroom.headroom_w = parseNum($("setpoint_headroom.headroom_w").value);
  if ($("setpoint_headroom.headroom_pct")) headroom.headroom_pct = parseNum($("setpoint_headroom.headroom_pct").value);
}

async function loadPages() {
  const views = [...document.querySelectorAll("[data-page]")];
  await Promise.all(views.map(async (view) => {
    const response = await fetch(view.dataset.page);
    if (!response.ok) throw new Error(`Cannot load ${view.dataset.page}`);
    view.innerHTML = await response.text();
  }));
}

function siteFileName(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}
function editingSimSite() {
  return sitePath && simSitePath && sitePath === simSitePath;
}
function renderSiteFile() {
  const sel = $("siteFileSelect");
  if (!sel) return;
  sel.innerHTML = siteChoices.map((path) => {
    const label = siteFileName(path) + (path === simSitePath ? " (simulation)" : " (hardware)");
    return `<option value="${esc(path)}"${path === sitePath ? " selected" : ""}>${esc(label)}</option>`;
  }).join("");
}
function applyConfigPayload(data) {
  site = data.site;
  sitePath = data.site_path || sitePath;
  siteChoices = data.site_choices || siteChoices;
  simSitePath = data.sim_site_path || simSitePath;
  profiles = data.profiles;
  profileRequirements = data.profile_requirements;
  liveRows = data.live_rows;
  renderSiteFile();
}

function renderAll() {
  renderScenario();
  renderSiteYaml();
  renderProfileSelector();
  renderRealtime();
  renderLogs();
}

function renderScenario() {
  const scen = scenarioCfg();
  const alloc = allocationCfg();
  setValue("scenario.control_mode", scen.control_mode || "export_limit");
  setValue("scenario.active_power_limit_w", scen.active_power_limit_w ?? 0);
  $("scenario.connection_point_device_id").innerHTML = deviceOptions(scen.connection_point_device_id, (d) => !String(d.profile).startsWith("inverters/"));
  $("scenario.unit_device_id").innerHTML = deviceOptions(scen.unit_device_id, (d) => String(d.profile).startsWith("inverters/") || d.id !== scen.connection_point_device_id);
  setValue("scenario.connection_point_device_id", scen.connection_point_device_id);
  setValue("scenario.unit_device_id", scen.unit_device_id);
  setValue("allocation.p_min_w", alloc.p_min_w);
  setValue("allocation.p_max_w", alloc.p_max_w);
  setValue("allocation.default_w", alloc.default_w);
  setValue("allocation.ramp_rate_w_per_s", alloc.ramp_rate_w_per_s);
  setValue("allocation.ramp_down_w_per_s", alloc.ramp_down_w_per_s ?? "");
  setValue("allocation.deadband_w", alloc.deadband_w);
  const headroom = headroomCfg();
  setValue("setpoint_headroom.headroom_w", headroom.headroom_w ?? Math.round((alloc.p_max_w || 100000) * 0.1));
  setValue("setpoint_headroom.headroom_pct", headroom.headroom_pct ?? 0);
  $("limitLabel").firstChild.textContent = scen.control_mode === "import_limit" ? "Import limit at connection point, W" : "Export limit at connection point, W";
  const cp = scen.connection_point_device_id || "grid";
  const unit = scen.unit_device_id || "pv";
  $("bindingRows").innerHTML = [
    ["Connection point active power input", `${cp}.W`, "Read from the grid connection meter."],
    ["Unit active power input", `${unit}.W`, "Read from the controlled generating unit."],
    ["Unit active power setpoint", `${unit}.WSet`, "Only PowerAllocator writes this channel."],
    ["Safety status", "sys.safe_mode", "1 means communication safety trip."],
    ["Comms age", "sys.comms_age_s", "Seconds since the last successful Modbus read."],
  ].map(([name, tag, hint]) => `<tr><td>${esc(name)}</td><td class="nowrap"><strong>${esc(tag)}</strong></td><td>${esc(hint)}</td></tr>`).join("");
  renderRequirementRows(profileRequirements, "requirementRows");
}

function renderSiteYaml() {
  const scen = scenarioCfg();
  const gains = site.connection_point_active_power.gains || {};
  setValue("control.fast_cycle_s", site.control.fast_cycle_s);
  setValue("control.poll_interval_s", site.control.poll_interval_s);
  setValue("safety.max_comms_age_s", site.safety.max_comms_age_s);
  setValue("scenario.pid_tuning", scen.pid_tuning || "auto");
  setValue("scenario.export_priority", scen.export_priority ?? 5);
  setValue("scenario.regulation_priority", scen.regulation_priority ?? 10);
  setValue("pid.kp", gains.kp);
  setValue("pid.ki", gains.ki);
  setValue("pid.kd", gains.kd);
  setValue("pid.tt", gains.tt);
  const manual = (scen.pid_tuning || "auto") === "manual";
  document.querySelectorAll("#pidFields input").forEach((input) => input.readOnly = !manual);
  $("deviceRows").innerHTML = site.devices.map((device, idx) => `
    <tr data-device-index="${idx}">
      <td><input data-field="id" value="${esc(device.id)}" required></td>
      <td><select data-field="profile" required>${profileOptions(device.profile)}</select></td>
      <td><input data-field="host" value="${esc(device.host)}" required></td>
      <td><input data-field="port" type="number" step="1" value="${device.port ?? ""}"></td>
      <td><input data-field="slave_id" type="number" step="1" min="0" value="${device.slave_id ?? 1}" required></td>
      <td><button type="button" data-remove-device="${idx}">Remove</button></td>
    </tr>
  `).join("");
}

function renderProfileSelector() {
  $("profileDeviceSelect").innerHTML = site.devices.map((device) => `<option value="${esc(device.id)}">${esc(device.id)} - ${esc(device.profile)}</option>`).join("");
  if (!$("profileDeviceSelect").value && site.devices[0]) $("profileDeviceSelect").value = site.devices[0].id;
}

function renderRequirementRows(requirements, targetId) {
  $(targetId).innerHTML = requirements.map((item) => {
    const tagClass = item.present ? "ok" : "bad";
    const label = item.present ? `OK @ ${item.address}` : "Missing";
    return `<tr>
      <td>${esc(item.device_id)}</td>
      <td>${esc(item.profile)}</td>
      <td>${esc(item.field)}</td>
      <td><strong>${esc(item.expected_tag)}</strong></td>
      <td><span class="tag ${tagClass}">${esc(label)}</span></td>
    </tr>`;
  }).join("");
}

function renderProfile(profilePayload) {
  currentProfile = profilePayload;
  setValue("profile.model", profilePayload.profile.model);
  setValue("profile.protocol", profilePayload.profile.protocol);
  setValue("profile.default_port", profilePayload.profile.default_port);
  $("profileRequiredRows").innerHTML = profilePayload.requirements.map((item) => {
    const tagClass = item.present ? "ok" : "bad";
    return `<tr><td>${esc(item.field)}</td><td>${esc(item.expected_tag)}</td><td>${esc(item.profile_channel || "")}</td><td><span class="tag ${tagClass}">${item.present ? "OK" : "Missing"}</span></td></tr>`;
  }).join("");
  $("registerRows").innerHTML = profilePayload.profile.registers.map((reg, idx) => {
    const required = profilePayload.requirements.some((item) => item.register_index === idx);
    return `<tr data-register-index="${idx}">
      <td>${required ? '<span class="tag ok">required</span>' : ""}</td>
      <td><input data-field="channel" value="${esc(reg.channel)}" required></td>
      <td><input data-field="address" type="number" step="1" value="${reg.address}" required></td>
      <td><select data-field="type">${["int16","uint16","int32","uint32"].map((t) => `<option value="${t}"${reg.type === t ? " selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><input data-field="scale" type="number" step="0.0001" value="${reg.scale}" required></td>
      <td><input data-field="unit" value="${esc(reg.unit ?? "")}"></td>
      <td><select data-field="access"><option value="read"${reg.access === "read" ? " selected" : ""}>read</option><option value="read_write"${reg.access === "read_write" ? " selected" : ""}>read_write</option></select></td>
      <td><input data-field="min_val" type="number" step="1" value="${Number.isFinite(reg.min_val) ? reg.min_val : ""}"></td>
      <td><input data-field="max_val" type="number" step="1" value="${Number.isFinite(reg.max_val) ? reg.max_val : ""}"></td>
      <td><button type="button" data-remove-register="${idx}">Remove</button></td>
    </tr>`;
  }).join("");
}

function renderRealtime(readData = null) {
  const scen = scenarioCfg();
  const devices = site.devices.map((device) => device.id).join(", ");
  const modeLabel = scen.control_mode === "import_limit" ? "Import limit" : "Export limit";
  const lastRead = readData ? readData.read_at : "not read yet";
  $("summary").innerHTML = [
    [modeLabel, `${scen.active_power_limit_w ?? 0} W`],
    ["Connection point meter", scen.connection_point_device_id || ""],
    ["Controlled unit", scen.unit_device_id || ""],
    ["Devices", devices || "none"],
    ["Last read", lastRead],
  ].map(([name, value]) => `<div class="metric"><div class="name">${esc(name)}</div><div class="value">${esc(value)}</div></div>`).join("");
  renderLiveRows(readData ? readData.rows : liveRows);
}

function formatValue(value) {
  if (value === null || value === undefined) return "0";
  if (typeof value === "number") return Math.abs(value) >= 1000 ? value.toFixed(1) : (Number.isInteger(value) ? String(value) : value.toFixed(3));
  return String(value);
}
function renderLiveRows(rows) {
  $("liveRows").innerHTML = (rows || []).map((row) => `
    <tr>
      <td>${esc(row.device)}</td><td>${esc(row.channel)}</td><td>${esc(formatValue(row.value))}</td>
      <td>${esc(row.unit)}</td><td><span class="tag">${esc(row.access)}</span></td><td>${esc(row.role)}</td>
    </tr>
  `).join("");
}
function renderLogs() {
  const rows = $("errorLogRows");
  if (!rows) return;
  const empty = $("errorLogEmpty");
  if (empty) empty.hidden = errorLog.length > 0;
  rows.innerHTML = errorLog.map((entry) => `
    <tr>
      <td class="nowrap">${esc(entry.logged_at)}</td>
      <td>${esc(entry.source)}</td>
      <td><span class="tag bad">${esc(entry.level || "error")}</span></td>
      <td class="log-message">${esc(entry.message)}</td>
    </tr>
  `).join("");
}

function gatherDevices() {
  return [...document.querySelectorAll("[data-device-index]")].map((row) => {
    const data = {};
    for (const input of row.querySelectorAll("[data-field]")) {
      const field = input.dataset.field;
      if (field === "port") {
        if (input.value !== "") data[field] = parseNum(input.value);
      } else if (field === "slave_id") {
        data[field] = parseNum(input.value);
      } else {
        data[field] = input.value.trim();
      }
    }
    return data;
  });
}

function gatherSite() {
  const next = JSON.parse(JSON.stringify(site));
  next.devices = gatherDevices();
  next.scenario = {
    control_mode: $("scenario.control_mode").value,
    active_power_limit_w: parseNum($("scenario.active_power_limit_w").value),
    connection_point_device_id: $("scenario.connection_point_device_id").value,
    unit_device_id: $("scenario.unit_device_id").value,
    pid_tuning: $("scenario.pid_tuning").value,
    export_priority: parseNum($("scenario.export_priority").value),
    regulation_priority: parseNum($("scenario.regulation_priority").value),
  };
  next.control = {
    fast_cycle_s: parseNum($("control.fast_cycle_s").value),
    poll_interval_s: parseNum($("control.poll_interval_s").value),
  };
  next.safety = { max_comms_age_s: parseNum($("safety.max_comms_age_s").value), unit_active_power_setpoint_channels: [] };
  next.connection_point_active_power ||= {};
  next.connection_point_active_power.gains = {
    kp: parseNum($("pid.kp").value),
    ki: parseNum($("pid.ki").value),
    kd: parseNum($("pid.kd").value),
    tt: parseNum($("pid.tt").value),
  };
  const allocChannel = {
    setpoint_channel: "",
    p_min_w: parseNum($("allocation.p_min_w").value),
    p_max_w: parseNum($("allocation.p_max_w").value),
    default_w: parseNum($("allocation.default_w").value),
    ramp_rate_w_per_s: parseNum($("allocation.ramp_rate_w_per_s").value),
    deadband_w: parseNum($("allocation.deadband_w").value),
  };
  if ($("allocation.ramp_down_w_per_s").value !== "") {
    allocChannel.ramp_down_w_per_s = parseNum($("allocation.ramp_down_w_per_s").value);
  }
  next.allocation = { channels: [allocChannel] };
  next.setpoint_headroom = {
    ...(site.setpoint_headroom || {}),
    headroom_w: parseNum($("setpoint_headroom.headroom_w").value),
    headroom_pct: parseNum($("setpoint_headroom.headroom_pct").value),
  };
  return next;
}

function gatherProfile() {
  return {
    model: $("profile.model").value.trim(),
    protocol: $("profile.protocol").value,
    default_port: parseNum($("profile.default_port").value),
    registers: [...document.querySelectorAll("[data-register-index]")].map((row) => {
      const data = {};
      for (const input of row.querySelectorAll("[data-field]")) {
        const field = input.dataset.field;
        if (["address", "scale", "min_val", "max_val"].includes(field)) {
          if (input.value !== "") data[field] = parseNum(input.value);
        } else {
          data[field] = input.value;
        }
      }
      return data;
    }),
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.error || response.statusText);
    if (data.error_entry) error.logged = rememberErrorEntry(data.error_entry);
    throw error;
  }
  return data;
}
async function loadErrorLog() {
  const data = await api("/api/error-log");
  errorLog = data.entries || [];
  renderLogs();
  return data;
}
async function loadConfig() {
  setStatus("Loading configuration...");
  const data = await api("/api/config");
  applyConfigPayload(data);
  renderAll();
  await loadSelectedProfile();
  await loadErrorLog();
  const name = siteFileName(sitePath);
  setStatus(data.validation.ok ? `Editing ${name}.` : data.validation.error, data.validation.ok ? "ok" : "warn");
}
async function saveConfig() {
  const name = siteFileName(sitePath) || "site.yaml";
  setStatus(`Saving ${name}...`);
  const data = await api("/api/config", { method: "POST", body: JSON.stringify({ site: gatherSite() }) });
  applyConfigPayload(data);
  renderAll();
  await loadSelectedProfile();
  setStatus(editingSimSite()
    ? `${name} saved. Configs are read at startup: restart the simulator AND the EMS (pyems --site ${sitePath}) to apply.`
    : `${name} saved.`, "ok");
  return data;
}
async function switchSiteFile(path) {
  await api("/api/site-file", { method: "POST", body: JSON.stringify({ path }) });
  await loadConfig();
}
async function loadSelectedProfile() {
  if (!site || !site.devices.length) return;
  const deviceId = $("profileDeviceSelect").value || site.devices[0].id;
  const data = await api(`/api/profile?device_id=${encodeURIComponent(deviceId)}`);
  renderProfile(data);
}
async function saveProfile() {
  if (!currentProfile) return;
  setStatus("Saving profile YAML...");
  const data = await api("/api/profile", { method: "POST", body: JSON.stringify({ profile_path: currentProfile.profile_path, profile: gatherProfile(), device_id: currentProfile.device_id }) });
  currentProfile = data;
  renderProfile(data);
  const cfg = await api("/api/config");
  profileRequirements = cfg.profile_requirements;
  renderRequirementRows(profileRequirements, "requirementRows");
  setStatus("Profile YAML saved.", "ok");
}
async function testRead() {
  await saveConfig();
  setStatus("Reading devices once...");
  const data = await api("/api/test-read", { method: "POST", body: "{}" });
  renderRealtime(data);
  setStatus(`Read ${data.rows.length} channels in ${data.read_s.toFixed(3)} s.`, "ok");
  showView("realtime");
}
async function startLive() {
  await saveConfig();
  setStatus("Starting live read...");
  await api("/api/live/start", { method: "POST", body: "{}" });
  await refreshLive();
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = setInterval(() => refreshLive().catch(handleError), 1000);
  setStatus("Live read is running.", "ok");
}
async function refreshLive() {
  const data = await api("/api/live");
  renderRealtime(data);
  return data;
}
async function stopLive() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = null;
  await api("/api/live/stop", { method: "POST", body: "{}" });
  setStatus("Live read stopped.", "ok");
}
async function clearErrorLog() {
  const data = await api("/api/error-log/clear", { method: "POST", body: "{}" });
  errorLog = data.entries || [];
  renderLogs();
  setStatus("Error log cleared.", "ok");
}
let simStatus = null;
let simTimer = null;
function simPanelUrl() {
  return simStatus ? `http://${location.hostname}:${simStatus.panel_port}/` : "";
}
function renderSim() {
  const text = $("simStateText");
  if (!text || !simStatus) return;
  const running = simStatus.reachable;
  text.textContent = running
    ? (simStatus.managed ? "Simulator running (started from this UI)." : "Simulator running (started externally).")
    : "Simulator is not running.";
  text.className = "hint " + (running ? "ok" : "");
  $("simStartBtn").disabled = running;
  $("simStopBtn").disabled = !running;
  $("simOpenBtn").disabled = !running;
  $("simSitePath").textContent = simStatus.sim_site;
  $("simEmsCmd").textContent = simStatus.ems_command;
  const frame = $("simFrame");
  frame.hidden = !running;
  if (running && !frame.src) frame.src = simPanelUrl();
  if (!running) frame.removeAttribute("src");
}
async function refreshSim() {
  simStatus = await api("/api/sim/status");
  renderSim();
}
async function startSim() {
  setStatus("Starting device simulator...");
  $("simStartBtn").disabled = true;
  await api("/api/sim/start", { method: "POST", body: "{}" });
  await refreshSim();
  setStatus(`Simulator running. Start the EMS with: ${simStatus.ems_command}`, "ok");
}
async function stopSim() {
  await api("/api/sim/stop", { method: "POST", body: "{}" });
  await refreshSim();
  setStatus("Simulator stopped.", "ok");
}

function showView(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === name));
  if (name === "simulation") {
    refreshSim().catch(handleError);
    if (!simTimer) simTimer = setInterval(() => refreshSim().catch(() => {}), 3000);
  } else if (simTimer) {
    clearInterval(simTimer);
    simTimer = null;
  }
}

document.addEventListener("change", async (event) => {
  const id = event.target.id || "";
  if (id.startsWith("scenario.") || id.startsWith("allocation.") || id.startsWith("setpoint_headroom.")) {
    syncScenarioForm();
    renderScenario();
    renderRealtime();
  }
  if (id === "scenario.pid_tuning") renderSiteYaml();
  if (event.target.id === "profileDeviceSelect") loadSelectedProfile().catch(handleError);
  if (event.target.id === "siteFileSelect") switchSiteFile(event.target.value).catch(handleError);
});
document.addEventListener("click", async (event) => {
  const target = event.target;
  if (target.matches(".tab")) showView(target.dataset.view);
  if (target.id === "reloadBtn") loadConfig().catch(handleError);
  if (target.id === "saveBtn" || target.id === "saveSiteBtn") saveConfig().catch(handleError);
  if (target.id === "testReadBtn") testRead().catch(handleError);
  if (target.id === "startLiveBtn") startLive().catch(handleError);
  if (target.id === "refreshLiveBtn") refreshLive().catch(handleError);
  if (target.id === "stopLiveBtn") stopLive().catch(handleError);
  if (target.id === "saveProfileBtn") saveProfile().catch(handleError);
  if (target.id === "refreshErrorLogBtn") loadErrorLog().catch(handleError);
  if (target.id === "clearErrorLogBtn") clearErrorLog().catch(handleError);
  if (target.id === "simStartBtn") startSim().catch((error) => { handleError(error); refreshSim().catch(() => {}); });
  if (target.id === "simStopBtn") stopSim().catch(handleError);
  if (target.id === "simOpenBtn" && simStatus) window.open(simPanelUrl(), "_blank");
  if (target.id === "addDeviceBtn") {
    site.devices.push({ id: "unit" + (site.devices.length + 1), profile: profiles[0] || "", host: "", slave_id: 1 });
    renderAll();
  }
  if (target.id === "addRegisterBtn" && currentProfile) {
    currentProfile.profile.registers.push({ channel: "device.W", address: 0, type: "int32", scale: 1, unit: "W", access: "read" });
    renderProfile(currentProfile);
  }
  if (target.dataset.removeDevice !== undefined) {
    site.devices.splice(Number(target.dataset.removeDevice), 1);
    renderAll();
  }
  if (target.dataset.removeRegister !== undefined && currentProfile) {
    currentProfile.profile.registers.splice(Number(target.dataset.removeRegister), 1);
    renderProfile(currentProfile);
  }
});

loadPages()
  .then(loadConfig)
  .catch(handleError);
