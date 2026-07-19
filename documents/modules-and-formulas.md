# PyEMS — module interaction & formulas (user guide)

This document explains, in plain language, how the main PyEMS modules work
together, what every parameter in `config/site.yaml` means, and collects all
formulas the system uses in a dedicated section. It is written for the
user/integrator configuring a site, not for a developer (code details live in
the docstrings of the respective modules). Every statement below is derived
from the actual source code; file references point at the implementation.

Related documents:

- [internal-tags.md](internal-tags.md) — registry of all internal `sys.*`
  tags (who writes and who reads them);
- [simulation.md](simulation.md) — running against a simulated plant;
- [control-design-references.md](control-design-references.md) — design
  references.

---

## 1. The big picture

PyEMS is a local energy management system controlling energy sources
(inverter, genset, storage) over Modbus. Control logic is the core; Modbus is
only an edge adapter. The architecture follows the industrial-PLC model of
IEC 61131-3: one **resource** (`Scheduler`), **tasks** (`Task`) with a period
and a priority, and **function blocks** (controllers).

The single most important idea: **controllers never write a device setpoint
directly**. Several controllers may legitimately want to command the same
setpoint channel (e.g. `pv.WSet`) in one cycle, so each of them only **posts
a request** onto a request board (`RequestBoard`), and a separate arbiter
(`PowerAllocator`) resolves all requests into one value per channel once per
cycle and is the **sole writer** of that channel.

### Data flow in one cycle

```
   Modbus devices (meter, inverter, ...)
        │  ▲
        │  │  background thread (poll_interval_s)
        ▼  │
   CachedDriver ──── measurement cache + setpoint queue ─┐
        │                                                │
        ▼ (1) read inputs                                │ (6) write outputs
   SystemState (tag pool: grid.W, pv.W, pv.WSet, sys.*)  │
        │                                                │
        ▼ (2) UI command file → sys.generation_allowed   │
        ▼ (3) tasks in priority order:                   │
      SafetyController (priority 0)  ───┐                │
      GridExportLimitController         │ requests       │
      ConnectionPointPowerController    ├──────────────▶ RequestBoard
      SetpointHeadroomLimiter           │                │
      GenerationGateController          │                │
      SetpointComplianceMonitor ────────┘                │
        │                                                │
        ▼ (4) arbitration                                │
      PowerAllocator ── one value per channel ── SystemState
        │
        ▼ (5) CSV recorder row, telemetry snapshot for the UI
```

The steps inside one cycle (`Scheduler.step()` in `src/pyems/scheduler.py`):

1. **Read inputs**: `CachedDriver.read_state()` copies cached measurements
   into `SystemState` (no bus access — microseconds).
2. **Operator commands**: `CommandFileReader` reads the JSON file the UI
   writes and sets `sys.generation_allowed`, `sys.inverter_command`, etc.
3. **Tasks**: controllers execute in priority order (0 = highest). They read
   measurements from `SystemState` and post requests onto the `RequestBoard`
   (and write `sys.*` status tags).
4. **Arbitration**: `PowerAllocator` resolves the requests for each
   configured setpoint channel into one number (formulas in §7.7) and writes
   it into `SystemState`.
5. **Write outputs**: `CachedDriver.write_setpoints()` copies setpoints into
   the cache; the background thread flushes them to the bus.
6. **Records**: the recorder (CSV) and telemetry (JSON for the UI) capture
   what was actually commanded this cycle.

The control cycle period is `control.fast_cycle_s`. The background Modbus
thread runs on its own period `control.poll_interval_s` — a slow or hung bus
**never** stalls the control cycle.

---

## 2. The main modules

### 2.1 Scheduler (`src/pyems/scheduler.py`)

The IEC "resource": owns the tasks and runs the control loop. Tasks execute
in priority order; a bug in a normal controller is logged and the cycle
continues (one broken optimizer cannot take the system down). The exception
is the priority-0 (safety) task: its failure deliberately aborts the process
— running without protection is worse than restarting (systemd restarts the
service).

