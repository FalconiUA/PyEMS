# Control design vs. open systems and standards

How PyEMS's export-limit chain compares to OpenEMS, SunSpec and IEEE 1547 —
checked against their sources/specs (June 2026). Conclusion up front: the
core algorithm matches the de-facto standard implementations; the two recent
additions (asymmetric gradient, available-power headroom) correspond to
known patterns from grid codes and utility-scale plant controllers.

## Feed-forward export-limit law — identical to OpenEMS

OpenEMS `Controller PV-Inverter Sell-To-Grid Limit` computes, every cycle:

    limit = gridPower + pvInverter.activePower + maximumSellToGridPower

with the same sign convention (grid import positive). PyEMS's
`GridExportLimitController` / `ConnectionPointPowerController` cap is the
same expression: `p_unit + p_cp + export_limit_w`. Both are deadbeat
feed-forward laws re-evaluated each scan cycle against fresh measurements.

## Rate limiting — grid codes constrain the INCREASE, not curtailment

- IEEE 1547-2018 / Rule 21 ramp settings: DER active power shall *increase*
  linearly or stepwise (default 300 s, steps ≤ 20 % of rating); reductions
  commanded for compliance respond within the (much shorter) open-loop
  response time.
- OpenEMS slews its limit by ±20 % of the last value per cycle in both
  directions — a multiplicative slew that drops fast in absolute terms when
  the limit is large.

PyEMS expresses this directly as `ramp_up_w_per_s` (gentle, grid-code
gradient) and `ramp_down_w_per_s` (fast curtailment) in the allocation
channel config.

## Available-power headroom — utility-scale PPC pattern, absent in OpenEMS

OpenEMS leaves the limit parked at the feed-forward value when the inverter
cannot reach it (resource shortage) and relies on the 1 s closed loop plus
fast multiplicative down-slew to catch the export spike when the resource
returns — i.e. it *accepts* a transient overshoot. PyEMS's
`SetpointHeadroomLimiter` (`max_w = P_unit + headroom_w`) is the proactive
variant: the pattern is known from utility-scale power plant controllers as
available-power tracking / delta control (maintaining the setpoint a fixed
margin above actual output). Trade-off: return to full power takes the
configured up-ramp instead of the inverter's own jump — slightly more
energy left uncollected, no export excursion.

## Comms-loss fallback — same mechanism as SunSpec

SunSpec Immediate Controls (model 123) pairs `WMaxLimPct` with
`WMaxLimPct_RvrtTms`: a device-side revert timer that drops the limit back
to a safe default when the controller stops talking. PyEMS's
`safety.device_comms_watchdog_s` + the CachedDriver keep-alive rewrite is
the same contract (validated at startup: rewrites at least twice per
watchdog period).

## Noted difference (not adopted)

SunSpec and most vendors command a *percentage of rated power*
(`WMaxLimPct`; Huawei also offers % derate @ 40125), while the current
Huawei profile uses the absolute-watts register (40126). Both are valid;
profiles-as-data already allows a percent-based register where a device
needs it (mind the scale).

## References

- OpenEMS controller list: https://openems.github.io/openems.io/openems/latest/edge/controller.html
- OpenEMS SellToGridLimit source: https://github.com/OpenEMS/openems/tree/develop/io.openems.edge.controller.pvinverter.selltogridlimit
- SunSpec model 123 (Immediate Controls): https://github.com/sunspec/models/blob/master/json/model_123.json
- Fronius GEN24 Modbus manual (RvrtTms fallback semantics): https://manuals.fronius.com/html/4204102649/en-US.html
- IEEE 1547-2018 default settings (ramp 300 s, ≤20 % steps): https://www.mass.gov/doc/tsrg-inverter-source-requirements-document/download
- NREL PES-TR67 smart inverter functions report: https://www.nrel.gov/media/docs/libraries/grid/smart-inverters-applications-in-power-systems.pdf
- SMA feed-in limitation at the connection point: https://manuals.sma.de/HM-20/en-US/1071201163.html
