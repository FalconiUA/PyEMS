let site = null;
let sitePath = "";
let fieldLabels = {};
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
  fieldLabels = data.field_labels || fieldLabels;
  renderSiteFile();
}

function renderAll() {
  renderOverview();
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

// The <class> prefix of a profile channel is cosmetic (namespaced() swaps it
// for the site.yaml device id), but validate_channel() still requires SOME
// prefix. Reuse the prefix the profile already uses so autocompleted channels
// match its existing rows; fall back to "device" for an empty profile.
function profilePrefix(profile) {
  for (const reg of profile.registers || []) {
    const head = String(reg.channel || "").split(".")[0];
    if (head) return head;
  }
  return "device";
}
function renderChannelVocabulary(profile) {
  const list = $("channelFields");
  if (!list) return;
  const prefix = profilePrefix(profile);
  list.innerHTML = Object.entries(fieldLabels)
    .map(([field, label]) => `<option value="${esc(prefix)}.${esc(field)}">${esc(label)}</option>`)
    .join("");
}

function renderProfile(profilePayload) {
  currentProfile = profilePayload;
  setValue("profile.model", profilePayload.profile.model);
  setValue("profile.protocol", profilePayload.profile.protocol);
  setValue("profile.default_port", profilePayload.profile.default_port);
  renderChannelVocabulary(profilePayload.profile);
  $("profileRequiredRows").innerHTML = profilePayload.requirements.map((item) => {
    const tagClass = item.present ? "ok" : "bad";
    return `<tr><td>${esc(item.field)}</td><td>${esc(item.expected_tag)}</td><td>${esc(item.profile_channel || "")}</td><td><span class="tag ${tagClass}">${item.present ? "OK" : "Missing"}</span></td></tr>`;
  }).join("");
  $("registerRows").innerHTML = profilePayload.profile.registers.map((reg, idx) => {
    const required = profilePayload.requirements.some((item) => item.register_index === idx);
    return `<tr data-register-index="${idx}">
      <td>${required ? '<span class="tag ok">required</span>' : ""}</td>
      <td><input data-field="channel" list="channelFields" value="${esc(reg.channel)}" required></td>
      <td data-decode>${esc(fieldLabel(reg.channel))}</td>
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

function fieldLabel(channel) {
  const name = String(channel ?? "");
  const field = name.includes(".") ? name.slice(name.indexOf(".") + 1) : name;
  return fieldLabels[field] || "";
}
function formatValue(value) {
  if (value === null || value === undefined) return "0";
  if (typeof value === "number") return Math.abs(value) >= 1000 ? value.toFixed(1) : (Number.isInteger(value) ? String(value) : value.toFixed(3));
  return String(value);
}
function renderLiveRows(rows) {
  $("liveRows").innerHTML = (rows || []).map((row) => `
    <tr>
      <td>${esc(row.device)}</td><td>${esc(row.channel)}</td><td>${esc(row.description ?? fieldLabel(row.channel))}</td><td>${esc(formatValue(row.value))}</td>
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

// ── Overview: read-only dashboard fed by the fast-loop telemetry snapshot ────
// All data comes from /api/fast-loop-state (the JSON the running EMS rewrites
// each cycle) plus the already-loaded site config. NO direct Modbus polling.
let overviewTimer = null;
let lastSnapshot = null;
let overviewHistory = [];
const OVERVIEW_HISTORY_S = 600; // power-flows window: last 10 minutes

function snapValues() {
  return (lastSnapshot && lastSnapshot.ok && lastSnapshot.values) || {};
}
// Snapshot semantics: undefined = tag not published, null = +inf/NaN (e.g.
// comms age before the first good read).
function tagPresent(tag) {
  return snapValues()[tag] !== undefined;
}
function tagValue(tag) {
  const value = snapValues()[tag];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function gridDeviceId() {
  return scenarioCfg().connection_point_device_id || "grid";
}
function unitDeviceId() {
  return scenarioCfg().unit_device_id || "pv";
}
function formatQuantity(value, unit) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e6) return (value / 1e6).toFixed(2) + " M" + unit;
  if (abs >= 1e3) return (value / 1e3).toFixed(1) + " k" + unit;
  return (abs >= 100 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)) + " " + unit;
}
function formatPower(w) {
  return formatQuantity(w, "W");
}
function formatSeconds(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return "never";
  if (s < 10) return s.toFixed(1) + " s";
  if (s < 120) return Math.round(s) + " s";
  if (s < 7200) return Math.round(s / 60) + " min";
  return (s / 3600).toFixed(1) + " h";
}
function telemetryStale() {
  if (!lastSnapshot || !lastSnapshot.ok) return false;
  const cycle = Number(lastSnapshot.cycle_s) || 1;
  return typeof lastSnapshot.age_s === "number" && lastSnapshot.age_s > Math.max(5, 3 * cycle);
}
function emsState() {
  if (!lastSnapshot || !lastSnapshot.ok) {
    return { label: "No EMS data", kind: "off", detail: "No fast-loop telemetry snapshot found. Is pyems running?" };
  }
  if (telemetryStale()) {
    return { label: "Telemetry stale", kind: "warn", detail: `Snapshot is ${formatSeconds(lastSnapshot.age_s)} old — the EMS stopped publishing.` };
  }
  if ((tagValue("sys.safe_mode") ?? 0) >= 0.5) {
    return { label: "Safety trip / EMS error", kind: "bad", detail: "sys.safe_mode = 1 — units forced to the safe active power." };
  }
  return { label: "EMS active", kind: "ok", detail: "Telemetry fresh, no safety trip." };
}
function deviceCommsAge(deviceId) {
  const values = snapValues();
  const perDevice = values[`sys.${deviceId}.comms_age_s`];
  if (perDevice !== undefined) return perDevice; // null = never read
  return values["sys.comms_age_s"]; // global fallback when per-device tags absent
}
function deviceCommsLimit(deviceId) {
  const safety = (site && site.safety) || {};
  const perDevice = (safety.device_comms_max_age_s || {})[deviceId];
  return Number(perDevice ?? safety.max_comms_age_s ?? 10);
}
function assetStatus(deviceId) {
  if (!lastSnapshot || !lastSnapshot.ok) return { kind: "off", label: "No data" };
  const age = deviceCommsAge(deviceId);
  if (age === undefined || age === null) return { kind: "bad", label: "Disconnected" };
  if (age > deviceCommsLimit(deviceId)) return { kind: "bad", label: "Stale" };
  if (telemetryStale()) return { kind: "warn", label: "Telemetry stale" };
  if ((tagValue(`${deviceId}.Alarm`) ?? 0) >= 0.5) return { kind: "warn", label: "Alarm" };
  if (deviceId === unitDeviceId() && (tagValue("sys.setpoint_violation") ?? 0) >= 0.5) {
    return { kind: "warn", label: "Setpoint violation" };
  }
  return { kind: "ok", label: "Connected" };
}
function unitEnvelopeMaxW() {
  const channels = (site.allocation && site.allocation.channels) || [];
  const setpointTag = `${unitDeviceId()}.WSet`;
  const channel = channels.find((ch) => ch.setpoint_channel === setpointTag) || channels[0];
  return channel && Number.isFinite(Number(channel.p_max_w)) ? Number(channel.p_max_w) : null;
}
function bessDeviceIds() {
  return site.devices.map((device) => device.id).filter((id) => tagPresent(`${id}.SoC`));
}
function gensetDeviceIds() {
  const taken = new Set([gridDeviceId(), unitDeviceId(), "load", ...bessDeviceIds()]);
  return site.devices
    .filter((device) => !taken.has(device.id))
    .filter((device) => String(device.profile).includes("genset") || /^gen/i.test(String(device.id)))
    .filter((device) => tagPresent(`${device.id}.W`))
    .map((device) => device.id);
}
function loadActivePowerW() {
  if (tagPresent("load.W")) return { value: tagValue("load.W"), estimated: false };
  const gridW = tagValue(`${gridDeviceId()}.W`);
  const unitW = tagValue(`${unitDeviceId()}.W`);
  // generating-unit convention: grid.W = load - generation, so load = grid + unit
  if (gridW === null || unitW === null) return { value: null, estimated: true };
  return { value: gridW + unitW, estimated: true };
}

function renderStatusBar() {
  const el = $("overviewStatusBar");
  if (!el || !site) return;
  const scen = scenarioCfg();
  const state = emsState();
  const live = lastSnapshot && lastSnapshot.ok;
  const modeLabel = scen.control_mode === "import_limit" ? "Import limit" : "Export limit";
  const items = [
    ["Site", siteFileName(sitePath) + (editingSimSite() ? " (simulation)" : "")],
    ["Controller time", live ? lastSnapshot.read_at || "—" : "—"],
    ["Last telemetry", live ? `${formatSeconds(lastSnapshot.age_s)} ago` : "—"],
    ["Control mode", modeLabel],
    ["Configured limit", formatPower(Number(scen.active_power_limit_w ?? 0))],
    ["Comms age", live ? formatSeconds(tagValue("sys.comms_age_s")) : "—"],
    ["Write age", live ? formatSeconds(tagValue("sys.write_age_s")) : "—"],
  ];
  el.innerHTML = `
    <div class="ems-state ${state.kind}" title="${esc(state.detail)}">${esc(state.label)}</div>
    ${items.map(([name, value]) => `<div class="bar-item"><span class="name">${esc(name)}</span><span class="value">${esc(value)}</span></div>`).join("")}
    <button class="bar-btn" disabled title="Start/stop requires a safe service-control API — not wired yet (read-only placeholder).">Start / Stop</button>
  `;
}

function renderStatusCounters() {
  const el = $("overviewCounters");
  if (!el || !site) return;
  if (!lastSnapshot || !lastSnapshot.ok) {
    el.innerHTML = "";
    return;
  }
  let green = 0;
  let yellow = 0;
  let red = 0;
  for (const device of site.devices) {
    const status = assetStatus(device.id);
    if (status.kind === "ok") green += 1;
    else if (status.kind === "warn") yellow += 1;
    else red += 1;
  }
  el.innerHTML = [
    ["ok", green, "connected, no alarms"],
    ["warn", yellow, "with active alarms"],
    ["bad", red, "disconnected / stale"],
  ].map(([kind, count, label]) => `
    <div class="counter ${kind}"><span class="count">${count}</span><span class="label">${esc(label)}</span></div>
  `).join("");
}

function assetCard({ title, subtitle, status, primaryValue, primaryLabel, primaryKind = "", rows = [], badges = [], cardKind = null }) {
  const kind = cardKind || status.kind;
  return `<section class="asset-card state-${esc(kind)}">
    <header>
      <div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div>
      <span class="badge ${esc(status.kind)}">${esc(status.label)}</span>
    </header>
    <div class="primary-metric ${esc(primaryKind)}">
      <span class="value">${esc(primaryValue)}</span>
      <span class="label">${esc(primaryLabel)}</span>
    </div>
    ${badges.length ? `<div class="badges">${badges.map((badge) => `<span class="badge ${esc(badge.kind)}">${esc(badge.label)}</span>`).join("")}</div>` : ""}
    <dl>${rows.filter(Boolean).map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
  </section>`;
}

function gridCardHtml() {
  const dev = gridDeviceId();
  const scen = scenarioCfg();
  const w = tagValue(`${dev}.W`);
  const va = tagValue(`${dev}.VA`);
  const hz = tagValue(`${dev}.Hz`);
  const pf = w !== null && va !== null && va > 0 ? Math.min(1, Math.abs(w) / va) : null;
  const limitW = Number(scen.active_power_limit_w ?? 0);
  const exporting = w !== null && w < 0;
  const overLimit = scen.control_mode !== "import_limit" && exporting && Math.abs(w) > limitW;
  const volts = ["PhVphA", "PhVphB", "PhVphC"].map((field) => tagValue(`${dev}.${field}`)).filter((v) => v !== null);
  let primaryLabel = "—";
  let primaryKind = "flow-import";
  if (w !== null) {
    primaryLabel = exporting ? (overLimit ? "Export — over limit" : "Export") : "Import";
    primaryKind = exporting ? (overLimit ? "flow-over" : "flow-export") : "flow-import";
  }
  return assetCard({
    title: "Grid",
    subtitle: `connection point — ${dev}`,
    status: assetStatus(dev),
    primaryValue: formatPower(w === null ? null : Math.abs(w)),
    primaryLabel,
    primaryKind,
    cardKind: overLimit ? "bad" : null,
    badges: overLimit ? [{ kind: "bad", label: "Export over configured limit" }] : [],
    rows: [
      ["Reactive (Q)", formatQuantity(tagValue(`${dev}.VAR`), "var")],
      ["Apparent (S)", formatQuantity(va, "VA")],
      ["Power factor", pf === null ? "—" : pf.toFixed(2)],
      ["Frequency", hz === null ? "—" : hz.toFixed(2) + " Hz"],
      volts.length ? ["Voltage", volts.map((v) => Math.round(v)).join(" / ") + " V"] : null,
      [scen.control_mode === "import_limit" ? "Import limit" : "Export limit", formatPower(limitW)],
    ],
  });
}

function unitCardHtml() {
  const dev = unitDeviceId();
  const w = tagValue(`${dev}.W`);
  const wset = tagValue(`${dev}.WSet`);
  const pMaxW = unitEnvelopeMaxW();
  const toleranceW = Number((site.setpoint_compliance || {}).tolerance_w ?? 2000);
  const badges = [];
  if (wset !== null && pMaxW !== null && wset < pMaxW) badges.push({ kind: "warn", label: "Curtailed" });
  if (w !== null && wset !== null && w > wset + toleranceW) badges.push({ kind: "warn", label: "Above setpoint" });
  if ((tagValue("sys.setpoint_violation") ?? 0) >= 0.5) badges.push({ kind: "bad", label: "Setpoint violation" });
  const status = tagValue(`${dev}.Status`);
  const opMode = tagValue(`${dev}.OperatingMode`);
  const alarm = tagValue(`${dev}.Alarm`);
  return assetCard({
    title: "PV plant",
    subtitle: `controlled unit — ${dev}`,
    status: assetStatus(dev),
    primaryValue: formatPower(w),
    primaryLabel: "Generated",
    rows: [
      ["Setpoint", formatPower(wset)],
      pMaxW !== null ? ["P_max", formatPower(pMaxW)] : null,
      w !== null && pMaxW ? ["Utilization", Math.round((100 * w) / pMaxW) + " %"] : null,
      ["Reactive (Q)", formatQuantity(tagValue(`${dev}.VAR`), "var")],
      tagPresent(`${dev}.VA`) ? ["Apparent (S)", formatQuantity(tagValue(`${dev}.VA`), "VA")] : null,
      tagPresent(`${dev}.Hz`) ? ["Frequency", tagValue(`${dev}.Hz`) === null ? "—" : tagValue(`${dev}.Hz`).toFixed(2) + " Hz"] : null,
      tagPresent(`${dev}.Status`) ? ["Status word", status === null ? "—" : String(Math.round(status))] : null,
      tagPresent(`${dev}.OperatingMode`) ? ["Operating mode", opMode === null ? "—" : String(Math.round(opMode))] : null,
      tagPresent(`${dev}.Alarm`) ? ["Alarm word", alarm === null ? "—" : String(Math.round(alarm))] : null,
    ],
    badges,
  });
}

function loadCardHtml() {
  const { value, estimated } = loadActivePowerW();
  const hasMeter = !estimated;
  const rows = [];
  if (hasMeter && tagPresent("load.VAR")) rows.push(["Reactive (Q)", formatQuantity(tagValue("load.VAR"), "var")]);
  let status = { kind: "off", label: "No data" };
  if (hasMeter) status = assetStatus("load");
  else if (lastSnapshot && lastSnapshot.ok) {
    // derived value is only as good as its source measurements
    const sources = [assetStatus(gridDeviceId()), assetStatus(unitDeviceId())];
    const worst = sources.find((s) => s.kind === "bad") || sources.find((s) => s.kind === "warn");
    status = worst ? { kind: worst.kind, label: `Sources: ${worst.label.toLowerCase()}` } : { kind: "ok", label: "Derived" };
  }
  return assetCard({
    title: "Load",
    subtitle: hasMeter ? "site consumption — load" : `derived from ${gridDeviceId()}.W + ${unitDeviceId()}.W`,
    status,
    primaryValue: formatPower(value),
    primaryLabel: "Consumption",
    badges: estimated ? [{ kind: "", label: "Estimated" }] : [],
    rows,
  });
}

function bessCardHtml(deviceId) {
  const w = tagValue(`${deviceId}.W`);
  let primaryLabel = "Idle";
  if (w !== null && w > 50) primaryLabel = "Discharging";
  else if (w !== null && w < -50) primaryLabel = "Charging";
  const soc = tagValue(`${deviceId}.SoC`);
  return assetCard({
    title: "BESS",
    subtitle: `storage — ${deviceId}`,
    status: assetStatus(deviceId),
    primaryValue: formatPower(w === null ? null : Math.abs(w)),
    primaryLabel,
    rows: [
      ["State of charge", soc === null ? "—" : soc.toFixed(1) + " %"],
      tagPresent(`${deviceId}.SoH`) ? ["State of health", formatQuantity(tagValue(`${deviceId}.SoH`), "%")] : null,
      ["Reactive (Q)", formatQuantity(tagValue(`${deviceId}.VAR`), "var")],
      tagPresent(`${deviceId}.WSet`) ? ["Setpoint", formatPower(tagValue(`${deviceId}.WSet`))] : null,
    ],
  });
}

function gensetCardHtml(deviceId) {
  const status = tagValue(`${deviceId}.Status`);
  const opMode = tagValue(`${deviceId}.OperatingMode`);
  return assetCard({
    title: "Genset",
    subtitle: `generator — ${deviceId}`,
    status: assetStatus(deviceId),
    primaryValue: formatPower(tagValue(`${deviceId}.W`)),
    primaryLabel: "Output",
    rows: [
      ["Reactive (Q)", formatQuantity(tagValue(`${deviceId}.VAR`), "var")],
      tagPresent(`${deviceId}.VA`) ? ["Apparent (S)", formatQuantity(tagValue(`${deviceId}.VA`), "VA")] : null,
      tagPresent(`${deviceId}.Hz`) ? ["Frequency", tagValue(`${deviceId}.Hz`) === null ? "—" : tagValue(`${deviceId}.Hz`).toFixed(2) + " Hz"] : null,
      tagPresent(`${deviceId}.Status`) ? ["Status word", status === null ? "—" : String(Math.round(status))] : null,
      tagPresent(`${deviceId}.OperatingMode`) ? ["Operating mode", opMode === null ? "—" : String(Math.round(opMode))] : null,
      tagPresent(`${deviceId}.WSet`) ? ["Setpoint", formatPower(tagValue(`${deviceId}.WSet`))] : null,
    ],
  });
}

function safetyCardHtml() {
  const live = lastSnapshot && lastSnapshot.ok;
  const safety = (site && site.safety) || {};
  const safeMode = tagValue("sys.safe_mode");
  const violation = tagValue("sys.setpoint_violation");
  const commsAge = tagValue("sys.comms_age_s");
  const writeAge = tagValue("sys.write_age_s");
  const maxCommsAge = Number(safety.max_comms_age_s ?? 10);
  const maxWriteAge = safety.max_write_age_s !== undefined ? Number(safety.max_write_age_s) : null;
  let cardKind = "ok";
  const badges = [];
  if (!live) cardKind = "off";
  else {
    if ((commsAge === null || commsAge > maxCommsAge) && tagPresent("sys.comms_age_s")) {
      cardKind = "bad";
      badges.push({ kind: "bad", label: "Comms age over limit" });
    }
    if (maxWriteAge !== null && (writeAge === null || writeAge > maxWriteAge)) {
      if (cardKind !== "bad") cardKind = "warn";
      badges.push({ kind: "warn", label: "Write age over limit" });
    }
    if ((violation ?? 0) >= 0.5) {
      if (cardKind === "ok") cardKind = "warn";
      badges.push({ kind: "warn", label: "Setpoint violation" });
    }
    if ((safeMode ?? 0) >= 0.5) {
      cardKind = "bad";
      badges.push({ kind: "bad", label: "Safety trip" });
    }
  }
  const stateLabel = !live ? "No data" : (safeMode ?? 0) >= 0.5 ? "Tripped" : cardKind === "ok" ? "Healthy" : "Degraded";
  return assetCard({
    title: "Safety / EMS",
    subtitle: "why the status above is (not) green",
    status: { kind: live ? cardKind : "off", label: stateLabel },
    primaryValue: !live ? "—" : (safeMode ?? 0) >= 0.5 ? "TRIP" : "OK",
    primaryLabel: "sys.safe_mode",
    cardKind,
    badges,
    rows: [
      ["Safe mode", !live || safeMode === null ? "—" : String(Math.round(safeMode))],
      ["Global comms age", live ? `${formatSeconds(commsAge)} (limit ${formatSeconds(maxCommsAge)})` : "—"],
      ["Write age", live ? `${formatSeconds(writeAge)}${maxWriteAge !== null ? ` (limit ${formatSeconds(maxWriteAge)})` : ""}` : "—"],
      ["Setpoint violation", !live || violation === null ? "—" : String(Math.round(violation))],
    ],
  });
}

function renderAssetCards() {
  const el = $("overviewCards");
  if (!el || !site) return;
  const cards = [gridCardHtml(), unitCardHtml(), loadCardHtml()];
  for (const deviceId of bessDeviceIds()) cards.push(bessCardHtml(deviceId));
  for (const deviceId of gensetDeviceIds()) cards.push(gensetCardHtml(deviceId));
  cards.push(safetyCardHtml());
  el.innerHTML = cards.join("");
}

function renderCommsRows() {
  const el = $("overviewCommsRows");
  if (!el || !site) return;
  el.innerHTML = site.devices.map((device) => {
    const status = assetStatus(device.id);
    const age = lastSnapshot && lastSnapshot.ok ? deviceCommsAge(device.id) : undefined;
    const alarm = tagValue(`${device.id}.Alarm`);
    const endpoint = device.host ? `${device.host}${device.port ? ":" + device.port : ""}` : "—";
    return `<tr>
      <td><strong>${esc(device.id)}</strong></td>
      <td>${esc(device.profile)}</td>
      <td class="nowrap">${esc(endpoint)}</td>
      <td>${esc(age === undefined ? "—" : formatSeconds(age))}</td>
      <td>${tagPresent(`${device.id}.Alarm`) ? esc(alarm === null ? "—" : String(Math.round(alarm))) : "—"}</td>
      <td><span class="badge ${esc(status.kind)}">${esc(status.label)}</span></td>
    </tr>`;
  }).join("");
}

function pushOverviewHistory() {
  if (!lastSnapshot || !lastSnapshot.ok || telemetryStale()) return;
  const t = Date.now() / 1000;
  const grid = tagValue(`${gridDeviceId()}.W`);
  const unit = tagValue(`${unitDeviceId()}.W`);
  const wset = tagValue(`${unitDeviceId()}.WSet`);
  const load = loadActivePowerW().value;
  overviewHistory.push({ t, grid, unit, wset, load });
  const cutoff = t - OVERVIEW_HISTORY_S;
  while (overviewHistory.length && overviewHistory[0].t < cutoff) overviewHistory.shift();
}

function drawOverviewChart() {
  const canvas = $("overviewChart");
  if (!canvas) return;
  const scen = scenarioCfg();
  const width = (canvas.width = canvas.clientWidth || 900);
  const height = canvas.height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  const series = [
    { key: "grid", label: `Grid (${gridDeviceId()}.W)`, color: "#2563eb" },
    { key: "unit", label: `Unit (${unitDeviceId()}.W)`, color: "#16a34a" },
    { key: "wset", label: `Setpoint (${unitDeviceId()}.WSet)`, color: "#9333ea", dash: [6, 4] },
    { key: "load", label: "Load" + (tagPresent("load.W") ? " (load.W)" : " (estimated)"), color: "#d97706" },
  ];
  // export shows up as NEGATIVE grid.W, so the export limit line sits at -limit
  const limitW = scen.control_mode === "import_limit"
    ? Number(scen.active_power_limit_w ?? 0)
    : -Number(scen.active_power_limit_w ?? 0);
  const legend = $("overviewChartLegend");
  if (legend) {
    legend.innerHTML = series
      .map((s) => `<span class="legend-item"><span class="swatch" style="background:${esc(s.color)}"></span>${esc(s.label)}</span>`)
      .join("") + `<span class="legend-item"><span class="swatch dashed"></span>Limit at connection point</span>`;
  }
  if (overviewHistory.length < 2) {
    ctx.fillStyle = "#657086";
    ctx.font = "13px system-ui, sans-serif";
    ctx.fillText("Collecting telemetry…", 12, 24);
    return;
  }
  let minW = limitW;
  let maxW = limitW;
  for (const point of overviewHistory) {
    for (const s of series) {
      const v = point[s.key];
      if (v === null || v === undefined) continue;
      if (v < minW) minW = v;
      if (v > maxW) maxW = v;
    }
  }
  if (minW === maxW) { minW -= 1; maxW += 1; }
  const pad = 0.08 * (maxW - minW);
  minW -= pad;
  maxW += pad;
  const left = 8;
  const right = width - 8;
  const top = 8;
  const bottom = height - 22;
  const now = Date.now() / 1000;
  const x = (t) => left + ((t - (now - OVERVIEW_HISTORY_S)) / OVERVIEW_HISTORY_S) * (right - left);
  const y = (w) => bottom - ((w - minW) / (maxW - minW)) * (bottom - top);
  // horizontal reference lines: zero and the configured limit
  for (const [w, color, label] of [[0, "#d7dde6", "0"], [limitW, "#b3261e", formatPower(limitW)]]) {
    if (w < minW || w > maxW) continue;
    ctx.strokeStyle = color;
    ctx.setLineDash(w === 0 ? [] : [6, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, y(w));
    ctx.lineTo(right, y(w));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color === "#d7dde6" ? "#657086" : color;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(label, left + 2, y(w) - 3);
  }
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    let started = false;
    for (const point of overviewHistory) {
      const v = point[s.key];
      if (v === null || v === undefined) { started = false; continue; }
      const px = x(point.t);
      const py = y(v);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#657086";
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillText("-10 min", left + 2, height - 8);
  ctx.fillText("now", right - 28, height - 8);
}

function renderOverview() {
  if (!site || !$("overviewStatusBar")) return;
  renderStatusBar();
  renderStatusCounters();
  renderAssetCards();
  renderCommsRows();
  drawOverviewChart();
}

async function refreshOverview() {
  const data = await api("/api/fast-loop-state");
  lastSnapshot = data;
  pushOverviewHistory();
  renderOverview();
  return data;
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
async function startFastLoop() {
  setStatus("Starting fast-loop monitor...");
  showView("realtime");
  await refreshFastLoop();
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = setInterval(() => refreshFastLoop().catch(handleError), 1000);
}
async function refreshFastLoop() {
  const data = await api("/api/fast-loop-state");
  renderRealtime(data);
  if (data.ok) {
    setStatus(`Fast-loop state at ${data.read_at ?? "?"} — ${data.rows.length} channels.`, "ok");
  } else {
    setStatus(data.error || "No fast-loop state available.", "warn");
  }
  return data;
}
function stopFastLoop() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = null;
  setStatus("Fast-loop monitor stopped.", "ok");
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
  // Overview polls the fast-loop snapshot only while it is the active view —
  // never /api/live, never a direct Modbus read.
  if (name === "overview") {
    refreshOverview().catch(handleError);
    if (!overviewTimer) overviewTimer = setInterval(() => refreshOverview().catch(() => {}), 1000);
  } else if (overviewTimer) {
    clearInterval(overviewTimer);
    overviewTimer = null;
  }
}

// Secondary in-view navigation: a .subtab swaps which .subview group is shown
// within the same view. Fields stay in the DOM (just hidden), so save/read in
// app.js still finds every scenario.*/allocation.*/setpoint_headroom.* input.
function showSubtab(button) {
  const name = button.dataset.subtab;
  const view = button.closest(".view") || document;
  view.querySelectorAll(".subtab").forEach((b) => b.classList.toggle("active", b.dataset.subtab === name));
  view.querySelectorAll(".subview").forEach((s) => s.classList.toggle("active", s.dataset.subtab === name));
}

document.addEventListener("input", (event) => {
  // live decode while typing a channel name in the profile register editor
  if (event.target.matches('[data-field="channel"]')) {
    const cell = event.target.closest("tr")?.querySelector("[data-decode]");
    if (cell) cell.textContent = fieldLabel(event.target.value) || "— not in vocabulary —";
  }
});
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
  if (target.matches(".subtab")) showSubtab(target);
  if (target.id === "reloadBtn") loadConfig().catch(handleError);
  if (target.id === "saveBtn" || target.id === "saveSiteBtn") saveConfig().catch(handleError);
  if (target.id === "testReadBtn") testRead().catch(handleError);
  if (target.id === "startFastLoopBtn") startFastLoop().catch(handleError);
  if (target.id === "refreshFastLoopBtn") refreshFastLoop().catch(handleError);
  if (target.id === "stopFastLoopBtn") stopFastLoop().catch(handleError);
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
    currentProfile.profile.registers.push({ channel: profilePrefix(currentProfile.profile) + ".W", address: 0, type: "int32", scale: 1, unit: "W", access: "read" });
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
  .then(() => showView("overview")) // first/default view; starts the 1 s snapshot poll
  .catch(handleError);