### 2.2 CachedDriver (`src/pyems/drivers/cached.py`)

The non-blocking I/O layer. Real Modbus transactions run in a background
thread; the control cycle only touches an in-memory cache. In addition:

- **Data freshness**: tag `sys.comms_age_s` — seconds since the last
  successful bus read (plus `sys.<id>.comms_age_s` per device), and
  `sys.write_age_s` — seconds since the last successful setpoint flush. The
  safety controller reads these.
- **Keep-alive**: a changed setpoint is flushed on the next poll; an
  unchanged one is re-written every `control.setpoint_rewrite_s` seconds —
  to feed the device's own comms watchdog.
- **Command registers** (e.g. remote start/stop) are written exactly once
  per command via a one-shot queue and are never re-asserted by the
  keep-alive.

### 2.3 Device profiles and ModbusDeviceDriver (`profiles/*.yaml`, `src/pyems/drivers/modbus_device.py`)

A device's register map is **data** (YAML), not code. Adding a device model
= adding a YAML file. Each register is described by:

| Field | Meaning |
|---|---|
| `channel` | canonical field name (`pv.W`, `grid.W`, ...) from the vocabulary in `src/pyems/device_fields.py` — vendor register-map display names are never used |
| `address` | Modbus register address |
| `type` | `int16` / `uint16` / `int32` / `uint32` |
| `scale` | multiplier to the SI unit: `value = decode(raw) × scale` (see §7.10) |
| `unit` | SI unit (`W`, not kW — conversion is done by `scale`) |
| `access` | `read` (measurement) or `read_write` (setpoint) |
| `min_val`/`max_val` | plausibility bounds. For measurements, a value outside the bounds fails the poll (guards against a wrong scale/type/address); for writable registers they bound what the EMS itself writes |
| `command` | `true` = discrete command register (start/stop): written one-shot, never keep-alived |

A tag in the system is `<device id>.<field>`: the `id` from `site.yaml`
replaces the class segment of the profile name, so two identical inverters
get `pv1.W` and `pv2.W` with no collisions (`namespaced()`).

### 2.4 SystemState (`src/pyems/channels.py`)

The shared tag pool of one scan cycle — the resource's "global variables".
Controllers read inputs and write statuses only here, never to hardware
directly. `set()` clamps setpoint values into the channel's
`min_val`/`max_val` bounds (from the profile).

### 2.5 Controllers (`src/pyems/controllers/`)

Each controller is a "function block" bound to concrete tags via its
constructor (from `site.yaml`), so one class works with any device. The main
ones:

| Controller | Request priority | What it does |
|---|---|---|
| `SafetyController` | **0** (reserved) | Safety interlock: on failed preconditions (stale/frozen measurements, dead write path) pins the setpoint to a safe value |
| `GenerationGateController` | 1 (default) | Operational generation permit: until the operator enables generation from the UI, holds the unit at a safe floor |
| `GridExportLimitController` | 5 (default) | Export limitation at the connection point: posts a **pure upper-bound constraint** (`max_w`), no preferred value |
| `ConnectionPointPowerController` | 10 (default) | PID regulation of active power at the connection point: feed-forward + PID trim; posts a range and a target |
| `SetpointHeadroomLimiter` | 11 (default: regulation + 1) | Available-power tracking: keeps the setpoint from parking far above actual production |
| `SetpointComplianceMonitor` | — (posts no requests) | Actuator monitoring: if the unit sustainedly overshoots the applied setpoint — alarm `sys.setpoint_violation` (the device ignores commands) |
| `HardSwitchController` | — (writes commands via the one-shot queue) | Hard remote start/stop of the inverter on an operator action; fires once per new command |

**A lower priority number means a stronger claim.** Priority 0 is reserved
for safety only.

