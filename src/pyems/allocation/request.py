"""
Setpoint requests and the board controllers post them into.

A request is a *standing claim* on one setpoint channel by one named requester.
Reposting replaces the previous claim by the same requester on the same channel.
A claim persists across cycles until replaced, withdrawn, or expired (TTL) — so a
slow task (e.g. a 15-minute planner follower) can keep a claim alive between its
executions while the fast allocator runs every cycle.

All values are active power in watts, generating-unit convention (positive =
injection into the site AC bus; for storage P > 0 = discharge, P < 0 = charge).

Threading: a single foreground thread (the scheduler thread) touches the board,
so there is no locking. Do not share a RequestBoard across threads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivePowerRequest:
    """One requester's standing claim on an active-power setpoint channel.

    `min_w`/`max_w` are a hard range the requester insists on. `target_w` is an
    optional preferred value inside that range (`None` = pure constraint: the
    requester only narrows the range, expresses no preference). `priority` 0 is
    highest (reserved for safety); lower number wins. `ttl_s` is the validity
    window from post time in monotonic seconds (`None` = until replaced/withdrawn).
    """

    requester: str
    priority: int
    min_w: float = float("-inf")
    max_w: float = float("inf")
    target_w: float | None = None
    ttl_s: float | None = None

    def __post_init__(self) -> None:
        # Validation at post time (a frozen dataclass validates on construction).
        if self.min_w > self.max_w:
            raise ValueError(
                f"request from '{self.requester}': min_w ({self.min_w}) > "
                f"max_w ({self.max_w})"
            )
        if self.target_w is not None and not (self.min_w <= self.target_w <= self.max_w):
            raise ValueError(
                f"request from '{self.requester}': target_w ({self.target_w}) "
                f"outside [{self.min_w}, {self.max_w}]"
            )
        if self.priority < 0:
            raise ValueError(
                f"request from '{self.requester}': priority must be >= 0, "
                f"got {self.priority}"
            )
        if self.ttl_s is not None and self.ttl_s <= 0:
            raise ValueError(
                f"request from '{self.requester}': ttl_s must be > 0, got {self.ttl_s}"
            )


class RequestBoard:
    """Per-resource registry controllers post claims into.

    Storage is ``dict[channel, dict[requester, (request, posted_at)]]`` — at most
    one live claim per (channel, requester). The scheduler calls `tick(now)` at
    the top of each cycle so controllers can `post()` without each carrying
    `now`; `valid_requests()` (called by the allocator) lazily purges expired
    claims.
    """

    def __init__(self, setpoint_channels: list[str]) -> None:
        self._claims: dict[str, dict[str, tuple[ActivePowerRequest, float]]] = {
            ch: {} for ch in setpoint_channels
        }
        self._now: float = 0.0

    def tick(self, now: float) -> None:
        """Advance the board's notion of 'now' (monotonic seconds). The scheduler
        calls this once at cycle start; `post()` stamps claims with this time."""
        self._now = now

    @property
    def now(self) -> float:
        """Current scan-cycle timestamp set by `tick()`."""
        return self._now

    def post(self, channel: str, request: ActivePowerRequest, now: float | None = None) -> None:
        """Post (or replace) a requester's claim on a channel.

        Posting against an unknown channel is a programming error — fail loudly.
        `now` defaults to the board's current tick time (the usual call from a
        controller); pass it explicitly only when driving time directly in tests.
        """
        if channel not in self._claims:
            raise ValueError(
                f"post against unknown setpoint channel '{channel}'; "
                f"configured channels: {sorted(self._claims)}"
            )
        posted_at = self._now if now is None else now
        self._claims[channel][request.requester] = (request, posted_at)

    def withdraw(self, channel: str, requester: str) -> None:
        """Remove a requester's claim on a channel. No-op if absent."""
        if channel not in self._claims:
            raise ValueError(
                f"withdraw against unknown setpoint channel '{channel}'; "
                f"configured channels: {sorted(self._claims)}"
            )
        self._claims[channel].pop(requester, None)

    def valid_requests(self, channel: str, now: float) -> list[ActivePowerRequest]:
        """Non-expired claims on a channel, sorted by ``(priority, requester)``.

        Expired claims are purged here (lazily) and logged once on the dropping
        cycle — by construction a one-shot transition, since the entry is gone
        afterward.
        """
        claims = self._claims[channel]
        expired: list[str] = []
        live: list[ActivePowerRequest] = []
        for requester, (request, posted_at) in claims.items():
            if request.ttl_s is not None and now - posted_at >= request.ttl_s:
                expired.append(requester)
                continue
            live.append(request)
        for requester in expired:
            request, _ = claims.pop(requester)
            logger.warning(
                "%s: request from '%s' dropped (TTL %.1fs expired)",
                channel, requester, request.ttl_s,
            )
        live.sort(key=lambda r: (r.priority, r.requester))
        return live
