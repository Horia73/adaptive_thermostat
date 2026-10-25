"""Feed measured signals into the zone estimator.

The observer turns raw, irregular Home Assistant signals into estimator
inputs:

* own heat input ``u`` = open fraction of the zone's valves while the boiler
  fires, followed by a decaying pump-overrun contribution while the valves
  stay open after the burner stops;
* shared heat input ``b`` = boiler firing while this zone's valves are closed
  (heat from distribution pipes and heated neighbours, learned per zone);
* both from the *measured* actuator states, applied after a transport delay;
* outdoor temperature through a time-based filter;
* temperature reports as measurements (threshold crossings corrected to the
  bin edge), and silence as a band constraint.

The caller (``ZoneCore`` or the replay harness) decides when learning is
allowed by setting :attr:`ZoneObserver.learn_params` / :attr:`learn_d`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .estimator import EstimatorConfig, ZoneEstimator
from .filters import OutdoorFilter, ReportHistory


@dataclass
class ObserverConfig:
    """Measurement handling settings."""

    transport_delay_s: float = 60.0
    # After the burner stops, the pump keeps circulating hot water through any
    # open valve; the supply temperature excess decays roughly exponentially.
    overrun_tau_s: float = 90.0
    overrun_step_s: float = 30.0
    # A report that changes the reading by >= 0.15 K is a threshold crossing:
    # the temperature has only just entered the new 0.1 K bin, i.e. it sits
    # about half a step on the near side of the new value.  Single 0.1 K
    # steps may be crossings or periodic samples, so only a quarter step.
    crossing_offset_large: float = 0.05
    crossing_offset_small: float = 0.025
    silence_half_width: float = 0.15  # K, band a silent sensor is assumed to stay in
    silence_check_s: float = 600.0
    outdoor_tau_s: float = 1200.0
    default_outdoor: float = 10.0
    robust_quiet_s: float = 2 * 3600.0  # wide innovation gate this long after heat


def overrun_profile(since_off_s: float, tau_s: float, step_s: float) -> list[tuple[float, float]]:
    """Piecewise-constant pump-overrun level after the boiler stopped.

    Returns ``[(offset_from_now_s, level)]`` starting at offset 0 and ending
    with level 0.  ``since_off_s`` is how long ago the boiler stopped.
    """
    if tau_s <= 0:
        return [(0.0, 0.0)]
    out: list[tuple[float, float]] = []
    end = 4.0 * tau_s
    k = int(max(0.0, since_off_s) // step_s)
    offset = 0.0
    while k * step_s < end:
        mid = (k + 0.5) * step_s
        out.append((offset, math.exp(-mid / tau_s)))
        offset = (k + 1) * step_s - since_off_s
        k += 1
    out.append((max(0.0, offset), 0.0))
    return out


def input_schedule(
    t: float,
    valve_fraction: float,
    boiler_on: bool,
    boiler_off_at: float | None,
    config: ObserverConfig,
) -> list[tuple[float, float, float]]:
    """Effective ``(t_eff, u, b)`` inputs from the actuator state at ``t`` onwards."""
    delay = config.transport_delay_s
    if boiler_on:
        return [(t + delay, valve_fraction, 1.0 - valve_fraction)]
    since = 0.0 if boiler_off_at is None else max(0.0, t - boiler_off_at)
    return [
        (t + delay + offset, valve_fraction * level, 0.0)
        for offset, level in overrun_profile(since, config.overrun_tau_s, config.overrun_step_s)
    ]


@dataclass
class ZoneObserver:
    """Estimator plus signal conditioning for one zone."""

    config: ObserverConfig = field(default_factory=ObserverConfig)
    estimator: ZoneEstimator = field(default_factory=ZoneEstimator)
    outdoor: OutdoorFilter = field(default_factory=OutdoorFilter)
    history: ReportHistory = field(default_factory=ReportHistory)
    learn_params: bool = True
    learn_d: bool = True
    displayed: float | None = None
    displayed_since: float | None = None
    last_report_t: float | None = None
    sensor_available: bool = False
    heat_input: float = 0.0  # own heat while the boiler fires (no overrun)
    valve_fraction: float = 0.0
    boiler_on: bool = False
    boiler_off_at: float | None = None
    last_heat_t: float | None = None
    _last_silence_check: float | None = None
    _actuators_known: bool = False

    def __post_init__(self) -> None:
        self.outdoor.tau_s = self.config.outdoor_tau_s

    @classmethod
    def create(
        cls,
        estimator: ZoneEstimator | None = None,
        config: ObserverConfig | None = None,
        estimator_config: EstimatorConfig | None = None,
    ) -> ZoneObserver:
        est = estimator or ZoneEstimator(config=estimator_config or EstimatorConfig())
        return cls(config=config or ObserverConfig(), estimator=est)

    # ------------------------------------------------------------- inputs
    def on_outdoor(self, t: float, value: float | None) -> None:
        self.estimator.advance(t, learn_params=self.learn_params)
        filtered = self.outdoor.update(t, value)
        if filtered is not None:
            self.estimator.set_outdoor(filtered)

    @property
    def outdoor_temperature(self) -> float:
        value = self.outdoor.value
        return self.config.default_outdoor if value is None else value

    def on_actuators(self, t: float, valve_fraction: float, boiler_on: bool) -> None:
        """Record measured actuator states (valve open fraction, boiler firing)."""
        valve_fraction = min(1.0, max(0.0, valve_fraction))
        if boiler_on and not self.boiler_on:
            self.boiler_off_at = None
        elif not boiler_on and self.boiler_on:
            self.boiler_off_at = t
        if (
            self._actuators_known
            and abs(valve_fraction - self.valve_fraction) < 1e-9
            and boiler_on == self.boiler_on
        ):
            return
        self._actuators_known = True
        self.valve_fraction = valve_fraction
        self.boiler_on = boiler_on
        self.estimator.advance(t, learn_params=self.learn_params)
        items = input_schedule(t, valve_fraction, boiler_on, self.boiler_off_at, self.config)
        self.estimator.replace_future_inputs(t + self.config.transport_delay_s, items)
        self.heat_input = valve_fraction if boiler_on else 0.0
        if self.heat_input > 0:
            self.last_heat_t = t

    def recently_heated(self, t: float) -> bool:
        if self.heat_input > 0:
            return True
        return self.last_heat_t is not None and t - self.last_heat_t < self.config.robust_quiet_s

    def on_temperature(self, t: float, value: float | None) -> float | None:
        """Process a temperature report; returns the innovation if any."""
        if value is None:
            self.sensor_available = False
            return None
        self.sensor_available = True
        self.last_report_t = t
        est = self.estimator
        if not est.initialized:
            est.initialize(t, value)
            est.set_outdoor(self.outdoor_temperature)
            self._set_displayed(t, value)
            return None
        changed = self.displayed is None or abs(value - self.displayed) > 1e-6
        if not changed:
            # A repeated identical report (heartbeat): the reading is still
            # valid, i.e. the temperature is within half a quantisation step.
            est.apply_silence_bound(t, value, 0.05, learn_params=self.learn_params, learn_d=self.learn_d)
            self._last_silence_check = t
            return None
        z = value
        if self.displayed is not None:
            delta = value - self.displayed
            offset = (
                self.config.crossing_offset_large if abs(delta) >= 0.15 else self.config.crossing_offset_small
            )
            z = value - math.copysign(offset, delta)
        innov = est.update_report(
            t,
            z,
            learn_params=self.learn_params,
            learn_d=self.learn_d,
            robust=not self.recently_heated(t),
        )
        self._set_displayed(t, value)
        return innov

    def _set_displayed(self, t: float, value: float) -> None:
        if self.displayed is None or abs(value - self.displayed) > 1e-6:
            self.displayed = value
            self.displayed_since = t
            self.history.add(t, value)
        self._last_silence_check = t

    def tick(self, t: float) -> None:
        """Advance the model and apply the silence constraint when due."""
        est = self.estimator
        if not est.initialized:
            return
        self.outdoor.advance(t)
        if self.outdoor.value is not None:
            est.set_outdoor(self.outdoor.value)
        if (
            self.sensor_available
            and self.displayed is not None
            and self._last_silence_check is not None
            and t - self._last_silence_check >= self.config.silence_check_s
        ):
            est.apply_silence_bound(
                t,
                self.displayed,
                self.config.silence_half_width,
                learn_params=self.learn_params,
                learn_d=self.learn_d,
            )
            self._last_silence_check = t
        else:
            est.advance(t, learn_params=self.learn_params)

    def resync(self, t: float, value: float) -> None:
        """Re-anchor the temperature state (after a stale sensor recovers)."""
        self.estimator.initialize(t, value)
        self.history.clear()
        self.displayed = None
        self._set_displayed(t, value)


def effective_input_timeline(
    actuator_events: list[tuple[float, float, bool]], config: ObserverConfig
) -> list[tuple[float, float, float]]:
    """Effective ``(t, u, b)`` change points from ``[(t, valve_fraction, boiler_on)]``.

    Mirrors :meth:`ZoneObserver.on_actuators` for offline forecasting.
    """
    out: list[tuple[float, float, float]] = []
    pending: list[tuple[float, float, float]] = []
    boiler_prev = False
    off_at: float | None = None
    for t, frac, boiler in actuator_events:
        if boiler and not boiler_prev:
            off_at = None
        elif not boiler and boiler_prev:
            off_at = t
        boiler_prev = boiler
        cut = t + config.transport_delay_s
        out.extend(p for p in pending if p[0] < cut)
        pending = input_schedule(t, frac, boiler, off_at, config)
    out.extend(pending)
    merged: list[tuple[float, float, float]] = []
    for item in sorted(out, key=lambda it: it[0]):
        if merged and abs(merged[-1][1] - item[1]) < 1e-9 and abs(merged[-1][2] - item[2]) < 1e-9:
            continue
        merged.append(item)
    return merged