### 2.6 RequestBoard (`src/pyems/allocation/request.py`)

The request board. A request (`ActivePowerRequest`) is a *standing claim* by
one named requester on one setpoint channel:

- `min_w` / `max_w` — a hard range the requester insists on;
- `target_w` — an optional preferred value inside that range (without it the
  request is a "pure constraint": it only narrows the range);
- `priority` — 0 is highest, reserved for safety;
- `ttl_s` — validity window; without it the claim lives until replaced or
  withdrawn.

Reposting by the same requester **replaces** its previous claim. A claim
persists across cycles — a slow task can keep its claim alive between runs.

All values are active power in watts in the **generating-unit convention**:
positive = injection into the AC bus (for storage `P > 0` discharge,
`P < 0` charge).

### 2.7 PowerAllocator (`src/pyems/allocation/allocator.py`)

The arbiter. Once per cycle, after all tasks and before the bus flush, it
resolves the requests of every channel listed under `allocation:` into one
value (algorithm and formulas in §7.7) and is the **sole** writer of the
setpoint channel. It owns the channel's physical properties: the device
envelope (`p_min_w`/`p_max_w`), ramp gradient, deadband, and default value.

### 2.8 UI exchange: telemetry, commands, recorder, events

- `LiveSnapshotPublisher` (`telemetry:`) — atomically rewrites one JSON
  state file per cycle; the UI polls it off the filesystem (no second Modbus
  session).
- `CommandFileReader` (`control.command_json`) — the opposite direction: the
  UI writes one JSON command file, the EMS reads it at the top of each
  cycle. The channel is **fail-closed**: a missing/corrupt/stale file means
  generation disabled; an "enable" issued at or before the current EMS run
  started is also ignored (guards against a leftover file after a restart).
- `CycleRecorder` (`recording:`) — a CSV flight recorder: one row per cycle
  with the actually-commanded values.
- `EventJournal` (`events:`) — an alarm/event journal (JSONL): safety
  trip/release, requests rejected by the arbiter, etc.

---

## 3. Who outranks whom: the priority ladder

When several requests conflict, the arbiter processes them in ascending
priority-number order, and **a higher claim discards an incompatible lower
one entirely** (never "split the difference"). The typical ladder:

| Priority | Who | Meaning |
|---|---|---|
| 0 | safety | forced value; bypasses deadband and ramp — lands exactly, in one cycle |
| 1 | generation gate | generation not yet enabled by the operator |
| 5 | export limit | grid-code constraint |
| 10 | connection-point PID regulator | the "economic" control layer |
| 11 | headroom limiter | tracks actual production |

If there are no requests at all, the channel gets `default_w` (for PV,
`default_w = p_max_w` reproduces "run free until told otherwise").

---

## 4. `site.yaml` parameters, section by section

> Markers: **req.** = required; otherwise the default is given.
> All powers are watts (W), all times seconds (s).

### 4.1 `scenario:` — the high-level scenario (filled in by the UI)

This is the web UI's "form". From it the UI automatically **derives** the
`export_limit` and `connection_point_active_power` sections and the
`safety`/`allocation` bindings (`apply_scenario` in `src/pyems/ui.py`). If
you hand-edit those derived sections, saving the scenario from the UI will
overwrite them.

| Parameter | Default | Explanation |
|---|---|---|
| `control_mode` | `export_limit` | Mode: `export_limit` (cap injection into the grid) or `import_limit` (keep grid import below a limit by backing it with own generation) |
| `active_power_limit_w` | — | The active-power limit at the connection point for the selected mode |
| `connection_point_device_id` | `grid` | id of the meter device at the connection point (hence the tag `grid.W`) |
| `unit_device_id` | `pv` | id of the controlled unit (hence the tags `pv.W`, `pv.WSet`) |
| `pid_tuning` | `auto` | `auto` — standard PID gains (`kp 0.4, ki 0.08, kd 0.0, tt 5.0`); `manual` — taken from `connection_point_active_power.gains` |
| `export_priority` | 5 | Priority of the export-limit request |
| `regulation_priority` | 10 | Priority of the PID regulator's request |

