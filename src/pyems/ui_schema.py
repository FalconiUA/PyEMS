"""Single source of truth for the configuration UI.

The web UI grew by bolting a new tab (or sub-tab) on for every controller, and
each scalar setting had to be hand-wired in four places: the page HTML, the
``render``/``gather`` pair in ``app.js``, and the validation in ``ui.py``. This
registry centralises that. It declares, once:

* ``NAV`` — the navigation tree (category groups -> pages) the sidebar mirrors;
* ``FIELDS`` — every scalar setting's path, type, range, unit and help text;

and derives both the served schema (``schema_payload``) the front end renders
its forms from and the scalar validation (``validate_site``) the back end runs.

Bespoke widgets (device tables, the profile editor, hard-switch writes, the
allocation envelope, live views) stay custom — only the many plain scalar
fields are registry-driven. Adding such a setting is now one ``FIELDS`` entry.
"""
from __future__ import annotations

import math
from typing import Any

# ── Navigation tree ──────────────────────────────────────────────────────────
# Category groups -> pages. ``view`` is the section id (and data-view); ``page``
# is the static HTML file loaded into that section. The sidebar in index.html
# mirrors this exactly (enforced by tests); serving it lets the front end and
# future tooling build navigation from one declaration.
NAV: list[dict[str, Any]] = [
    {
        "group": "Monitor",
        "pages": [
            {"view": "overview", "label": "Overview", "page": "overview.html"},
            {"view": "realtime", "label": "Realtime", "page": "realtime.html"},
            {"view": "logs", "label": "Logs", "page": "logs.html"},
        ],
    },
    {
        "group": "Control",
        "pages": [
            {"view": "scenario", "label": "Scenario", "page": "scenario.html"},
        ],
    },
    {
        "group": "Setup",
        "pages": [
            {"view": "devices", "label": "Devices", "page": "devices.html"},
            {"view": "profiles", "label": "Device profiles", "page": "profiles.html"},
            {"view": "timing", "label": "Control timing", "page": "control-timing.html"},
            {"view": "safety", "label": "Safety", "page": "safety.html"},
            {"view": "hardware", "label": "Hardware switch", "page": "hardware-switch.html"},
            {"view": "network", "label": "Network", "page": "network.html"},
            {"view": "time", "label": "Time", "page": "time.html"},
            {"view": "telemetry", "label": "Telemetry & recording", "page": "telemetry.html"},
        ],
    },
    {
        "group": "Tools",
        "pages": [
            {"view": "diagnostics", "label": "Diagnostics", "page": "diagnostics.html"},
            {"view": "simulation", "label": "Simulation", "page": "simulation.html"},
        ],
    },
]

# ── Field registry ───────────────────────────────────────────────────────────
# Each field declares: path (dotted site-dict path, integer segments index into
# lists), type, label and help (the tooltip), plus optional unit/min/max/step,
# select options, required flag, and a `group` — the `data-schema-group`
# container the front end renders the field into. `validate=False` means the
# field is metadata-only here and validated by ui.py (the conditionally-gated
# allocation envelope and headroom limiter), but still served for rendering.
#
# `access` ("operator"/"installer") is recorded for a later access-level gate;
# nothing consumes it yet.


def _field(
    path: str,
    type: str,
    label: str,
    help: str = "",
    *,
    unit: str = "",
    required: bool = False,
    min: float | None = None,
    max: float | None = None,
    step: float | None = None,
    options: list[dict[str, str]] | None = None,
    placeholder: str = "",
    rows: int | None = None,
    group: str | None = None,
    id: str | None = None,
    access: str = "operator",
    validate: bool = True,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "path": path,
        "type": type,
        "label": label,
        "help": help,
        "required": required,
        "access": access,
        "validate": validate,
    }
    for key, value in (
        ("id", id),
        ("unit", unit),
        ("placeholder", placeholder),
        ("group", group),
    ):
        if value:
            spec[key] = value
    for key, value in (("min", min), ("max", max), ("step", step), ("rows", rows)):
        if value is not None:
            spec[key] = value
    if options is not None:
        spec["options"] = options
    return spec


_MODE_OPTIONS = [
    {"value": "export_limit", "label": "Export limit"},
    {"value": "import_limit", "label": "Import limit"},
]
_TUNING_OPTIONS = [
    {"value": "auto", "label": "Auto"},
    {"value": "manual", "label": "Manual"},
]

