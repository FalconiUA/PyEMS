"""
The arbitration stage: resolve contending requests into one setpoint per channel.

IEC 61131-3 analogy: controllers are FUNCTION_BLOCKs whose setpoint VAR_OUTPUTs
are *requests*; the allocator is the output-image arbitration stage that resolves
contention before the output scan. Conceptually like the OpenEMS constraint
solver, but deliberately simpler: interval intersection in strict priority order,
no LP solving.

One `ChannelArbiter` owns one setpoint channel: its device envelope, ramp rate,
deadband, default, and the retained `last_setpoint` (IEC VAR RETAIN). The
`PowerAllocator` holds one arbiter per configured channel and is the sole writer
of those channels. Resolution is deterministic for identical inputs regardless of
request post order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pyems.allocation.request import ActivePowerRequest
from pyems.channels import SystemState
from pyems.events import SEVERITY_WARNING

logger = logging.getLogger(__name__)

# Owner labels for the two non-requester target sources, used in transition logs.
_HOLD = "<hold-last>"
_DEFAULT = "<default>"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class SetpointChannelConfig:
    """Per-channel physical properties (device envelope, gradient, deadband).

    `p_min_w`/`p_max_w` are the device envelope in RfG terms (`p_max_w` =
    Maximum Capacity). `default_w` is the fail-safe value written when no valid
    request exists (for a PV unit `default_w = p_max_w` reproduces "run free until
    told otherwise"). Ramp and deadband are properties of the physical unit, not
    of any one control scenario, so they live here rather than in a controller.
    """

    setpoint_channel: str
    p_min_w: float
    p_max_w: float
    default_w: float
    ramp_rate_w_per_s: float = 5000.0
    ramp_up_w_per_s: float | None = None
    ramp_down_w_per_s: float | None = None
    deadband_w: float = 200.0

    def __post_init__(self) -> None:
        if self.ramp_rate_w_per_s < 0:
            raise ValueError("ramp_rate_w_per_s must be >= 0")
        ramp_up_w_per_s = (
            self.ramp_rate_w_per_s
            if self.ramp_up_w_per_s is None
            else self.ramp_up_w_per_s
        )
        ramp_down_w_per_s = (
            self.ramp_rate_w_per_s
            if self.ramp_down_w_per_s is None
            else self.ramp_down_w_per_s
        )
        if ramp_up_w_per_s < 0 or ramp_down_w_per_s < 0:
            raise ValueError("ramp_up_w_per_s and ramp_down_w_per_s must be >= 0")
        object.__setattr__(self, "ramp_up_w_per_s", float(ramp_up_w_per_s))
        object.__setattr__(self, "ramp_down_w_per_s", float(ramp_down_w_per_s))


class ChannelArbiter:
    """Resolves one setpoint channel per cycle (§3.3). Holds the retained
    `last_setpoint` and the per-requester / target-owner transition-log state.

    `resolve()` is pure (requests in, value out — no SystemState) so it is easy
    to unit-test; `PowerAllocator` does the `state.set`.
    """

    def __init__(self, config: SetpointChannelConfig, cycle_s: float, journal=None) -> None:
        self._cfg = config
        self._cycle_s = cycle_s
        self._last_setpoint: float | None = None          # VAR RETAIN; None = never resolved
        self._honored: dict[str, bool] = {}               # requester -> honored last cycle?
        self._target_owner: str | None = None             # owner of the winning target last cycle
        # Optional alarm journal (pyems.events.EventJournal). When set, a request
        # rejected by an empty intersection raises a `warning`, cleared when it is
        # honored again or withdrawn. None keeps `resolve()` a pure, testable API.
        self._journal = journal

    def resolve(self, requests: list[ActivePowerRequest], now: float | None = None) -> float:
        cfg = self._cfg
        ch = cfg.setpoint_channel
        # Journal records are stamped with the control-loop `now`; pure-API callers
        # (no journal) may omit it.
        now_s = 0.0 if now is None else now

        # 1. Sort deterministically (board already sorts; repeat for a pure API).
        requests = sorted(requests, key=lambda r: (r.priority, r.requester))

        # 2. Intersect ranges in priority order, starting from the device envelope.
        lo, hi = cfg.p_min_w, cfg.p_max_w
        honored: list[ActivePowerRequest] = []
        seen: set[str] = set()
        for req in requests:
            seen.add(req.requester)
            new_lo, new_hi = max(lo, req.min_w), min(hi, req.max_w)
            if new_lo <= new_hi:
                lo, hi = new_lo, new_hi
                honored.append(req)
                self._note_honored(ch, req.requester, True, now_s)
            else:
                # Higher-priority constraints always win — discard this range
                # entirely, never split the difference.
                self._note_honored(ch, req.requester, False, now_s)
        # Forget requesters no longer present (withdrawn/expired); a request that
        # was rejected and then vanishes must have its standing alarm cleared too.
        for requester in [r for r in self._honored if r not in seen]:
            if self._journal is not None and self._honored[requester] is False:
                self._journal.clear(
                    source="allocation", key=f"alloc.{ch}.{requester}", now=now_s
                )
        self._honored = {r: v for r, v in self._honored.items() if r in seen}

        # 3. Pick the target.
        if not requests:
            # No valid requests at all → fail-safe known value.
            target, owner = cfg.default_w, _DEFAULT
            forced_by_safety = False
        else:
            target, owner, forced_by_safety = None, None, False
            for req in honored:
                if req.target_w is not None:
                    target, owner = req.target_w, req.requester
                    forced_by_safety = req.priority == 0
                    break
            if target is None:
                # Only pure-constraint requests honored: hold last, or default on
                # the first-ever cycle.
                if self._last_setpoint is not None:
                    target, owner = self._last_setpoint, _HOLD
                else:
                    target, owner = cfg.default_w, _DEFAULT
        target = _clamp(target, lo, hi)

        # 4. Deadband — suppress hunting. Bypassed for a priority-0 forced value
        #    (safety setpoints must land exactly).
        if (
            self._last_setpoint is not None
            and not forced_by_safety
            and abs(target - self._last_setpoint) < cfg.deadband_w
        ):
            target = self._last_setpoint

        # 5. Ramp limit (active power gradient). A priority-0 forced value is a
        #    step change by definition — applied immediately, no ramp. First-ever
        #    cycle has no reference, so it also lands directly.
        if forced_by_safety or self._last_setpoint is None:
            value = target
        else:
            delta = target - self._last_setpoint
            ramp_rate_w_per_s = (
                cfg.ramp_up_w_per_s if delta >= 0 else cfg.ramp_down_w_per_s
            )
            max_step = ramp_rate_w_per_s * self._cycle_s
            value = self._last_setpoint + _clamp(
                delta, -max_step, max_step
            )

        self._note_target_owner(ch, owner, value)
        logger.debug(
            "%s: envelope=[%.0f, %.0f] owner=%s target=%.0f -> %.0f W",
            ch, lo, hi, owner, target, value,
        )
        self._last_setpoint = value  # RETAIN
        return value

    # -- transition logging (state changes only; never per-cycle spam) ----------

    def _note_honored(
        self, channel: str, requester: str, honored: bool, now: float
    ) -> None:
        prev = self._honored.get(requester)
        self._honored[requester] = honored
        if prev == honored:
            return
        if not honored:
            logger.warning(
                "%s: request from '%s' rejected (empty intersection with "
                "higher-priority constraints)", channel, requester,
            )
            if self._journal is not None:
                self._journal.raise_alarm(
                    source="allocation",
                    key=f"alloc.{channel}.{requester}",
                    message=(
                        f"{channel}: request from '{requester}' rejected "
                        f"(empty intersection with higher-priority constraints)"
                    ),
                    severity=SEVERITY_WARNING,
                    now=now,
                )
        elif prev is not None:  # rejected -> honored again (don't log first sight)
            logger.info("%s: request from '%s' honored again", channel, requester)
            if self._journal is not None:
                self._journal.clear(
                    source="allocation", key=f"alloc.{channel}.{requester}", now=now
                )

    def _note_target_owner(self, channel: str, owner: str, value: float) -> None:
        if owner == self._target_owner:
            return
        self._target_owner = owner
        logger.info(
            "%s: target now from %s, %.1f kW", channel, owner, value / 1000.0
        )


class PowerAllocator:
    """Resolves every configured setpoint channel once per cycle; sole writer of
    those channels. Runs every cycle unconditionally (TTLs and ramps evolve each
    cycle), driven by the same `now` the scheduler passes to `step()`."""

    def __init__(
        self, configs: list[SetpointChannelConfig], board, cycle_s: float, journal=None
    ) -> None:
        self._board = board
        self._arbiters: dict[str, ChannelArbiter] = {
            cfg.setpoint_channel: ChannelArbiter(cfg, cycle_s, journal=journal)
            for cfg in configs
        }

    @property
    def channels(self) -> list[str]:
        return list(self._arbiters)

    def resolve(self, state: SystemState, now: float) -> None:
        """Run §3.3 for every configured channel and write the result. Never
        touches channels it is not configured for."""
        for channel, arbiter in self._arbiters.items():
            requests = self._board.valid_requests(channel, now)
            value = arbiter.resolve(requests, now)
            state.set(channel, value)
