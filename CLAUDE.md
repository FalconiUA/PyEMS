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
  comes from the canonical vocabulary in `src/pyems/device_fields.py`
  (SunSpec-style point names, identical across all device models); the
  `<device id>` is the per-device namespace from `site.yaml` (see
  `namespaced()` in `src/drivers/modbus_device.py`). Two identical devices
  get distinct tags (`pv1.W`, `pv2.W`) — no collisions.
- Profiles map vendor registers ONTO the vocabulary: the vendor doc
  contributes only address/type/scale; vendor register-map display names are
  never channel names. `DeviceProfile.load()` rejects an unknown field or a
  non-canonical unit (values are SI: W, not kW — convert with `scale`).
- System status tags use the `sys.` namespace (`sys.safe_mode`,
  `sys.comms_age_s`) — these are status words, not device registers.

## Tag binding (IEC VAR_INPUT/VAR_OUTPUT)

Controllers never hardcode tag name strings. Each controller takes the channel
names it reads/drives via its constructor, wired from `site.yaml`. This keeps a
controller class reusable across any device. Example: `GridExportLimitController`
binds `connection_point_active_power_channel`, `unit_active_power_channel`,
`unit_active_power_setpoint_channel`.

## Setpoint arbitration (PowerAllocator)

Controllers do **not** write unit setpoint channels (e.g. `pv.WSet`) directly —
several scenarios may legitimately command the same setpoint in one scan cycle,
so "last writer wins" is unacceptable. Instead each controller **posts a
request** onto a `RequestBoard`: a standing claim on one setpoint channel keyed
by the requester's name, carrying a hard range (`min_w`/`max_w`), an optional
preferred value (`target_w`), a `priority` (0 = highest, reserved for safety),
and an optional `ttl_s`. A claim persists across cycles until replaced,
withdrawn, or expired, so a slow task can keep a claim alive between its runs.
All requests use the **generating-unit convention**: positive active power =
injection into the AC bus (for storage, `P > 0` discharge, `P < 0` charge).

Once per cycle, after all tasks and before the driver flush, the `PowerAllocator`
resolves each channel and is its **sole writer**. Resolution (see
`src/pyems/allocation/`): sort requests by `(priority, requester)`; intersect
ranges starting from the device envelope `[p_min_w, p_max_w]` — a higher-priority
constraint that conflicts discards the lower one entirely (never split the
difference); take the target from the first honored request that has one, else
hold the last setpoint (or `default_w` when there are no requests at all); then
apply per-unit deadband and ramp gradient. A priority-0 (safety) forced value
bypasses deadband and ramp so it lands exactly, in one cycle. Per-channel
envelope, ramp, deadband and default live in the `allocation:` section of
`site.yaml`, not in any controller. Controllers still read measurements and write
status tags (e.g. `sys.safe_mode`) via `state`; only writable unit setpoints go
through the board.

## Device profiles are data

Modbus register maps live in `profiles/*.yaml`, never hardcoded in Python.
Adding a device model = add a YAML, zero code changes. Site-specific values
(addresses, setpoints, safety thresholds) live in `config/site.yaml`.

## Project layout & packaging

src-layout, installable package `pyems`:

    src/pyems/        # the package — import as `from pyems.<module> import ...`
    tests/            # pytest suite (in-memory fakes, no hardware)
    profiles/         # device register maps (YAML data)
    config/           # site.yaml (per-installation values)

Imports use the real package name (`from pyems.drivers... import`), never
`from src...`. The package is resolved via an editable install, not sys.path
hacks. Data dirs (`profiles/`, `config/`) live at the repo root, outside the
package; `ems.py` anchors to them via `Path(__file__).parents[2]`.

## Environment

Run with `.venv/Scripts/python.exe` (global `py`/`python` lacks deps).
Setup:  `.venv/Scripts/python.exe -m pip install -e .[dev]`
Tests:  `.venv/Scripts/python.exe -m pytest`  (config in pyproject.toml)
Run:    `pyems` console script, or `python -m pyems.ems`.

Production target: Raspberry Pi + PREEMPT_RT; keep the fast control loop free of
blocking bus I/O (see `CachedDriver`).