FIELDS: list[dict[str, Any]] = [
    # Scenario — basics (rendered bespoke: the device pickers are dynamic).
    _field("scenario.control_mode", "select", "Mode", required=True, options=_MODE_OPTIONS),
    _field("scenario.active_power_limit_w", "number", "Limit at connection point", unit="W",
           required=True, step=1, min=0),
    _field("scenario.connection_point_device_id", "text", "Connection point meter", required=True),
    _field("scenario.unit_device_id", "text", "Controlled generating unit", required=True),
    # Scenario — advanced (registry-rendered into the Advanced sub-tab).
    # These carry defaults from default_site.yaml, so they are validated for type
    # when present but not required (matching the original ui.py, which left them
    # unchecked).
    _field("scenario.export_priority", "number", "Export-limit priority",
           step=1, min=1, group="allocation_priorities",
           help="Priority of the export cap (lower wins). Default 5 — above regulation, below safety."),
    _field("scenario.regulation_priority", "number", "Regulation priority",
           step=1, min=1, group="allocation_priorities",
           help="Priority of target-power regulation (lower wins). Default 10 — below the export cap."),
    _field("scenario.pid_tuning", "select", "Tuning mode", options=_TUNING_OPTIONS,
           help="Auto fills conservative PID gains and locks them. Manual lets you edit the gains below."),
    # Control timing (registry-rendered).
    _field("control.fast_cycle_s", "number", "Fast control cycle", unit="s", required=True,
           step=0.1, min=0.1, group="control_timing",
           help="How often the control logic runs. Shorter reacts faster but uses more CPU. Typically 0.2–1 s."),
    _field("control.poll_interval_s", "number", "Modbus poll interval", unit="s", required=True,
           step=0.1, min=0.1, group="control_timing",
           help="How often the background reader polls devices over Modbus. Kept separate from the control cycle so bus I/O never blocks control."),
    _field("control.setpoint_rewrite_s", "number", "Setpoint keepalive rewrite", unit="s",
           step=0.1, min=0, group="control_timing",
           help="Even an unchanged setpoint is re-sent at this cadence to keep the device's command watchdog fed. Empty = rewrite every cycle."),
    _field("control.command_json", "text", "Operator command file", placeholder="logs/commands.json",
           group="control_timing",
           help="JSON file the UI writes RUN/STOP and hard-switch commands to. Enables the soft generation gate. Empty disables operator commands."),
    _field("control.command_max_age_s", "number", "Command freshness window", unit="s",
           step=1, min=0, group="control_timing",
           help="A RUN command must be newer than this when the EMS first reads it; then it stays latched for the run. Guards against acting on a stale leftover command."),
    _field("control.generation_gate_priority", "number", "Generation-gate priority",
           step=1, min=1, group="control_timing",
           help="Allocator priority used to pin output to the floor while generation is OFF. Must sit below safety (0) and above normal control — usually 1."),
    # Safety interlocks (registry-rendered).
    _field("safety.safe_active_power_w", "number", "Safe active power", unit="W", step=1,
           group="safety_interlocks",
           help="Setpoint forced on a safety trip, at priority 0 (overrides everything). 0 W curtails to zero if the unit envelope allows."),
    _field("safety.max_comms_age_s", "number", "Bus communication timeout", unit="s", required=True,
           step=0.1, min=0, group="safety_interlocks",
           help="Trip the safe state when the whole bus has had no fresh read for this long."),
    _field("safety.max_write_age_s", "number", "Write-path timeout", unit="s", step=0.1, min=0,
           group="safety_interlocks",
           help="Trip when reads still work but setpoint writes stop reaching the bus. Empty disables this guard."),
    _field("safety.device_comms_watchdog_s", "number", "Device-side watchdog", unit="s", step=0.1,
           min=0, group="safety_interlocks",
           help="The command-watchdog period configured on the device itself. Used to check the keepalive rewrite is frequent enough."),
    _field("safety.max_measurement_frozen_s", "number", "Frozen-measurement timeout", unit="s",
           step=0.1, min=0, group="safety_interlocks",
           help="Trip when a watched measurement stops changing for this long (a stuck sensor). Empty disables freeze detection."),
    _field("safety.frozen_measurement_channels", "list", "Watched measurement tags", rows=3,
           placeholder="grid.W", group="safety_interlocks",
           help="Tags checked for freeze, one per line or comma-separated, e.g. grid.W."),
    # Actuator compliance (registry-rendered; an enable toggle stays hand-written).
    _field("setpoint_compliance.tolerance_w", "number", "Tolerance", unit="W", step=1, min=0,
           group="actuator_compliance",
           help="How far actual output may deviate from the setpoint before it counts as a violation."),
    _field("setpoint_compliance.max_violation_s", "number", "Violation time", unit="s", step=0.1,
           min=0, group="actuator_compliance",
           help="The deviation must persist this long before sys.setpoint_violation is raised."),
    # Telemetry & recording (registry-rendered).
    _field("telemetry.live_json", "text", "Live snapshot file", placeholder="logs/live_state.json",
           group="telemetry",
           help="JSON file the running EMS rewrites each cycle; Overview and Realtime read it (no extra bus polling)."),
    _field("recording.cycle_csv", "text", "Cycle log (CSV)", placeholder="logs/sim_cycles.csv",
           group="telemetry", help="Per-cycle CSV recording path. Empty disables cycle recording."),
    _field("recording.channels", "list", "Recorded tags", rows=3, group="telemetry",
           placeholder="Leave empty to record controller-bound tags",
           help="Tags to record, one per line or comma-separated. Empty records the default controller-bound tag set."),
    # Simulation model (registry-rendered; an enable toggle stays hand-written).
    _field("simulation.tick_s", "number", "Simulation tick", unit="s", step=0.1, min=0,
           group="simulation_model", help="Time step of the synthetic plant model."),
    _field("simulation.meter_noise_w", "number", "Meter noise", unit="W", step=1, min=0,
           group="simulation_model",
           help="Random noise added to simulated meter readings, to mimic a real sensor."),
    _field("simulation.unit.tau_s", "number", "Unit response time τ", unit="s", step=0.1, min=0,
           group="simulation_model",
           help="First-order lag: how slowly simulated unit output follows its setpoint."),
    _field("simulation.unit.peak_w", "number", "Unit peak available power", unit="W", step=1, min=0,
           group="simulation_model", help="Peak of the synthetic available-power curve (e.g. midday PV)."),
    _field("simulation.unit.period_s", "number", "Unit curve period", unit="s", step=1, min=1,
           group="simulation_model", help="Period of the synthetic availability curve."),
    _field("simulation.unit.noise_w", "number", "Unit noise", unit="W", step=1, min=0,
           group="simulation_model", help="Random noise on simulated unit output."),
    _field("simulation.load.base_w", "number", "Load base", unit="W", step=1, min=0,
           group="simulation_model", help="Average site consumption in the synthetic load profile."),
    _field("simulation.load.amplitude_w", "number", "Load amplitude", unit="W", step=1, min=0,
           group="simulation_model", help="Swing of the synthetic load above and below the base."),
    _field("simulation.load.period_s", "number", "Load curve period", unit="s", step=1, min=1,
           group="simulation_model", help="Period of the synthetic load profile."),
    _field("simulation.load.noise_w", "number", "Load noise", unit="W", step=1, min=0,
           group="simulation_model", help="Random noise on simulated load."),
    # Allocation envelope + headroom limiter: registry-rendered, but ui.py
    # validates them (the envelope requires exactly one channel; the limiter is
    # gated by its enabled flag), so they carry validate=False. The envelope
    # fields use a flattened DOM id (`allocation.p_min_w`) distinct from their
    # site path (`allocation.channels.0.p_min_w`) — the bespoke render/gather
    # already address them by that id.
    _field("allocation.channels.0.p_min_w", "number", "Minimum active power", unit="W",
           id="allocation.p_min_w", group="allocation_envelope", required=True, step=1, validate=False,
           help="Envelope floor: the lowest active power the unit may be commanded to. Usually 0 W."),
    _field("allocation.channels.0.p_max_w", "number", "P_max (rated capacity)", unit="W",
           id="allocation.p_max_w", group="allocation_envelope", required=True, step=1, validate=False,
           help="Envelope ceiling: the unit's maximum active power (RfG Maximum Capacity). No setpoint may exceed it."),
    _field("allocation.channels.0.default_w", "number", "Default active power", unit="W",
           id="allocation.default_w", group="allocation_envelope", required=True, step=1, validate=False,
           help="Setpoint applied when no controller is requesting one (e.g. before the first regulation cycle)."),
    _field("allocation.channels.0.deadband_w", "number", "Deadband", unit="W",
           id="allocation.deadband_w", group="allocation_envelope", required=True, step=1, min=0,
           validate=False,
           help="Setpoint changes smaller than this are ignored, so the inverter is not chattered by tiny corrections."),
    _field("allocation.channels.0.ramp_rate_w_per_s", "number", "Active power gradient (up)", unit="W/s",
           id="allocation.ramp_rate_w_per_s", group="allocation_envelope", required=True, step=1, min=0,
           validate=False,
           help="Grid-code ramp rate for raising production. Limits how fast the setpoint may increase."),
    _field("allocation.channels.0.ramp_down_w_per_s", "number", "Curtailment gradient (down)", unit="W/s",
           id="allocation.ramp_down_w_per_s", group="allocation_envelope", step=1, min=0, validate=False,
           help="Faster ramp for reducing power (export-limit compliance). Leave empty to use the same gradient as up."),
    _field("setpoint_headroom.headroom_w", "number", "Setpoint headroom floor", unit="W",
           group="spike_protection", required=True, step=100, min=1, validate=False,
           help="The setpoint may sit at most this much above actual production — the fixed part of the cap. Prevents export spikes when the resource returns."),
    _field("setpoint_headroom.headroom_pct", "number", "Setpoint headroom, % of output",
           group="spike_protection", step=1, min=0, validate=False,
           help="Dynamic part: a percentage of current production, used when larger than the floor. 0 = floor only."),
    _field("setpoint_headroom.priority", "number", "Headroom priority",
           group="spike_protection", step=1, min=1, validate=False,
           help="Allocator priority of this limiter (lower wins). In import-limit mode keep it just below regulation — usually 11 when regulation is 10."),
    # PID regulator gains. DOM id (`pid.kp`) differs from the site path
    # (`connection_point_active_power.gains.kp`); the bespoke render/gather and
    # the manual/auto read-only toggle address them by id, so validate=False.
    _field("connection_point_active_power.gains.kp", "number", "Proportional gain kp",
           id="pid.kp", group="pid_gains", required=True, step=0.01, validate=False,
           help="Immediate correction proportional to the connection-point active-power error."),
    _field("connection_point_active_power.gains.ki", "number", "Integral gain ki",
           id="pid.ki", group="pid_gains", required=True, step=0.01, validate=False,
           help="Slow correction that removes steady-state error."),
    _field("connection_point_active_power.gains.kd", "number", "Derivative gain kd",
           id="pid.kd", group="pid_gains", required=True, step=0.01, validate=False,
           help="Damping term. Usually 0 for noisy power measurements."),
    _field("connection_point_active_power.gains.tt", "number", "Anti-windup tracking time tt", unit="s",
           id="pid.tt", group="pid_gains", required=True, step=0.1, min=0, validate=False,
           help="How fast the integrator backs off when the allocator clamps the output (ramp or deadband), to prevent wind-up."),
]

