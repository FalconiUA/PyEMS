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
| `sys.<device id>.comms_age_s` | `comms_age_channel(device_id)` | `CachedDriver` (per-device mode) | `SafetyController` (if `safety.device_comms_max_age_s` set), UI, recorder | Seconds since that device's last successful read; `inf` until the first one. Lets one failed device age without freezing healthy devices. |
| `sys.write_age_s` | `WRITE_AGE_CHANNEL` | `CachedDriver` (every cycle) | `SafetyController` (if `safety.max_write_age_s` set), UI, recorder | Seconds since the last successful setpoint flush; `inf` until the first one. Grows while writes fail even though reads keep `comms_age_s` fresh — so a write-blind EMS (remote control lost, half-open socket) is detectable, not just a dead bus. |
| `sys.safe_mode` | `SAFE_MODE_CHANNEL` | `SafetyController` | UI, SCADA/history, recorder | 1.0 = safety trip active (stale/frozen measurements, stale write path), 0.0 = healthy. Status word only — the actual interlock is the priority-0 board claim. |
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
| `connection_point_import_limit` | `IMPORT_LIMIT_REQUESTER` | `connection_point_active_power.priority` (10) | `ConnectionPointPowerController` import-limit mode |

## Binding keys in site.yaml (IEC VAR_INPUT/VAR_OUTPUT names)

Controllers never hardcode tag strings; these config keys wire them:

| Key | Direction | Used by sections |
|---|---|---|
| `connection_point_active_power_channel` | measurement (read) | `export_limit`, `connection_point_active_power` |
| `unit_active_power_channel` | measurement (read) | `export_limit`, `connection_point_active_power`, `setpoint_compliance`, `setpoint_headroom` |
| `unit_active_power_setpoint_channel` | setpoint (write, via allocator) | `export_limit`, `connection_point_active_power`, `setpoint_compliance`, `setpoint_headroom` |
| `unit_active_power_setpoint_channels` | setpoint list (safety claims) | `safety` |
| `frozen_measurement_channels` | measurement list (freeze guard) | `safety` |
| `device_comms_max_age_s` | device-id map to per-device age limit | `safety` |

## Adding a new controller / system tag — checklist

1. Define the name ONCE in `src/pyems/system_tags.py`:
   `*_CHANNEL` constants must be `sys.*` tags, `*_REQUESTER` constants are
   board claim keys. Naming convention and uniqueness are enforced
   automatically by `tests/test_system_tags.py` (it introspects the module —
   no test edits needed).
2. In the new controller: `from pyems.system_tags import <NAME>` — one
   import line; never write the string literal.
3. Add a row to the tables in THIS file (writer/readers/meaning).
4. Teach `src/pyems/tag_registry.py::collect()` the new config section, so
   `pyems-tags` shows the tag's readers/writers on the live map.
5. If the tag is a new status word the EMS exposes, add its `Channel` where
   the others are created (`build_ems()` in ems.py and `_system_channels()`
   in ui.py).

## Device tags (for completeness)

`<device id>.<field>` — the `<device id>` comes from `site.yaml` (see
`namespaced()` in `src/pyems/drivers/modbus_device.py`); the `<field>` part
MUST come from the canonical vocabulary in `src/pyems/device_fields.py`
(SunSpec-style point names: `W`, `WphA`, `VAR`, `PhVphA`, `WSet`, ...).
Vendor register-map display names are never channel names: a profile maps
vendor registers onto the vocabulary (address/type/scale per device, meaning
and SI unit fixed by the vocabulary). `DeviceProfile.load()` rejects an
unknown field or a non-canonical unit at startup. A genuinely new quantity
is added ONCE in `device_fields.py`; `tests/test_device_fields.py`
introspects the vocabulary and globs `profiles/`, so no test edits needed.
