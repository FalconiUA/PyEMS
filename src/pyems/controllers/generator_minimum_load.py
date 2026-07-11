"""
Generator minimum-load protection while the site runs on a genset.

When the grid is present the ATS feeds the site from the network and the
connection-point program (export/import limit) governs the unit. When the
grid is lost and the generator carries the site, a PV unit left uncurtailed
displaces generator load: a diesel genset running below its minimum load
(wet stacking, glazing) or into reverse power is a machinery-damage case.
This controller keeps the generator loaded at or above a configured minimum.

Generator parameters (nameplate, from site.yaml):
  - rated_apparent_power_va : S_rated [VA] — nameplate apparent power.
  - rated_active_power_w    : P_rated [W]  — nameplate active power (P ≤ S).
  - minimum_load_pct        : minimum loading as % of P_rated. The engine
    limit is an ACTIVE-power (kW) limit, so the floor derives from P_rated:
        P_gen_min = minimum_load_pct / 100 × P_rated

Terminology follows EN 50549 / grid-code style (see CLAUDE.md): the
controlled unit is any generating unit (`unit_*` bindings); the generator is
observed through its own meter channel (generating convention, P > 0 =
injection into the AC bus).

IEC 61131-3 equivalent:
  FUNCTION_BLOCK GeneratorMinimumLoad
    VAR_INPUT
      p_generator_w : REAL;   (* generator meter active power, + = producing *)
      p_unit_w      : REAL;   (* controlled unit active power *)
    END_VAR
    VAR_OUTPUT
      p_setpoint_cap_w    : REAL;  (* upper bound posted while running *)
      generator_running   : BOOL;  (* sys.generator_running status word *)
    END_VAR
  END_FUNCTION_BLOCK

Control law (feed-forward, self-correcting each scan): on an islanded bus the
load is shared, `P_load = P_gen + P_unit`, so raising the unit by ΔP lowers
the generator by ΔP. To keep P_gen ≥ P_gen_min the unit may rise at most by
the generator's margin above its floor:
      p_setpoint_cap = p_unit + (p_gen − P_gen_min)
Because both terms are re-measured every cycle the loop converges (deadbeat),
exactly like the export-limit law. The cap is floored at 0 (full curtailment);
reverse power (p_gen < 0) therefore immediately demands cap 0.

Run detection: the generator is RUNNING when |P_gen| ≥ running_threshold_w
(magnitude, so a reverse-powered generator still counts as running), and
STOPPED only after |P_gen| stays below the threshold for off_delay_s — a
transient dip through zero must not drop the cap for a cycle. When the grid
returns and the changeover switch disconnects the generator, its meter reads
~0 W and the claim is withdrawn after the delay; the connection-point program
takes over untouched (it never stopped posting).

This controller is a **pure constraint**: it posts the cap as an upper bound
(`max_w`) request and expresses no preferred value. The PowerAllocator owns
the setpoint channel; the tightest cap among requesters wins by range
intersection, so this coexists with the export-limit and headroom caps
without special-casing.
"""
import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller
from pyems.system_tags import GENERATOR_RUNNING_CHANNEL

logger = logging.getLogger(__name__)

# Default run-detection threshold as a fraction of P_rated: low enough that an
# idling-but-connected generator is detected, high enough to sit above meter
# noise around 0 W when the generator breaker is open.
DEFAULT_RUNNING_THRESHOLD_FRACTION = 0.02

# Default seconds |P_gen| must stay below the threshold before the generator
# is declared stopped (debounces a dip through zero during a PV transient).
DEFAULT_OFF_DELAY_S = 5.0