# Top-level sections that may be absent entirely; their fields are validated
# only when the section is present (matching the original ui.py behaviour).
OPTIONAL_SECTIONS = frozenset(
    {"telemetry", "recording", "simulation", "setpoint_compliance", "setpoint_headroom"}
)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _segments(path: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in path.split(".")]


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path; integer segments index into lists. Missing -> default."""
    node = data
    for seg in _segments(path):
        if isinstance(seg, int):
            if not isinstance(node, list) or seg >= len(node):
                return default
            node = node[seg]
        else:
            if not isinstance(node, dict) or seg not in node:
                return default
            node = node[seg]
    return node


def has_path(data: Any, path: str) -> bool:
    sentinel = object()
    return get_path(data, path, sentinel) is not sentinel


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    """Write a dotted path, creating intermediate dicts/lists as needed."""
    segs = _segments(path)
    node: Any = data
    for seg, nxt in zip(segs, segs[1:]):
        if isinstance(seg, int):
            while len(node) <= seg:
                node.append({} if not isinstance(nxt, int) else [])
            if node[seg] is None:
                node[seg] = [] if isinstance(nxt, int) else {}
            node = node[seg]
        else:
            child = node.get(seg)
            if not isinstance(child, (dict, list)):
                child = [] if isinstance(nxt, int) else {}
                node[seg] = child
            node = child
    node[segs[-1]] = value


def iter_fields() -> list[dict[str, Any]]:
    return list(FIELDS)


def schema_payload() -> dict[str, Any]:
    """The schema as served at /api/settings-schema (front end + tooling)."""
    return {"nav": NAV, "fields": FIELDS}


# ── Validation ───────────────────────────────────────────────────────────────
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(
        float(value)
    )


def validate_site(site: dict[str, Any]) -> None:
    """Type/finite/required-checks for the registry-owned scalar fields.

    Raises ``ValueError`` (matching ui.py's message style) on the first problem.
    Conditionally-gated sections (allocation envelope, headroom) carry
    ``validate=False`` and are checked by ui.py instead.
    """
    for field in FIELDS:
        if not field.get("validate", True):
            continue
        path = field["path"]
        section = path.split(".", 1)[0]
        if section in OPTIONAL_SECTIONS and not isinstance(site.get(section), dict):
            continue  # optional section absent -> nothing to check
        present = has_path(site, path)
        if not present:
            if field["required"]:
                raise ValueError(f"{path} is required")
            continue
        value = get_path(site, path)
        ftype = field["type"]
        if ftype == "number":
            if not _is_number(value):
                raise ValueError(f"{path} must be a finite number")
        elif ftype in ("text", "select"):
            if field["required"]:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{path} is required")
            elif not isinstance(value, str):
                raise ValueError(f"{path} must be text")
        elif ftype == "list":
            if not isinstance(value, list):
                raise ValueError(f"{path} must be a list")
