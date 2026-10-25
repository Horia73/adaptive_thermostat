"""Zone core: everything one zone decides, independent of Home Assistant.

``ZoneCore`` owns the observer (estimator), the window detector, the sensor
health monitor, the burst controller and the burst state machine.  The Home
Assistant layer feeds it events and executes its ``want_heat`` output through
the actuator reconciler and the shared boiler coordinator; the simulator and
the tests drive exactly the same object.

Burst state machine::

    idle --start--> requested --heat flows--> heating --stop--> idle
                        \\--no heat within start_timeout--> idle

Heat time is counted only while heat actually flows (valve open *and* boiler
firing, as measured), so a burst delayed by the boiler's minimum off time or
by slow valves still delivers the planned amount of heat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import statistics
from typing import Any

from .controller import (
    BurstController,
    ControlDecision,
    ControlInput,
    ControllerConfig,
)
from .estimator import EstimatorConfig, ZoneEstimator
from .model import SECONDS_PER_HOUR, ThermalParams, ZoneState, simulate_burst, trajectory
from .observer import ObserverConfig, ZoneObserver, overrun_profile
from .safety import FAULT_SENSOR_STALE, FAULT_SENSOR_UNAVAILABLE, SafetyConfig, SensorHealth
from .window import WindowConfig, WindowDetector, WindowEvent

FAULT_VALVE_MISMATCH = "valve_mismatch"
FAULT_VALVE_UNAVAILABLE = "valve_unavailable"
FAULT_CENTRAL_UNAVAILABLE = "central_unavailable"
FAULT_PRIORITY = (
    FAULT_SENSOR_UNAVAILABLE,
    FAULT_SENSOR_STALE,
    FAULT_VALVE_UNAVAILABLE,
    FAULT_CENTRAL_UNAVAILABLE,
    FAULT_VALVE_MISMATCH,
)

TICK_S = 60.0
CORE_STORAGE_FORMAT = 1
# Physics stale check: heat episodes are evaluated against the model.
EPISODE_GAP_S = 600.0  # heat pauses shorter than this belong to one episode
EPISODE_MAX_HEAT_S = 40 * 60.0  # evaluate long continuous heating in chunks
EPISODE_SETTLE_S = 30 * 60.0  # wait this long after the heat for the response


@dataclass
class ZoneConfig:
    """Per-zone settings (from the config entry options)."""

    deadband: float = 0.1
    window_detection: bool = False
    use_model: bool = True
    start_timeout_s: float = 15 * 60.0
    mode_change_block_s: float = 15 * 60.0
    window_data_block_s: float = 30 * 60.0
    turn_off_delay_s: float = 120.0  # pump overrun hold of the boiler coordinator
    validation_window: int = 8
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    estimator: EstimatorConfig = field(default_factory=EstimatorConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


@dataclass
class Burst:
    """An active heating burst."""

    requested_at: float
    planned_s: float
    mode: str
    heat_started_at: float | None = None
    heat_elapsed_s: float = 0.0
    predicted_peak: float | None = None
    start_state: ZoneState | None = None
    start_params: ThermalParams | None = None


@dataclass
class PendingValidation:
    """A finished burst waiting for its tail to be observed."""

    started_at: float
    predicted_peak: float
    until: float
    observed_max: float | None = None


@dataclass
class ZoneStatus:
    """Output of :meth:`ZoneCore.update`."""

    want_heat: bool
    forced_stop: bool
    heat_flowing: bool
    burst_active: bool
    planned_burst_s: float | None
    heat_elapsed_s: float
    control_mode: str
    confidence: float
    predicted_peak: float | None
    tail_s: float | None
    time_to_peak_s: float | None
    fault: str | None
    fault_message: str | None
    window_open: bool
    window_alert: str | None
    learning: bool
    next_wake: float
    reason: str
    slope_instant: float
    slope_hourly: float
    window_event: WindowEvent | None = None


class ZoneCore:
    """All decisions of one zone (pure Python, injectable time)."""

    def __init__(
        self,
        config: ZoneConfig,
        *,
        stored: dict[str, Any] | None = None,
        now: float,
    ) -> None:
        self.config = config
        est_data = (stored or {}).get("estimator")
        estimator = ZoneEstimator.from_dict(est_data, config.estimator)
        self.observer = ZoneObserver.create(estimator=estimator, config=config.observer)
        self.observer.estimator.config = config.estimator
        self.health = SensorHealth(config.safety)
        self.window = WindowDetector(config.window)
        self.controller = BurstController(config.controller)
        self.hvac_on = False
        self.target = 21.0
        self.burst: Burst | None = None
        self.last_burst_end: float | None = None
        self.last_burst_tail_until: float | None = None
        self.actuator_fault: tuple[str, str] | None = None
        self.window_contact_open: bool | None = None
        self.window_alert: str | None = None
        self.start_delay_s = 30.0
        self.shared_duty = 0.0
        self.mode_block_until = now + config.mode_change_block_s
        self.window_block_until = 0.0
        self.last_fault: str | None = None
        self.last_fault_at: float | None = None
        self.last_forced_stop = False
        self._last_decision: ControlDecision | None = None
        self._last_window_event: WindowEvent | None = None
        self._heat_acc_t = now
        # Current heat episode (physics stale check): start time, last heat
        # time, heat seconds, model snapshot, and whether the reading moved.
        self._episode: dict[str, Any] | None = None
        self._validations: list[float] = [
            float(v) for v in (stored or {}).get("validation_errors", []) if isinstance(v, (int, float))
        ][-config.validation_window :]
        self._pending_validation: list[PendingValidation] = []
        self._restored_t = estimator.t
        if estimator.t is not None:
            # Restored model: the dynamic states are stale, re-anchor them at
            # the first report but keep parameters and gains.
            estimator.t = None

    # ------------------------------------------------------------ inputs
    def on_temperature(self, t: float, value: float | None, reported_t: float | None = None) -> None:
        self._account_heat(t)
        was_stale = self._sensor_fault(t) is not None
        first = not self.observer.estimator.initialized
        changed = (
            value is not None
            and self.health.last_value is not None
            and abs(value - self.health.last_value) > 1e-6
        )
        self.health.on_report(t, value, reported_t)
        if value is None:
            self.observer.on_temperature(t, None)
            return
        self._apply_gates(t)
        if first:
            self.observer.on_temperature(t, value)
            if self._restored_t is not None:
                est = self.observer.estimator
                est.initialize(t, value, mass=True)
                est.inflate_uncertainty((t - self._restored_t) / SECONDS_PER_HOUR)
                self._restored_t = None
        elif was_stale and self.health.fault(t) is None:
            # The sensor came back: re-anchor instead of "learning" the jump.
            self.observer.resync(t, value)
        else:
            self.observer.on_temperature(t, value)
        if changed and self._episode is not None:
            self._episode["changed"] = True
        for pending in self._pending_validation:
            if t <= pending.until:
                pending.observed_max = (
                    value if pending.observed_max is None else max(pending.observed_max, value)
                )
        if self.config.window_detection and self.window_contact_open is None:
            event = self.window.evaluate(t, self.observer.history)
            if event is not None:
                self._on_window_event(t, event)

    def on_outdoor(self, t: float, value: float | None) -> None:
        self.observer.on_outdoor(t, value)

    def on_actuators(self, t: float, valve_fraction: float, boiler_on: bool) -> None:
        """Measured actuator state (valves open fraction, boiler firing)."""
        self._account_heat(t)
        self.observer.on_actuators(t, valve_fraction, boiler_on)

    def set_hvac(self, t: float, on: bool) -> None:
        if on != self.hvac_on:
            self.mode_block_until = t + self.config.mode_change_block_s
        self.hvac_on = on

    def set_target(self, target: float) -> None:
        self.target = target

    def set_window_contact(self, t: float, is_open: bool | None) -> None:
        """Contact sensor state (None = no sensor / unavailable)."""
        self.window_contact_open = is_open
        if not self.config.window_detection or is_open is None:
            return
        event = self.window.force_open(t, "contact") if is_open else self.window.force_close(t, "contact")
        if event is not None:
            self._on_window_event(t, event)

    def set_actuator_fault(self, fault: tuple[str, str] | None) -> None:
        self.actuator_fault = fault

    def set_start_conditions(self, start_delay_s: float, shared_duty: float) -> None:
        self.start_delay_s = max(0.0, start_delay_s)
        self.shared_duty = min(1.0, max(0.0, shared_duty))

    def dismiss_window(self, t: float) -> None:
        event = self.window.dismiss(t)
        self.window_alert = None
        if event is not None:
            self.window_block_until = t  # user says it was not a window

    def reset_model(self) -> None:
        self.observer.estimator.reset_model()
        self._validations.clear()
        self._pending_validation.clear()

    # ----------------------------------------------------------- helpers
    @property
    def estimator(self) -> ZoneEstimator:
        return self.observer.estimator

    def _account_heat(self, t: float) -> None:
        dt = t - self._heat_acc_t
        if dt <= 0:
            return
        u = self.observer.heat_input
        if u > 0:
            if self.burst is not None:
                self.burst.heat_elapsed_s += dt
            self.health.on_heat(dt / SECONDS_PER_HOUR * u)
        if u > 0:
            episode = self._episode
            if episode is None and self.estimator.initialized:
                est = self.estimator
                episode = self._episode = {
                    "start": t - dt,
                    "last_heat": t,
                    "heat_s": 0.0,
                    "state": est.zone_state(0.0),
                    "params": est.params,
                    "changed": False,
                }
            if episode is not None:
                episode["heat_s"] += dt * u
                episode["last_heat"] = t
        self._heat_acc_t = t

    def _evaluate_episode(self, t: float) -> None:
        """Compare a finished heat episode with what the model says it must do."""
        episode = self._episode
        if episode is None:
            return
        quiet = t - episode["last_heat"]
        long_enough = episode["heat_s"] >= EPISODE_MAX_HEAT_S
        if not long_enough and quiet < EPISODE_SETTLE_S:
            return
        if not long_enough and quiet < EPISODE_GAP_S:
            return
        self._episode = None
        start = episode["state"]
        neutral = ZoneState(
            temp=start.temp, tm=start.tm, q=start.q, d=0.0, base=start.base, t_out=start.t_out
        )
        horizon = episode["heat_s"] + 2 * 3600.0
        heated = trajectory(
            neutral,
            episode["params"],
            delay_s=0.0,
            burst_s=episode["heat_s"],
            horizon_s=horizon,
            overrun=self.overrun(),
        )
        free = trajectory(neutral, episode["params"], delay_s=0.0, burst_s=0.0, horizon_s=horizon)
        # Heating effect: the largest difference the heat made to the air,
        # independent of how fast the room loses heat.
        effect = max(h[1] - f[1] for h, f in zip(heated, free))
        if long_enough and t - episode["last_heat"] < EPISODE_GAP_S:
            # Still heating: continue with a fresh episode from now.
            est = self.estimator
            self._episode = {
                "start": t,
                "last_heat": t,
                "heat_s": 0.0,
                "state": est.zone_state(0.0),
                "params": episode["params"],
                "changed": False,
            }
        self.health.on_episode(effect, bool(episode["changed"]))

    def _sensor_fault(self, t: float) -> tuple[str, str] | None:
        return self.health.fault(t)

    def _fault(self, t: float) -> tuple[str, str] | None:
        faults = [f for f in (self._sensor_fault(t), self.actuator_fault) if f is not None]
        if not faults:
            return None
        faults.sort(key=lambda f: FAULT_PRIORITY.index(f[0]) if f[0] in FAULT_PRIORITY else 99)
        return faults[0]

    def _apply_gates(self, t: float) -> None:
        sensor_fault = self._sensor_fault(t)
        ok = (
            sensor_fault is None
            and not self.health.suspect
            and not self.window.is_open
            and t >= self.window_block_until
            and t >= self.mode_block_until
            and (self.actuator_fault is None or self.actuator_fault[0] == FAULT_VALVE_MISMATCH)
        )
        self.observer.learn_params = ok
        self.observer.learn_d = not self.window.is_open and sensor_fault is None and not self.health.suspect

    def _on_window_event(self, t: float, event: WindowEvent) -> None:
        if event.opened:
            if event.reason == "contact":
                self.window_alert = "Door/window sensor reported open"
            else:
                self.window_alert = f"Open window detected (drop {abs(event.slope_per_hour):.2f}°C/h)"
        else:
            self.window_alert = None
            self.window_block_until = t + self.config.window_data_block_s
        self._last_window_event = event

    def confidence(self) -> float:
        base = self.estimator.confidence()
        if len(self._validations) < 3:
            return round(min(base, 0.5), 3)
        # Consistency after the offset correction: spread around the median.
        center = statistics.median(self._validations)
        spread = statistics.median(abs(v - center) for v in self._validations)
        bias = max(0.0, abs(center) - 0.3)
        c_val = min(1.0, max(0.0, (0.3 - spread - bias) / 0.2))
        return round(min(base, c_val), 3)

    def peak_correction(self) -> float:
        """Median observed-minus-predicted burst peak, bounded (offset-free control)."""
        if len(self._validations) < 3:
            return 0.0
        median = statistics.median(self._validations)
        return min(0.3, max(-0.3, -median))

    def overrun(self) -> tuple[tuple[float, float], ...]:
        cfg = self.config.observer
        delay = self.config.turn_off_delay_s
        prof = [
            (offset, level)
            for offset, level in overrun_profile(0.0, cfg.overrun_tau_s, cfg.overrun_step_s)
            if offset < delay
        ]
        prof.append((min(delay, prof[-1][0] + cfg.overrun_step_s) if prof else 0.0, 0.0))
        return tuple(prof)

    def _model_ready(self) -> bool:
        return self.estimator.initialized

    def _finish_validations(self, t: float) -> None:
        done = [p for p in self._pending_validation if t > p.until]
        for pending in done:
            self._pending_validation.remove(pending)
            if pending.observed_max is None:
                continue
            self._validations.append(pending.predicted_peak - pending.observed_max)
            self._validations = self._validations[-self.config.validation_window :]

    def _stop_burst(self, t: float, *, forced: bool, decision: ControlDecision | None) -> None:
        burst = self.burst
        if burst is None:
            return
        self.burst = None
        self.last_forced_stop = forced
        if burst.heat_started_at is None:
            return
        self.last_burst_end = t
        tail = decision.tail_s if decision is not None and decision.tail_s else None
        if tail is None:
            tail = 2.0 * self.estimator.params.tau_e * SECONDS_PER_HOUR
        self.last_burst_tail_until = t + min(tail, 3600.0)
        if forced or burst.start_state is None or burst.start_params is None:
            return
        # Validate the model: what did it predict for the burst as actually run?
        pred = simulate_burst(
            burst.start_state,
            burst.start_params,
            delay_s=0.0,
            burst_s=burst.heat_elapsed_s,
            horizon_s=burst.heat_elapsed_s + 3 * 3600.0,
            overrun=self.overrun(),
        )
        start = burst.heat_started_at
        self._pending_validation.append(
            PendingValidation(
                started_at=start,
                predicted_peak=pred.peak,
                until=start + pred.peak_after_s + 1800.0,
                observed_max=self.observer.displayed,
            )
        )

    # ------------------------------------------------------------ update
    def update(self, t: float, *, control: bool = True) -> ZoneStatus:
        """Advance everything to ``t`` and decide.

        ``control=False`` (replay of recorded history) skips the controller:
        estimation, safety and window detection still run.
        """
        self._account_heat(t)
        self._evaluate_episode(t)
        self._apply_gates(t)
        self.observer.tick(t)
        self._finish_validations(t)
        window_event: WindowEvent | None = None
        if self.config.window_detection:
            if self.window.is_open and self.window_contact_open is None:
                event = self.window.evaluate(t, self.observer.history)
                if event is not None:
                    self._on_window_event(t, event)
            window_event = self._last_window_event
            self._last_window_event = None
        elif self.window.is_open:
            event = self.window.force_close(t, "disabled")
            if event is not None:
                self._on_window_event(t, event)
        fault = self._fault(t)
        if fault is not None and fault[0] != self.last_fault:
            self.last_fault = fault[0]
            self.last_fault_at = t
        heat_flowing = self.observer.heat_input > 0

        # Safety first: these stop any burst immediately.
        blocked_reason = None
        if not self.hvac_on:
            blocked_reason = "off"
        elif self.window.is_open:
            blocked_reason = "window open"
        elif fault is not None:
            blocked_reason = fault[0]
        if blocked_reason is not None and self.burst is not None:
            self._stop_burst(t, forced=True, decision=None)

        decision: ControlDecision | None = None
        reason = blocked_reason or ""
        if blocked_reason is None and control:
            burst = self.burst
            if burst is not None and burst.heat_started_at is None and heat_flowing:
                burst.heat_started_at = t
            if (
                burst is not None
                and burst.heat_started_at is None
                and t - burst.requested_at > self.config.start_timeout_s
            ):
                self._stop_burst(t, forced=True, decision=None)
                reason = "heat did not start"
            else:
                decision = self.controller.decide(self._control_input(t))
                reason = decision.reason
                if self.burst is not None:
                    if decision.stop:
                        self._stop_burst(t, forced=False, decision=decision)
                    elif decision.burst_s is not None:
                        self.burst.planned_s = decision.burst_s
                elif decision.start and decision.burst_s:
                    est = self.estimator
                    self.burst = Burst(
                        requested_at=t,
                        planned_s=decision.burst_s,
                        mode=decision.mode,
                        predicted_peak=decision.predicted_peak,
                        start_state=est.zone_state(self.shared_duty) if est.initialized else None,
                        start_params=est.params,
                    )
                    self.last_forced_stop = False
            self._last_decision = decision

        burst = self.burst
        next_wake = t + TICK_S
        if burst is not None and burst.heat_started_at is not None and heat_flowing:
            remaining = burst.planned_s - burst.heat_elapsed_s
            next_wake = min(next_wake, t + max(1.0, remaining))
        window_open = self.window.is_open
        history = self.observer.history
        return ZoneStatus(
            want_heat=burst is not None,
            forced_stop=self.last_forced_stop,
            heat_flowing=heat_flowing,
            burst_active=burst is not None,
            planned_burst_s=burst.planned_s if burst else None,
            heat_elapsed_s=burst.heat_elapsed_s if burst else 0.0,
            control_mode=self.controller.mode,
            confidence=self.confidence(),
            predicted_peak=decision.predicted_peak if decision else None,
            tail_s=decision.tail_s if decision else None,
            time_to_peak_s=decision.time_to_peak_s if decision else None,
            fault=fault[0] if fault else None,
            fault_message=fault[1] if fault else None,
            window_open=window_open,
            window_alert=self.window_alert if window_open else None,
            learning=self.observer.learn_params,
            next_wake=next_wake,
            reason=reason,
            slope_instant=history.slope_per_hour(t, 1800.0),
            slope_hourly=history.slope_per_hour(t, 3600.0),
            window_event=window_event,
        )

    def _control_input(self, t: float) -> ControlInput:
        est = self.estimator
        initialized = est.initialized
        burst = self.burst
        return ControlInput(
            now=t,
            target=self.target,
            deadband=self.config.deadband,
            reading=self.observer.displayed,
            state=est.zone_state(self.shared_duty) if initialized else None,
            params=est.params if initialized else None,
            confidence=self.confidence(),
            use_model=self.config.use_model,
            start_delay_s=self.start_delay_s,
            overrun=self.overrun(),
            last_burst_end=self.last_burst_end,
            last_burst_tail_until=self.last_burst_tail_until,
            heating=burst is not None,
            heat_elapsed_s=burst.heat_elapsed_s if burst else 0.0,
            planned_burst_s=burst.planned_s if burst else None,
            peak_correction=self.peak_correction(),
        )

    # ------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": CORE_STORAGE_FORMAT,
            "estimator": self.estimator.to_dict(),
            "validation_errors": [round(v, 4) for v in self._validations],
        }