### 4.2 `control:` — cycle timing

| Parameter | Default | Explanation |
|---|---|---|
| `fast_cycle_s` | **req.** | Control cycle period (tasks and arbitration) |
| `poll_interval_s` | **req.** | Background Modbus poll period (may be faster than the cycle) |
| `setpoint_rewrite_s` | 10 | Keep-alive: how often an **unchanged** setpoint is re-written to feed the device's comms watchdog |
| `command_json` | off | Path to the operator command JSON file (UI → EMS). Setting this key **enables the generation gate** |
| `command_max_age_s` | 30 | Maximum age of an "enable generation" command for it to be accepted |
| `generation_gate_priority` | 1 | Priority of the generation-gate request |

### 4.3 `export_limit:` — export limitation (`export_limit` mode)

| Parameter | Default | Explanation |
|---|---|---|
| `limit_w` | **req.** | Allowed export **magnitude** into the grid (≥ 0) |
| `priority` | **req.** | Request priority (typically 5) |
| `connection_point_active_power_channel` | **req.** | Active-power tag at the connection point (e.g. `grid.W`; sign: + import, − export) |
| `unit_active_power_channel` | **req.** | Tag of the unit's measured active power (`pv.W`) |
| `unit_active_power_setpoint_channel` | **req.** | The setpoint channel the request is posted on (`pv.WSet`) |

### 4.4 `connection_point_active_power:` — connection-point PID regulator

| Parameter | Default | Explanation |
|---|---|---|
| `export_limit_w` | **req.** | Export limit (magnitude) — the regulation target in `export_limit` mode |
| `import_limit_w` | **req.** | Import limit (magnitude). In `export_limit` mode a very large value means "import unconstrained"; in `import_limit` mode it is the limit itself |
| `priority` | **req.** | Request priority (typically 10 — below the export limit) |
| `gains.kp`, `.ki`, `.kd` | 0.4 / 0.08 / 0.0 | PID gains (proportional, integral, derivative) |
| `gains.n_filter` | 10 | Derivative filter coefficient |
| `gains.tt` | auto | Anti-windup tracking time constant Tt; if absent, derived from kp/ki/kd (§7.5) |
| the three `..._channel` keys | **req.** | Same bindings as in 4.3 |

### 4.5 `safety:` — the safety interlock (priority 0)

| Parameter | Default | Explanation |
|---|---|---|
| `max_comms_age_s` | **req.** | Maximum age of the last successful bus read; older trips |
| `unit_active_power_setpoint_channels` | **req.** | List of setpoint channels pinned to the safe value on a trip |
| `safe_active_power_w` | auto | Safe value asserted on a trip. If absent: 0 W when 0 lies inside the channel envelope, else `p_min_w` (a storage unit parks at its minimum rather than being forced to charge) |
| `device_comms_max_age_s` | off | Per-device read-freshness guard: map `device id → maximum read age` (so one dead device only ages itself) |
| `frozen_measurement_channels` + `max_measurement_frozen_s` | off | Frozen-measurement guard: the bus answers but a watched tag has not changed for too long (a gateway serving cached data). A live power measurement always jitters at least one LSB — watch only jittery tags (the connection-point meter, not `pv.W`, which sits at exactly 0 all night) |
| `max_write_age_s` | off | Write-path guard: trips when setpoints stop reaching the bus even though reads still succeed (remote control lost, half-open socket) |
| `device_comms_watchdog_s` | off | The comms-watchdog period the device was commissioned with. Used only for startup consistency checks (§7.11) |

### 4.6 `allocation:` — physics of the setpoint channels (the arbiter)

A `channels:` list, one entry per setpoint channel:

