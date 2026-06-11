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
SAFE_MODE_CHANNEL = "sys.safe_mode"              # 1.0 = safety trip active, 0.0 = healthy
SETPOINT_VIOLATION_CHANNEL = "sys.setpoint_violation"  # 1.0 = unit not following its setpoint

# ── requester names (RequestBoard claim keys, unique per controller) ─────────
SAFETY_REQUESTER = "safety"                                  # priority 0 (reserved)
EXPORT_LIMIT_REQUESTER = "export_limit"                      # export cap constraint
CONNECTION_POINT_POWER_REQUESTER = "connection_point_active_power"  # PID regulation target
IMPORT_LIMIT_REQUESTER = "connection_point_import_limit"     # import-limit regulation
SETPOINT_HEADROOM_REQUESTER = "setpoint_headroom"            # available-power tracking cap
