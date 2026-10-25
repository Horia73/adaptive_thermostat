"""Shared central-heater (boiler) coordinator.

Exactly one coordinator exists per boiler entity, stored in
``hass.data[DOMAIN]["coordinators"][entity_id]``.  Zones register, report
their demand and valve state synchronously, and read back their pump-overrun
hold.  All decisions come from :class:`engine.boiler.BoilerPolicy` and are
made without awaiting, so concurrent zone updates cannot race.  Applying the
desired boiler state is a coalesced synchronous step (never concurrent with
itself) that issues short-lived command tasks, confirms the result through
the entity state, retries with back-off and reports a fault when the boiler
is unavailable or does not respond.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import math
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .actuators import CLOSED, OPEN, UNAVAILABLE, CommandTracker, async_command, classify
from .const import (
    ACTUATOR_MAX_ATTEMPTS,
    ACTUATOR_RETRY_BACKOFF,
    ACTUATOR_UNAVAILABLE_GRACE,
    DOMAIN,
    FAULT_CENTRAL_UNAVAILABLE,
)
from .engine.boiler import BoilerConfig, BoilerDecision, BoilerPolicy, ZoneReport

_LOGGER = logging.getLogger(__name__)

BOILER_CONFIRM_TIMEOUT = 45.0
DUTY_TAU_S = 2 * 3600.0
VALVE_TRAVEL_ESTIMATE = 25.0


class CentralHeaterCoordinator:
    """Owns one boiler entity on behalf of all zones using it."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.policy = BoilerPolicy(config=BoilerConfig())
        self.tracker = CommandTracker(entity_id)
        self._zones: dict[str, Callable[[], None]] = {}
        self._zone_limits: dict[str, tuple[float, float]] = {}
        self._holds: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._timer: CALLBACK_TYPE | None = None
        self._timer_at: float | None = None
        self._unsub_state: CALLBACK_TYPE | None = None
        self._step_task: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self.fault: tuple[str, str] | None = None
        self._duty = 0.0
        self._duty_t: float | None = None
        self._duty_on = False
        self._synced = False
        self.last_decision: BoilerDecision | None = None

    # -------------------------------------------------------------- state
    def _now(self) -> float:
        return dt_util.utcnow().timestamp()

    @property
    def kind(self) -> str:
        return classify(self.entity_id, self.hass.states.get(self.entity_id))

    @property
    def actual_on(self) -> bool | None:
        kind = self.kind
        if kind == UNAVAILABLE:
            return None
        return kind == OPEN

    def shared_duty(self) -> float:
        """Recent fraction of time the boiler fired (EWMA, 2 h)."""
        self._update_duty(self._now())
        return self._duty

    def _update_duty(self, now: float) -> None:
        if self._duty_t is not None and now > self._duty_t:
            alpha = 1.0 - math.exp(-(now - self._duty_t) / DUTY_TAU_S)
            self._duty += alpha * ((1.0 if self._duty_on else 0.0) - self._duty)
        self._duty_t = now
        self._duty_on = bool(self.actual_on)

    def start_delay(self, now: float, turn_on_delay: float) -> float:
        """Expected seconds until heat flows for a burst requested now."""
        if self.actual_on:
            return VALVE_TRAVEL_ESTIMATE
        delay = VALVE_TRAVEL_ESTIMATE + turn_on_delay
        if self.policy.off_since is not None:
            delay = max(delay, self.policy.off_since + self.policy.config.min_off_s - now)
        return delay

    def hold_until(self, zone_id: str, now: float) -> float | None:
        return self.policy.hold_until(zone_id, now)

    # ------------------------------------------------------- registration
    @callback
    def register(
        self,
        zone_id: str,
        waker: Callable[[], None],
        *,
        min_on_s: float,
        min_off_s: float,
    ) -> None:
        self._zones[zone_id] = waker
        self._zone_limits[zone_id] = (min_on_s, min_off_s)
        self._apply_limits()
        if not self._synced:
            state = self.hass.states.get(self.entity_id)
            since = state.last_changed.timestamp() if state is not None else self._now()
            self.policy.sync_actual(bool(self.actual_on), since)
            self._synced = True
        if not self._started:
            self._started = True
            self._stopping = False
            self._unsub_state = async_track_state_change_event(
                self.hass, [self.entity_id], self._state_changed
            )
            self.wake()

    @callback
    def seed_running(self, zone_id: str) -> None:
        """A zone found its valves open at startup while the boiler fires."""
        self.policy.seed_running(zone_id)

    async def async_unregister(self, zone_id: str) -> None:
        """Remove a zone (unload).  Its demand ends as a forced stop."""
        self._zones.pop(zone_id, None)
        self._zone_limits.pop(zone_id, None)
        self.policy.remove_zone(zone_id)
        self._apply_limits()
        if self._zones:
            self._evaluate(self._now())
            return
        # Last zone gone: make sure the boiler is off, then stop.
        if self.policy.on:
            self.policy.on = False
            self.policy.off_since = self._now()
        if self.actual_on:
            await async_command(self.hass, self.entity_id, False)
        await self.async_stop()

    async def async_stop(self) -> None:
        self._stopping = True
        self._started = False
        if self._step_task is not None:
            self._step_task.cancel()
            self._step_task = None
        if self._timer is not None:
            self._timer()
            self._timer = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        coordinators = self.hass.data.get(DOMAIN, {}).get("coordinators", {})
        if coordinators.get(self.entity_id) is self:
            del coordinators[self.entity_id]

    def _apply_limits(self) -> None:
        if not self._zone_limits:
            return
        self.policy.config.min_on_s = max(v[0] for v in self._zone_limits.values())
        self.policy.config.min_off_s = max(v[1] for v in self._zone_limits.values())

    # ------------------------------------------------------------ reports
    @callback
    def report(self, zone_id: str, report: ZoneReport) -> None:
        """Synchronous: record a zone's demand and re-evaluate the policy."""
        self.policy.update_zone(zone_id, report)
        self._evaluate(self._now())

    @callback
    def _evaluate(self, now: float) -> None:
        was_on = self.policy.on
        decision = self.policy.evaluate(now)
        self.last_decision = decision
        changed = {
            zid
            for zid in set(decision.holds) | set(self._holds)
            if decision.holds.get(zid) != self._holds.get(zid)
        }
        self._holds = dict(decision.holds)
        for zid in changed:
            waker = self._zones.get(zid)
            if waker is not None:
                waker()
        if decision.on != was_on:
            _LOGGER.debug(
                "Central heater %s -> %s (%s)",
                self.entity_id,
                "on" if decision.on else "off",
                decision.reason,
            )
            self.wake()
        self._schedule(decision.next_wake)

    def _schedule(self, when: float | None) -> None:
        if when is None or self._stopping:
            return
        if self._timer is not None and self._timer_at is not None and self._timer_at <= when:
            return
        if self._timer is not None:
            self._timer()
        self._timer_at = when
        self._timer = async_call_later(self.hass, max(0.0, when - self._now()) + 0.05, self._timer_fired)

    @callback
    def _timer_fired(self, _now: Any) -> None:
        self._timer = None
        self._timer_at = None
        if self._stopping:
            return
        self._evaluate(self._now())
        self.wake()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_duty(self._now())
        self.wake()
        for waker in list(self._zones.values()):
            waker()

    # ---------------------------------------------------------------- step
    @callback
    def wake(self) -> None:
        """Request a reconcile step (coalesced, never concurrent)."""
        if self._step_task is not None or self._stopping or not self._started:
            return
        # A tracked, non-eager task: runs after the current callback, at most
        # one pending at a time, awaited by async_block_till_done in tests and
        # cancelled on unload.  The step itself never awaits.
        self._step_task = self.hass.async_create_task(
            self._async_step(), f"{DOMAIN} central heater {self.entity_id} step", eager_start=False
        )

    async def _async_step(self) -> None:
        self._step_task = None
        self._run_step()

    @callback
    def _run_step(self) -> None:
        if self._stopping:
            return
        try:
            self._reconcile()
        except Exception:
            _LOGGER.exception("Central heater %s reconcile failed", self.entity_id)

    def _reconcile(self) -> None:
        now = self._now()
        self._update_duty(now)
        desired = self.policy.on
        tracker = self.tracker
        kind = self.kind
        previous_fault = self.fault
        if kind == UNAVAILABLE:
            if tracker.unavailable_since is None:
                tracker.unavailable_since = now
            if now - tracker.unavailable_since >= ACTUATOR_UNAVAILABLE_GRACE:
                self.fault = (FAULT_CENTRAL_UNAVAILABLE, "Central heater unavailable")
            else:
                self._schedule(tracker.unavailable_since + ACTUATOR_UNAVAILABLE_GRACE)
        else:
            tracker.unavailable_since = None
            reached = (kind == OPEN) if desired else (kind == CLOSED)
            if reached:
                tracker.target_open = desired
                tracker.attempts = 0
                self.fault = None
            elif tracker.in_flight:
                pass
            elif tracker.target_open == desired and now < tracker.deadline:
                self._schedule(tracker.deadline)
            elif now < tracker.next_allowed:
                self._schedule(tracker.next_allowed)
            else:
                if tracker.target_open == desired and tracker.deadline and now >= tracker.deadline:
                    tracker.attempts += 1
                elif tracker.target_open != desired:
                    tracker.attempts = 0
                if tracker.attempts >= ACTUATOR_MAX_ATTEMPTS:
                    self.fault = (
                        FAULT_CENTRAL_UNAVAILABLE,
                        f"Central heater does not turn {'on' if desired else 'off'}",
                    )
                    tracker.next_allowed = now + ACTUATOR_RETRY_BACKOFF
                    tracker.attempts = 0
                    self._schedule(tracker.next_allowed)
                else:
                    self._send(desired, now)
        if self.fault != previous_fault:
            for waker in list(self._zones.values()):
                waker()

    def _send(self, turn_on: bool, now: float) -> None:
        tracker = self.tracker
        tracker.target_open = turn_on
        tracker.issued_at = now
        tracker.deadline = now + BOILER_CONFIRM_TIMEOUT
        tracker.in_flight = True
        self._schedule(tracker.deadline)

        async def _run() -> None:
            ok = False
            try:
                ok = await async_command(self.hass, self.entity_id, turn_on)
            finally:
                tracker.in_flight = False
                if not ok:
                    tracker.deadline = self._now()
                self.wake()

        task = self.hass.async_create_task(_run(), f"{DOMAIN} central heater {self.entity_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def diagnostics(self) -> dict[str, Any]:
        decision = self.last_decision
        now = self._now()
        return {
            "entity_id": self.entity_id,
            "desired_on": self.policy.on,
            "actual": self.kind,
            "reason": decision.reason if decision else None,
            "holds": {k: round(v - now, 1) for k, v in self._holds.items()},
            "duty": round(self._duty, 3),
            "fault": self.fault[0] if self.fault else None,
            "zones": sorted(self._zones),
        }


def get_coordinator(hass: HomeAssistant, entity_id: str) -> CentralHeaterCoordinator:
    coordinators: dict[str, CentralHeaterCoordinator] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "coordinators", {}
    )
    coordinator = coordinators.get(entity_id)
    if coordinator is None:
        coordinator = CentralHeaterCoordinator(hass, entity_id)
        coordinators[entity_id] = coordinator
    return coordinator