| Parameter | Default | Explanation |
|---|---|---|
| `setpoint_channel` | **req.** | The setpoint channel (e.g. `pv.WSet`) |
| `p_min_w` / `p_max_w` | **req.** | The device envelope: minimum/maximum active power (`p_max_w` = RfG "Maximum Capacity"). For PV `p_min_w = 0`; for storage `p_min_w < 0` |
| `default_w` | **req.** | Value used when there are no requests at all (for PV usually `= p_max_w`) |
| `ramp_rate_w_per_s` | 5000 | Active power gradient (setpoint slew rate), W/s — shared for both directions |
| `ramp_up_w_per_s` / `ramp_down_w_per_s` | = `ramp_rate_w_per_s` | Separate up/down gradients when different ones are needed |
| `deadband_w` | 200 | Deadband: a target change smaller than this does not rewrite the setpoint (suppresses hunting) |

Ramp and deadband are properties of the **physical unit**, not of any one
control scenario — which is why they live here and not in a controller.

### 4.7 `setpoint_headroom:` — available-power tracking (ON by default)

The problem: when the unit cannot deliver what is commanded (clouds, a
derate), the setpoint parks far above actual production. The device ignores
the excess, but the moment the resource returns (a cloud edge passes),
production jumps to the stale inflated setpoint at the inverter's own speed —
bypassing the configured gradient. The limiter keeps the setpoint at no more
than "actual production + headroom", so the climb happens stepwise under the
arbiter's ramp control.

| Parameter | Default | Explanation |
|---|---|---|
| `enabled` | `true` | `false` disables the limiter |
| `headroom_w` | 10 % of `p_max_w` | The absolute headroom (floor of the allowed excess). Must be > 0, or the unit could never rise above its present output |
| `headroom_pct` | 0 | Dynamic headroom as % of current production; the larger of the two applies |
| `priority` | regulation priority + 1 | Priority of the constraint request |
| the two `..._channel` keys | = regulator bindings | The unit's measured-power tag and the setpoint channel |

### 4.8 `setpoint_compliance:` — setpoint-following monitor (optional)

A Modbus write ACK proves the register was written, not that the command
acts: many inverters silently ignore the active-power setpoint until remote
power control is enabled on the device. The monitor compares measured unit
power against the **applied** setpoint; a sustained overshoot raises the
`sys.setpoint_violation` alarm (no write can fix this — it is for the
operator).

| Parameter | Default | Explanation |
|---|---|---|
| `unit_active_power_channel` | **req.** | Tag of the unit's measured active power |
| `unit_active_power_setpoint_channel` | **req.** | The applied setpoint channel |
| `tolerance_w` | 2000 | Overshoot tolerance (measurement noise + the unit's own response lag) |
| `max_violation_s` | 30 | How long the overshoot must persist continuously to become an alarm |

### 4.9 `hard_switch:` — hard remote start/stop (optional)

A second level above the soft generation gate: not curtailment to 0 W but a
write into the device's own command register(s) (de-energizing the
inverter). Requires `control.command_json` (the operator issues the command
from the UI).

| Parameter | Explanation |
|---|---|
| `start_writes` / `stop_writes` | Lists of `{channel, value}` pairs — which command registers to write, and with what values, for a start/stop. Every channel must have `command: true` in the device profile |

### 4.10 `devices:` — field devices

| Parameter | Default | Explanation |
|---|---|---|
| `id` | — | Tag prefix for the device (`grid`, `pv`, `pv1`, ...) |
| `profile` | **req.** | Path to the YAML profile under `profiles/` |
| `host` | **req.** | IP (Modbus TCP) or serial port path (RTU) |
| `port` | profile default (502) | TCP port |
| `slave_id` | **req.** | Modbus unit/slave id |
| `serial` | 9600 8N1 | RTU settings: `{baudrate, bytesize, parity, stopbits}` |
| `timeout_s`, `retries` | pymodbus defaults | Per-transaction timeout and retries. A hung device costs ~`(retries+1)·timeout_s` per poll — set them explicitly in production |