class GeneratorMinimumLoadController(Controller):
    def __init__(
        self,
        name: str,
        priority: int,
        rated_apparent_power_va: float,
        rated_active_power_w: float,
        minimum_load_pct: float,
        generator_active_power_channel: str,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        running_threshold_w: float | None = None,
        off_delay_s: float = DEFAULT_OFF_DELAY_S,
        deadband_w: float = 200.0,
    ) -> None:
        if rated_active_power_w <= 0:
            raise ValueError("rated_active_power_w must be > 0")
        if rated_apparent_power_va < rated_active_power_w:
            raise ValueError(
                f"rated_apparent_power_va ({rated_apparent_power_va}) must be >= "
                f"rated_active_power_w ({rated_active_power_w}) — S is the "
                f"nameplate envelope P lives inside"
            )
        if not 0 < minimum_load_pct <= 100:
            raise ValueError(
                f"minimum_load_pct must be in (0, 100], got {minimum_load_pct}"
            )
        if running_threshold_w is None:
            running_threshold_w = (
                DEFAULT_RUNNING_THRESHOLD_FRACTION * rated_active_power_w
            )
        if running_threshold_w <= 0:
            raise ValueError("running_threshold_w must be > 0")
        if off_delay_s < 0:
            raise ValueError("off_delay_s must be >= 0")
        self._name = name              # requester key on the board (unique per instance)
        self._priority = priority      # machine-protection band (same as export limit)
        self._rated_apparent_power_va = float(rated_apparent_power_va)
        self._rated_active_power_w = float(rated_active_power_w)
        # The engine's minimum-load floor in W (see module docstring).
        self._minimum_load_w = minimum_load_pct / 100.0 * float(rated_active_power_w)
        self._running_threshold_w = float(running_threshold_w)
        self._off_delay_s = float(off_delay_s)
        # IEC VAR_INPUT/VAR_OUTPUT binding — which tags this instance reads/drives.
        # Set per device from site.yaml, so the same class serves any unit.
        # All are ACTIVE power (P, W) — distinct from reactive (Q) / apparent (S).
        self._gen_active_power_ch = generator_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        # Hysteresis for the ENGAGED/RELEASED log transition only (control
        # deadband lives in the channel's allocator config, not here).
        self._deadband_w = deadband_w
        # RETAIN state: run detection latch + curtailment log state.
        self._running = False
        self._below_since: float | None = None
        self._curtailing = False

    @property
    def minimum_load_w(self) -> float:
        """The generator active-power floor [W] derived from the nameplate."""
        return self._minimum_load_w

    def _update_running(self, p_gen_w: float, now: float) -> None:
        """Run-detection latch with an off-delay (see module docstring)."""
        if abs(p_gen_w) >= self._running_threshold_w:
            self._below_since = None
            if not self._running:
                self._running = True
                logger.info(
                    "Generator RUNNING detected: %s=%.0f W (threshold %.0f W); "
                    "enforcing minimum load %.0f W",
                    self._gen_active_power_ch, p_gen_w,
                    self._running_threshold_w, self._minimum_load_w,
                )
            return
        if self._below_since is None:
            self._below_since = now
        if self._running and now - self._below_since >= self._off_delay_s:
            self._running = False
            logger.info(
                "Generator STOPPED: %s below %.0f W for %.0fs; minimum-load cap withdrawn",
                self._gen_active_power_ch, self._running_threshold_w, self._off_delay_s,
            )

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        # VAR_INPUT reads (P = active power, generating convention)
        p_gen = state.get(self._gen_active_power_ch)    # generator meter
        p_unit = state.get(self._unit_active_power_ch)  # controlled unit

        self._update_running(p_gen, board.now)
        state.set(GENERATOR_RUNNING_CHANNEL, 1.0 if self._running else 0.0)

        if not self._running:
            # Grid operation (or generator off): the connection-point program
            # governs; this controller holds no claim on the setpoint channel.
            board.withdraw(self._setpoint_ch, self._name)
            if self._curtailing:
                logger.info(
                    "Generator-min-load RELEASED: generator stopped, %s cap withdrawn",
                    self._setpoint_ch,
                )
                self._curtailing = False
            return

        # feed-forward cap (see module docstring derivation). Lower-bounded at 0
        # (full curtailment); the unit's P_max upper bound is enforced by the
        # allocator's device envelope.
        cap = max(0.0, p_unit + p_gen - self._minimum_load_w)

        # VAR_OUTPUT: post the cap as a pure upper-bound constraint (no target).
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                max_w=cap,  # min stays -inf; no target_w
            ),
        )

        # Log curtailment as a state transition (with hysteresis, like the
        # export limit): we actually curtail only when the cap holds production
        # BELOW what the unit currently makes.
        curtailing = cap < p_unit - self._deadband_w
        if curtailing and not self._curtailing:
            logger.info(
                "Generator-min-load ENGAGED: P_gen=%.0f W < floor+margin, "
                "capping %s to %.0f W (floor %.0f W)",
                p_gen, self._setpoint_ch, cap, self._minimum_load_w,
            )
        elif not curtailing and self._curtailing:
            logger.info(
                "Generator-min-load RELEASED: %s cap above production", self._setpoint_ch
            )
        self._curtailing = curtailing
        logger.debug(
            "%s: P_gen=%.0f P_unit=%.0f floor=%.0f -> cap=%.0f W",
            self._setpoint_ch, p_gen, p_unit, self._minimum_load_w, cap,
        )
