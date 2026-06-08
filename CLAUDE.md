# PyEMS — project conventions

Local energy management system controlling energy sources over Modbus.
Control logic is the core; Modbus is an edge adapter. Architecture follows
IEC 61131-3 (RESOURCE / TASK / FUNCTION_BLOCK) and grid-code terminology
(EN 50549, ENTSO-E NC RfG), not vendor/hobby jargon.

## Naming: electrical quantities must be explicit

A name that says "power" is ambiguous — it hides which quantity is meant.
Every binding/parameter/variable that refers to an electrical quantity MUST
name that quantity. An electrician reading the signature should never guess.

| Quantity        | Symbol | Unit | Suffix    | Binding name example                       | Channel field |
|-----------------|--------|------|-----------|--------------------------------------------|---------------|
| Active power    | P      | W    | `_w`      | `unit_active_power_channel`                | `.W`          |
| Reactive power  | Q      | var  | `_var`    | `unit_reactive_power_channel`              | `.VAR`        |
| Apparent power  | S      | VA   | `_va`     | `unit_apparent_power_channel`              | `.VA`         |
| Power factor    | cos φ  | —    | —         | `unit_power_factor_channel`                | —             |
| Voltage         | U      | V    | `_v`      | `connection_point_voltage_channel`         | `.PhVph*`     |
| Frequency       | f      | Hz   | `_hz`     | `connection_point_frequency_channel`       | `.Hz`         |

Rules:
- Never use bare `power`, `power_channel`, `rated`, `wset`, or vendor terms
  (Huawei `WSet`, SunSpec names) in controller signatures or config keys.
- Use grid-code terms: **connection point** (POC/PCC), **active power setpoint**,
  **P_max** (maximum active power / RfG Maximum Capacity), **active power
  gradient** (ramp rate), **export limit at the connection point**.
- Controllers are **unit-agnostic**: say `unit_*`, not `pv_*`/`inverter_*`. The
  device type (PV, genset, storage) is fixed only by the tag binding in site.yaml.
- Numeric value parameters keep the unit suffix (`export_limit_w`, `p_max_w`,
  `safe_active_power_w`) so units are unambiguous at the call site.

## Channel tags

- Tag = `<device id>.<field>` (e.g. `pv1.W`, `grid.W`). The `<field>` part
  comes from the profile; the `<device id>` is the per-device namespace from
  `site.yaml` (see `namespaced()` in `src/drivers/modbus_device.py`). Two
  identical devices get distinct tags (`pv1.W`, `pv2.W`) — no collisions.
- System status tags use the `sys.` namespace (`sys.safe_mode`,
  `sys.comms_age_s`) — these are status words, not device registers.

## Tag binding (IEC VAR_INPUT/VAR_OUTPUT)

Controllers never hardcode tag name strings. Each controller takes the channel
names it reads/drives via its constructor, wired from `site.yaml`. This keeps a
controller class reusable across any device. Example: `GridExportLimitController`
binds `connection_point_active_power_channel`, `unit_active_power_channel`,
`unit_active_power_setpoint_channel`.

## Device profiles are data

Modbus register maps live in `profiles/*.yaml`, never hardcoded in Python.
Adding a device model = add a YAML, zero code changes. Site-specific values
(addresses, setpoints, safety thresholds) live in `config/site.yaml`.

## Environment

Run with `.venv/Scripts/python.exe` (global `py`/`python` lacks deps).
Production target: Raspberry Pi + PREEMPT_RT; keep the fast control loop free of
blocking bus I/O (see `CachedDriver`).
