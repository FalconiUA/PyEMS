# Internal EMS tags & setpoint register

Single source of truth for every name the EMS itself produces or claims.
**To rename anything below, edit `src/pyems/system_tags.py`** — that module
is the only place these names are defined; every other file imports the
constants from it directly (a test pins this). Device Modbus register names
are a separate world on purpose: they live in `profiles/*.yaml` as data.

For a LIVE cross-reference of one concrete site (every tag → origin →
readers → writers), run:

    pyems-tags --site config/site.sim.yaml
    pyems-tags --site config/site.sim.yaml --markdown documents/tag-map.sim.md

## System status tags (`sys.*` — status words, not device registers)

| Tag | Constant | Written by | Read by | Meaning |
|---|---|---|---|---|
| `sys.comms_age_s` | `COMMS_AGE_CHANNEL` | `CachedDriver` (every cycle) | `SafetyController`, UI, recorder | Seconds since the last successful bus read; `inf` until the first one. |
| `sys.safe_mode` | `SAFE_MODE_CHANNEL` | `SafetyController` | UI, SCADA/history, recorder | 1.0 = safety trip active (stale/frozen measurements), 0.0 = healthy. Status word only — the actual interlock is the priority-0 board claim. |
| `sys.setpoint_violation` | `SETPOINT_VIOLATION_CHANNEL` | `SetpointComplianceMonitor` | UI, SCADA/history, recorder | 1.0 = unit's measured power overshoots the applied setpoint for too long (remote control likely disabled on the device). Alarm only. |

## Unit setpoint channels (written ONLY by PowerAllocator)

Device setpoints (e.g. `pv.WSet`) come from profiles + site.yaml, not from
code — but the *write path* is fixed: controllers post requests on the
RequestBoard, and the `PowerAllocator` is the sole writer of every channel
listed under `allocation.channels` in site.yaml.

## Requester names (RequestBoard claim keys)

A claim is keyed `(channel, requester)`; these names appear in logs
("target now from safety") and must stay unique.

| Requester | Constant | Priority (site.yaml key) | Posts |
|---|---|---|---|
| `safety` | `SAFETY_REQUESTER` | 0 — reserved, hard-coded | forced safe value (min=max=target), bypasses deadband+ramp |
| `export_limit` | `EXPORT_LIMIT_REQUESTER` | `export_limit.priority` (5) | pure upper bound: `max_w = P_unit + P_cp + limit` |
| `setpoint_headroom` | `SETPOINT_HEADROOM_REQUESTER` | `setpoint_headroom.priority` (6) | pure upper bound: `max_w = P_unit + max(headroom_w, headroom_pct%)` |
| `connection_point_active_power` | `CONNECTION_POINT_POWER_REQUESTER` | `connection_point_active_power.priority` (10) | regulation target (feed-forward + PID trim) |
| `connection_point_import_limit` | `IMPORT_LIMIT_REQUESTER` | `connection_point_active_power.priority` (10) | import-limit regulation target (import-limit mode) |

## Binding keys in site.yaml (IEC VAR_INPUT/VAR_OUTPUT names)

Controllers never hardcode tag strings; these config keys wire them:

| Key | Direction | Used by sections |
|---|---|---|
| `connection_point_active_power_channel` | measurement (read) | `export_limit`, `connection_point_active_power` |
| `unit_active_power_channel` | measurement (read) | `export_limit`, `connection_point_active_power`, `setpoint_compliance`, `setpoint_headroom` |
| `unit_active_power_setpoint_channel` | setpoint (write, via allocator) | `export_limit`, `connection_point_active_power`, `setpoint_compliance`, `setpoint_headroom` |
| `unit_active_power_setpoint_channels` | setpoint list (safety claims) | `safety` |
| `frozen_measurement_channels` | measurement list (freeze guard) | `safety` |

## Device tags (for completeness)

`<device id>.<field>` — the `<field>` part comes from `profiles/*.yaml`, the
`<device id>` from `site.yaml` (see `namespaced()` in
`src/pyems/drivers/modbus_device.py`). Renaming those is a profile/site
edit, not a code edit.