Devices sharing an endpoint (host:port) share one client — their
`serial`/`timeout` settings must agree.

### 4.11 `telemetry:`, `events:`, `recording:` — observability (optional)

| Parameter | Explanation |
|---|---|
| `telemetry.live_json` | Live-state file for the UI, atomically rewritten each cycle |
| `events.journal_jsonl` | The EMS alarm journal (safety trip/release, rejected requests) |
| `events.ui_audit_jsonl` | Operator-action audit trail, written by the UI process |
| `recording.cycle_csv` | CSV recorder: one row per cycle |
| `recording.channels` | Tag list to record; if absent, every controller-bound tag |

Relative paths resolve against the repository root.

---

## 5. Sign conventions (the key to reading the formulas)

Two conventions coexist — the main source of confusion:

1. **Connection point** (the meter tag, e.g. `grid.W`), "import positive":

   ```
   P_cp > 0  → importing from the grid (consuming)
   P_cp < 0  → exporting to the grid (injecting)
   Export magnitude = max(0, −P_cp)
   ```

2. **The unit and every request on the board** — the "generating-unit"
   convention:

   ```
   P > 0 → injection into the AC bus
   (storage: P > 0 discharge, P < 0 charge)
   ```

The limits `export_limit_w` and `import_limit_w` are always **magnitudes**
(≥ 0).

---

## 6. A worked example: export limiting

1. The meter reads `grid.W = −60 000` (exporting 60 kW), the limit is
   50 kW, the inverter delivers `pv.W = 80 000`.
2. `GridExportLimitController` computes the cap
   `cap = 80 000 + (−60 000) + 50 000 = 70 000 W` and posts the request
   "`pv.WSet ≤ 70 000`" (priority 5, no target).
3. `ConnectionPointPowerController` posts a range plus a preferred target
   (feed-forward + PID trim), priority 10.
4. `SetpointHeadroomLimiter` posts "`pv.WSet ≤ 80 000 + 10 000`",
   priority 11.
5. The arbiter intersects the ranges starting from the envelope
   [0, 100 000], takes the target of the first honored request that has
   one, applies deadband and ramp — and writes a single value into
   `pv.WSet`.
6. Next cycle everything repeats with fresh measurements, so errors are
   self-correcting.

---

## 7. Formulas

Notation: `P_cp` — active power at the connection point (+ import,
− export); `P_unit` — the unit's measured active power;
`clamp(x, a, b) = max(a, min(b, x))`. All quantities are watts.

### 7.1 Export-limit cap (GridExportLimitController)

Reducing unit power by ΔP raises `P_cp` by ΔP. To keep `P_cp` from dropping
below the allowed minimum `−export_limit_w`, the setpoint must not exceed:

```
cap = max(0, P_unit + P_cp + export_limit_w)
```

Posted as a pure upper-bound constraint (`max_w = cap`, no target). Because
`P_unit` is re-measured every cycle, the loop converges on its own
(deadbeat).

The "actually curtailing" indication (log only, with hysteresis):

```
curtailing = cap < P_unit − deadband_w
```

### 7.2 Connection-point regulator, `export_limit` mode (ConnectionPointPowerController)

The request range:

```
cap   = max(0, P_unit + P_cp + export_limit_w)          # ceiling (export side)
floor = max(0, P_unit + P_cp − import_limit_w)          # floor (import side); 0 when import_limit_w = ∞
floor = min(floor, cap)
```

The `P_cp` target and the feed-forward:

```
P_cp_target = −export_limit_w
if import_limit_w is finite and P_cp > import_limit_w:
    P_cp_target = import_limit_w

feedforward = clamp(P_unit + P_cp − P_cp_target, floor, cap)
```

The PID trim (works in "export" coordinates, hence the flipped signs):

