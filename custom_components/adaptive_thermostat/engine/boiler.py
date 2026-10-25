"""Shared boiler (central heater) policy.

One policy object exists per boiler entity; every zone that uses that boiler
reports its state and the policy decides, synchronously and deterministically:

* when the boiler may fire: a demanding zone has its valves confirmed open,
  the turn-on delay has elapsed, the minimum off time has passed and no
  *non-demanding* zone still has an open valve (so a zone that was just
  switched off can never receive heat from another zone's burst);
* when it stops: the last demand is gone; after a normal end of burst the
  minimum on time is honoured (anti short-cycling), after a forced stop
  (mode OFF, window, fault) it stops immediately;
* pump overrun: the zones that were heating when the boiler stops keep their
  valves open for their ``turn_off_delay`` (the existing
  ``central_heater_turn_off_delay`` semantics); a zone that stops while other
  zones still heat closes immediately.

Because every evaluation runs to completion without awaiting, two zones that
stop "simultaneously" are simply processed one after the other: the first
sees the other still heating and closes, the second is the last demand and
turns the boiler off.  There is no window in which both believe the other
keeps the boiler on.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ZoneReport:
    """What a zone tells the boiler policy."""

    demand: bool  # wants heat now
    forced_stop: bool = False  # demand was removed for safety (OFF/window/fault)
    valves_open: bool = False  # all zone valves confirmed open
    valves_closed: bool = True  # all zone valves confirmed closed
    turn_on_delay_s: float = 10.0
    turn_off_delay_s: float = 120.0


@dataclass
class BoilerConfig:
    """Anti short-cycling limits for one boiler."""

    min_on_s: float = 120.0
    min_off_s: float = 180.0
    close_wait_s: float = 90.0  # max wait for stray open valves before firing


@dataclass
class BoilerDecision:
    """Desired boiler state plus per-zone valve holds."""

    on: bool
    holds: dict[str, float]
    next_wake: float | None
    reason: str


@dataclass
class BoilerPolicy:
    """Pure decision logic for a shared boiler."""

    config: BoilerConfig = field(default_factory=BoilerConfig)
    on: bool = False
    on_since: float | None = None
    off_since: float | None = None
    holds: dict[str, float] = field(default_factory=dict)
    zones: dict[str, ZoneReport] = field(default_factory=dict)
    _ready_since: float | None = None
    _blocked_since: float | None = None
    _pending_off_at: float | None = None
    _last_demanders: set[str] = field(default_factory=set)
    _forced_release: set[str] = field(default_factory=set)
    reason: str = "idle"

    def sync_actual(self, on: bool, since: float) -> None:
        """Initialise from the boiler's real state (startup)."""
        self.on = on
        if on:
            self.on_since = since
            self.off_since = None
        else:
            self.off_since = since
            self.on_since = None

    def update_zone(self, zone_id: str, report: ZoneReport) -> None:
        prev = self.zones.get(zone_id)
        if prev is not None and prev.demand and not report.demand and report.forced_stop:
            self._forced_release.add(zone_id)
        self.zones[zone_id] = report

    def remove_zone(self, zone_id: str, *, forced: bool = True) -> None:
        if zone_id in self.zones and self.zones[zone_id].demand and forced:
            self._forced_release.add(zone_id)
        self.zones.pop(zone_id, None)
        self.holds.pop(zone_id, None)

    def seed_running(self, zone_id: str) -> None:
        """Treat a zone as having been heating (startup: valves open, boiler on).

        Its next report without demand is processed as a forced release, so
        the boiler stops at once and the zone keeps its pump-overrun hold.
        """
        self._last_demanders.add(zone_id)
        self._forced_release.add(zone_id)

    def hold_until(self, zone_id: str, now: float) -> float | None:
        until = self.holds.get(zone_id)
        return until if until is not None and until > now else None

    # ---------------------------------------------------------------- core
    def evaluate(self, now: float) -> BoilerDecision:
        cfg = self.config
        demanders = {zid for zid, z in self.zones.items() if z.demand}
        released = self._last_demanders - demanders
        forced = bool(released & self._forced_release) or bool(self._forced_release - set(self.zones))
        self._forced_release.clear()
        wakes: list[float] = []

        # Expire holds.
        for zid in [z for z, until in self.holds.items() if until <= now]:
            del self.holds[zid]

        if self.on:
            if demanders:
                self._pending_off_at = None
                # Zones that stopped while others still heat close at once, and
                # no idle zone may keep a valve open while the boiler fires.
                for zid in list(self.holds):
                    if zid not in demanders:
                        del self.holds[zid]
                self.reason = "heating"
            else:
                min_on_end = (self.on_since or now) + cfg.min_on_s
                if self._pending_off_at is None and not forced and now < min_on_end:
                    # Normal end of burst before the minimum on time: keep
                    # firing (anti short-cycling) with the releasing zones open.
                    self._pending_off_at = min_on_end
                    for zid in released:
                        delay = self._delay(zid)
                        self.holds[zid] = min_on_end + delay
                if forced or self._pending_off_at is None or now >= self._pending_off_at:
                    self._turn_off(now, released)
                else:
                    self.reason = "minimum on time"
                    wakes.append(self._pending_off_at)
        elif demanders:
            # A new demand cancels stray overrun holds of idle zones; their
            # valves must close before the boiler fires again.
            for zid in list(self.holds):
                if zid not in demanders:
                    del self.holds[zid]
            ready = [zid for zid in demanders if self.zones[zid].valves_open]
            stray_open = [zid for zid, z in self.zones.items() if not z.demand and not z.valves_closed]
            if not ready:
                self._ready_since = None
                self.reason = "waiting for valves"
            else:
                if self._ready_since is None:
                    self._ready_since = now
                on_delay = max(self.zones[zid].turn_on_delay_s for zid in ready)
                earliest = self._ready_since + on_delay
                if self.off_since is not None:
                    earliest = max(earliest, self.off_since + cfg.min_off_s)
                if stray_open:
                    if self._blocked_since is None:
                        self._blocked_since = now
                    earliest = max(earliest, min(now + 5.0, self._blocked_since + cfg.close_wait_s))
                    if now < self._blocked_since + cfg.close_wait_s:
                        self.reason = "waiting for idle zones to close"
                        wakes.append(self._blocked_since + cfg.close_wait_s)
                        earliest = max(earliest, self._blocked_since + cfg.close_wait_s)
                else:
                    self._blocked_since = None
                if now >= earliest:
                    self.on = True
                    self.on_since = now
                    self.off_since = None
                    self._ready_since = None
                    self._blocked_since = None
                    self._pending_off_at = None
                    self.reason = "heating"
                else:
                    if self.reason != "waiting for idle zones to close":
                        self.reason = "turn-on delay / minimum off time"
                    wakes.append(earliest)
        else:
            self._ready_since = None
            self._blocked_since = None
            self.reason = "idle"

        self._last_demanders = demanders
        wakes.extend(until for until in self.holds.values() if until > now)
        next_wake = min(wakes) if wakes else None
        return BoilerDecision(on=self.on, holds=dict(self.holds), next_wake=next_wake, reason=self.reason)

    def _delay(self, zone_id: str) -> float:
        zone = self.zones.get(zone_id)
        return zone.turn_off_delay_s if zone is not None else 0.0

    def _turn_off(self, now: float, released: set[str]) -> None:
        self.on = False
        self.off_since = now
        self.on_since = None
        self._pending_off_at = None
        # Pump overrun: the zones that were heating keep their valves open.
        for zid in released:
            zone = self.zones.get(zid)
            if zone is None:
                continue
            delay = zone.turn_off_delay_s
            if delay > 0:
                self.holds[zid] = max(self.holds.get(zid, 0.0), now + delay)
        # Holds set while waiting for the minimum on time are re-based on the
        # actual stop time.
        for zid, until in list(self.holds.items()):
            zone = self.zones.get(zid)
            if zone is not None:
                self.holds[zid] = min(until, now + zone.turn_off_delay_s)
        self.reason = "stopped"
