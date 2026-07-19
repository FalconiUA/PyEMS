# Simulation harness (`pyems-sim`)

Run the **real, unmodified EMS** against simulated hardware. The simulator
serves genuine Modbus TCP (a pymodbus server per device, register maps taken
from the same `profiles/*.yaml` the production driver loads), so the whole
production pipeline is exercised: `build_ems()`, `CachedDriver` background
polling, controllers, `PowerAllocator`, setpoint writes on the wire.

## Quick start

From the configuration UI: run `pyems-ui`, open the **Simulation** tab and
press **Start simulator** — the control panel appears right in the tab (and a
simulator started by hand is detected too). Then start the EMS in a terminal:

    pyems --site config/site.sim.yaml

Or fully by hand, without the configuration UI:

    terminal 1:  pyems-sim                          # simulated devices + panel
    terminal 2:  pyems --site config/site.sim.yaml  # the real EMS
    browser:     http://127.0.0.1:8766

`config/site.sim.yaml` has the same schema as `site.yaml`; the devices simply
point at `127.0.0.1:15021/15022`. Sim-only knobs (inverter lag `tau_s`, meter
noise, synthetic-curve defaults) live in its optional `simulation:` section,
which the EMS ignores.

## Control panel

For each quantity (**PV generation** and **site load**) pick a source, live:

- **Synthetic curve** — base + amplitude · sin(2πt/period) + noise; a fast
  "day/night" cycle by default.
- **Manual value** — type or slide a fixed W value.
- **Replay CSV** — upload/paste recorded 1-second samples (your real PV and
  load data); plain values one per line, or `time,value` rows — the last
  numeric field of each line is used. Speed factor compresses time; loop or
  hold the last sample at the end.

The charts show PV available/actual/EMS setpoint and load/connection-point
power against the configured export limit, so curtailment and ramping are
visible as they happen.

## Generator / island (the ATS toggle)

When the site yaml has a `generator_minimum_load:` section and a generator
meter device (the sim one serves `profiles/meters/
generator_meter_huawei_smartlogger3000.yaml` on port 15023), the panel shows
a **Grid (ATS)** panel and the connection-point chart gains the generator
power line plus its minimum-load floor:

- **Drop the grid** — island on the genset: the grid meter reads 0 (open
  ATS) and the generator covers `load − PV`, including *reverse power* when
  PV over-produces at the moment of transfer. Watch the EMS detect the
  running generator (`sys.generator_running`), cap PV within a couple of
  cycles and hold the generator at or above its minimum load. Then drive the
  load below the floor — PV must go to 0 while the generator carries the
  whole site.
- **Restore the grid** — the generator meter falls to 0; after the
  configured `off_delay_s` the EMS withdraws the minimum-load cap and the
  normal export/import program ramps PV back.

## Fault injection (per device)

| Fault | What the EMS sees | Expected reaction |
|---|---|---|
| Offline | TCP connect/read errors | comms age grows → safety trip (`sys.safe_mode`) |
| Freeze registers | bit-identical measurements | frozen-measurement guard trips |
| Modbus exceptions | true Modbus error responses | poll fails, comms age grows |
| Reject setpoint writes | exception on write | `ModbusWriteError`, WRITE-failed log, retries |
| Ignore setpoint (unit only) | unit power ≠ applied setpoint | `sys.setpoint_violation` after `max_violation_s` |

Faults toggle on/off without restarting anything, so recovery paths (safety
release, ramp back, write retry) are testable too.

## What it does not cover

Vendor firmware quirks, RTU/RS-485 electrical behaviour, and the real
inverter's control dynamics — those still need a hardware acceptance run.
