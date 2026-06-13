"""THE single place for every EMS-internal tag and requester name.

Two namespaces meet in the SystemState tag pool:

  - device tags  `<device id>.<field>` — defined by profiles/*.yaml + the
    per-device id from site.yaml (see `namespaced()` in drivers/modbus_device);
  - system tags  `sys.*` — status words produced by the EMS itself, defined
    HERE and nowhere else. Rename a system tag in this module and every
    producer/consumer follows (modules import these constants; none redefine
    them). The human-readable register of all of them, with writers/readers,
    lives in documents/internal-tags.md — keep both in sync.

Requester names are the arbitration identities on the RequestBoard: a claim
is keyed by (channel, requester), so these strings appear in logs
("target now from safety") and must stay unique across controllers.
"""

# ── system status tags (sys.* — status words, not device registers) ──────────
COMMS_AGE_CHANNEL = "sys.comms_age_s"            # seconds since last good bus read; inf until first
WRITE_AGE_CHANNEL = "sys.write_age_s"            # seconds since last good setpoint flush; inf until first
SAFE_MODE_CHANNEL = "sys.safe_mode"              # 1.0 = safety trip active, 0.0 = healthy
SETPOINT_VIOLATION_CHANNEL = "sys.setpoint_violation"  # 1.0 = unit not following its setpoint

# ── generation gate (operational interlock, NOT a safety-rated E-stop) ────────
# Two-level operation: the EMS process being alive (safety + polling) is
# separate from generation being permitted. The gate lets an operator pin the
# unit to a safe floor until they explicitly enable production from the UI.
GENERATION_ALLOWED_CHANNEL = "sys.generation_allowed"      # 1.0 = production permitted, 0.0 = pinned to floor
GENERATION_GATE_ACTIVE_CHANNEL = "sys.generation_gate_active"  # 1.0 = gate is actively pinning the unit
COMMAND_AGE_CHANNEL = "sys.command_age_s"        # seconds since the UI command file was issued; inf if none

# ── hard inverter switch (latched remote start/stop — an OPERATOR ACTION) ─────
# Separate level from the soft gate: this drives the device's own start/stop
# command register(s), de-energizing the inverter, not just curtailing to 0 W.
INVERTER_COMMAND_CHANNEL = "sys.inverter_command"      # latest requested action: 1=start, 0=stop, NaN=none
INVERTER_COMMAND_ID_CHANNEL = "sys.inverter_command_id"  # wall-clock id of that action; NaN if none/leftover
INVERTER_RUN_STATE_CHANNEL = "sys.inverter_run_state"  # what the EMS last commanded: 1=started, 0=stopped, NaN=never

# Per-device read-freshness tag (parametric, so a helper not a constant):
# `sys.<device id>.comms_age_s`, seconds since THAT device's last good read.
# Prefixed with `sys.` so it never collides with the device's own tags
# (`grid.W` vs `sys.grid.comms_age_s`). Produced by CachedDriver in per-device
# mode; read by SafetyController when `safety.device_comms_max_age_s` is set.
def comms_age_channel(device_id: str) -> str:
    return f"sys.{device_id}.comms_age_s"


# ── requester names (RequestBoard claim keys, unique per controller) ─────────
SAFETY_REQUESTER = "safety"                                  # priority 0 (reserved)
EXPORT_LIMIT_REQUESTER = "export_limit"                      # export cap constraint
CONNECTION_POINT_POWER_REQUESTER = "connection_point_active_power"  # PID regulation target
IMPORT_LIMIT_REQUESTER = "connection_point_import_limit"     # ConnectionPointPowerController import mode
SETPOINT_HEADROOM_REQUESTER = "setpoint_headroom"            # available-power tracking cap
GENERATION_GATE_REQUESTER = "generation_gate"               # priority-1 pin-to-floor when generation disabled