```
trim = PID.step(setpoint = −P_cp_target, measurement = −P_cp, dt)
        with output bounds: out_min = floor − feedforward
                            out_max = cap  − feedforward
target = clamp(feedforward + trim, floor, cap)
```

The request: `min_w = floor`, `max_w = cap`, `target_w = target`.

### 7.3 Regulator, `import_limit` mode

Goal: keep grid import from exceeding `import_limit_w` by backing the load
with own generation.

```
if P_cp ≤ import_limit_w + deadband_w:
    the request is withdrawn and the PID resets (the unit runs free)

otherwise:
    floor = max(0, P_unit + P_cp − import_limit_w)      # minimum required generation
    trim  = PID.step(setpoint = −import_limit_w, measurement = −P_cp, dt)
             with out_min = 0, out_max = ∞
    target = max(floor, floor + trim)
```

The request: `min_w = floor`, `target_w = target` (no ceiling — the
envelope and other requests provide it).

### 7.4 Headroom cap (SetpointHeadroomLimiter)

```
base     = max(0, P_unit)               # negative standby draw must not pull the cap down
headroom = max(headroom_w, headroom_pct/100 × base)
cap      = base + headroom
```

The request: `max_w = cap` (pure constraint). The default `headroom_w`
(when not configured) = `0.1 × p_max_w` of the channel.

### 7.5 Discrete PID with anti-windup (PIDController)

Each step of period `dt` (e — error, SP — setpoint, PV — measurement):

```
e   = SP − PV
P   = kp · e
I'  = I + ki · e · dt                        # integral candidate

D (derivative on measurement, first-order filtered):
    raw = −(PV − PV_prev) / dt               # or (e − e_prev)/dt on error
    α   = dt / (kd/(kp·n_filter) + dt)       # 0..1
    D  += α · (kd · raw − D)

u_raw = P + I' + D
u     = clamp(u_raw, out_min, out_max)
```

Back-calculation anti-windup: when the output is clamped, part of the excess
is fed back into the integrator:

```
I = I' + β · (u − u_raw),   β = min(dt / Tt, 1)
```

The tracking time constant `Tt`:

```
Tt = tt, when set in gains
otherwise: Ti = kp/ki,  Td = kd/kp
           Tt = sqrt(Ti · Td)   when Td > 0
           Tt = Ti              when Td = 0
```

External saturation: the arbiter may have applied a different value than the
regulator requested (ramp, envelope, a higher claim). Before the next step:

```
I += β · (applied − requested)
```

— so the integrator does not wind up while someone else owns the command.

### 7.6 Safety interlock (SafetyController)

Trip conditions (any one → trip):

```
sys.comms_age_s            >  max_comms_age_s
sys.<id>.comms_age_s       >  device_comms_max_age_s[id]      (when configured)
sys.write_age_s            >  max_write_age_s                  (when configured)
a tag in frozen_measurement_channels unchanged
                           >  max_measurement_frozen_s         (when configured)
```

On a trip, a priority-0 request is posted on every guarded channel:

```
min_w = max_w = target_w = safe_active_power_w
```

The safe value (when not configured explicitly):

```
safe_active_power_w = 0,        when p_min_w ≤ 0 ≤ p_max_w
                    = p_min_w,  otherwise (storage parks, never force-charges)
```

A priority-0 request bypasses deadband and ramp — the value lands exactly,
in one cycle. On release the claim is withdrawn and the arbiter ramps the
unit smoothly back toward whatever the economic layer wants.

### 7.7 Channel arbitration (ChannelArbiter.resolve, once per cycle)

Input: all live requests of the channel. Steps:

**1. Sort** (deterministic regardless of post order):

```
requests sorted by (priority, requester)
```

**2. Intersect ranges**, starting from the device envelope:

```
[lo, hi] = [p_min_w, p_max_w]
for each request in order:
    [lo', hi'] = [max(lo, min_w), min(hi, max_w)]
    if lo' ≤ hi':  accept  ([lo, hi] = [lo', hi'])
    else:          the request is REJECTED entirely (higher priorities win;
                   the difference is never split) + a journal warning
```

**3. Pick the target:**

```
target = target_w of the first (by priority) honored request that has one
if none has a target:  target = last_setpoint (hold)
on the first-ever cycle: target = default_w
if there are no requests at all: target = default_w
target = clamp(target, lo, hi)
```

**4. Deadband** (bypassed for a priority-0 forced value):

```
if |target − last_setpoint| < deadband_w:  target = last_setpoint
```

**5. Ramp (active power gradient)** — also bypassed by priority 0 and on the
first-ever cycle:

```
Δ        = target − last_setpoint
ramp     = ramp_up_w_per_s   when Δ ≥ 0, else ramp_down_w_per_s
max_step = ramp × fast_cycle_s
value    = last_setpoint + clamp(Δ, −max_step, +max_step)
```

The result is written into the channel and retained as `last_setpoint`
(IEC VAR RETAIN).

**Request TTL** (on the board, before arbitration):

```
a request is dropped when now − posted_at ≥ ttl_s
```

### 7.8 Setpoint-following monitor (SetpointComplianceMonitor)

```
overshoot: P_unit > P_setpoint_applied + tolerance_w
alarm:     the overshoot persists continuously ≥ max_violation_s
           → sys.setpoint_violation = 1
```

Only overshoot is a fault (the setpoint is a cap in the generating-unit
convention; producing less is normal: clouds, night).

### 7.9 Generation gate

```
allowed = (sys.generation_allowed ≥ 0.5)
if not allowed: a priority-1 request: min_w = max_w = target_w = floor_w
floor_w = 0,        when p_min_w ≤ 0 ≤ p_max_w
        = p_min_w,  otherwise
```

### 7.10 Modbus register scaling

```
read:   SI_value = decode(raw registers, type) × scale
write:  raw = int(SI_value / scale)
```

`decode` assembles 32-bit values from two registers (high word first) and
applies two's complement for signed types. Example: `pv.WSet` with
`scale: 100` — the register holds hundreds of watts; the EMS always operates
in watts.

Read plausibility: for a measurement, a value outside `[min_val, max_val]`
fails the poll (the tag keeps its last value, `comms_age` grows) — a wrong
scale/address must not feed the control loop as if it were a measurement.

### 7.11 Timing consistency rules (checked at startup)

The EMS refuses to start on a contradictory configuration:

```
2 × setpoint_rewrite_s   ≤  device_comms_watchdog_s
    (otherwise the keep-alive starves the device watchdog in normal operation)

max_write_age_s          ≥  setpoint_rewrite_s + 2 × poll_interval_s
    (otherwise the healthy keep-alive cadence itself trips the write-age guard)

max_write_age_s          ≤  device_comms_watchdog_s
    (the EMS must raise sys.safe_mode no later than the device fail-safes)

safe_active_power_w      ∈  [p_min_w, p_max_w] of every guarded channel
    (otherwise the arbiter itself would reject the priority-0 claim — safety
     neutralized by a config error)
```

Freshness (all age tags):

```
age = now − time_of_last_success            (∞ until the first success)
sys.comms_age_s = max over all devices      (in per-device mode)
```

---

## 8. Where to look next

- The full registry of internal tags with "who writes / who reads":
  [internal-tags.md](internal-tags.md), or a live cross-reference for a
  concrete site: `pyems-tags --site config/site.yaml`.
- Verifying behavior without hardware: [simulation.md](simulation.md) (the
  simulator exercises the **same** controllers and tuning as production).
- Modbus connection diagnostics (register probe, slave-id scan) — the
  connection tab in the UI (`probe_registers` / `scan_unit_ids` in
  `src/pyems/drivers/modbus_device.py`).
