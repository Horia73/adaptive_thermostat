"""Burst controller: when to heat and for how long.

Hydronic radiators keep heating the room after the valve closes (the "tail"),
so a thermostat that simply waits for the target overshoots.  This controller
plans bursts by forward simulation of the zone model (receding horizon):

*Idle* - simulate "fire now" and "fire in ``lookahead``".  If waiting would
let the room drop below ``target - deadband`` (or it already is), fire now.
The burst length is the shortest one whose predicted peak, including the
residual tail and pump overrun, reaches the target.

*Heating* - re-plan every tick: once stopping now is predicted to bring the
peak to the target, stop (respecting the minimum burst).  The burst is never
extended past the maximum, and a burst that is clearly not enough (room
colder than predicted) may run up to it.

*Between bursts* - a new burst waits for the minimum off time and for the
predicted tail of the previous one, so the controller never "stacks" bursts
on a tail it has not seen yet (unless the room is far below target).

While the model is not trusted (fresh install, after a reset, inconsistent
predictions) a conservative fallback is used instead: fire when the reading
is at or below ``target - deadband``, a burst length proportional to the
error, a long lock-out after each burst to let the tail show up, and an early
stop if the reading reaches ``target + deadband``.  Like a relay auto-tuner
it scales its bursts per room: longer after a burst that made no progress,
shorter after one that overshot.  It is deliberately simple and well behaved,
and it is what the model learns from.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import BurstPrediction, ThermalParams, ZoneState, simulate_burst

MODE_MODEL = "model"
MODE_FALLBACK = "fallback"


@dataclass
class ControllerConfig:
    """Timing limits (seconds) and tuning."""

    min_burst_s: float = 120.0
    max_burst_s: float = 45 * 60.0
    min_off_s: float = 300.0
    lookahead_s: float = 600.0
    horizon_s: float = 4 * 3600.0
    aim_offset: float = 0.0  # K added to the target the peak should reach
    emergency_below: float = 0.6  # K below target that overrides the tail wait
    max_lower_band: float = 0.25  # K, lowest trigger below target when centring
    model_confidence_on: float = 0.6
    model_confidence_off: float = 0.4
    # Fallback controller
    fallback_base_s: float = 180.0
    fallback_per_kelvin_s: float = 600.0
    fallback_max_s: float = 25 * 60.0
    fallback_boost_error: float = 1.0  # K below target: allow bursts up to max_burst_s
    fallback_lockout_s: float = 35 * 60.0
    fallback_cold_lockout_s: float = 10 * 60.0
    fallback_cold_error: float = 0.5
    fallback_gain_bounds: tuple[float, float] = (0.4, 3.0)
    fallback_gain_up: float = 1.4
    fallback_gain_down: float = 0.7
    fallback_progress: float = 0.33  # share of the gap a burst should close


@dataclass
class ControlInput:
    """Everything the controller needs for one decision."""

    now: float
    target: float
    deadband: float
    reading: float | None  # last displayed sensor value
    state: ZoneState | None  # model state (None when not initialised)
    params: ThermalParams | None
    confidence: float
    use_model: bool  # option: allow model-based control
    start_delay_s: float  # expected wait until heat actually flows
    overrun: tuple[tuple[float, float], ...]
    last_burst_end: float | None
    last_burst_tail_until: float | None  # predicted peak time of the previous burst
    heating: bool  # a burst is active
    heat_elapsed_s: float  # heat-on time of the active burst
    planned_burst_s: float | None
    # Offset-free correction: observed minus predicted burst peaks (median of
    # recent bursts), added to every predicted burst peak.
    peak_correction: float = 0.0


@dataclass
class ControlDecision:
    """Result of one decision."""

    mode: str
    start: bool = False
    stop: bool = False
    burst_s: float | None = None
    predicted_peak: float | None = None
    tail_s: float | None = None
    time_to_peak_s: float | None = None
    reason: str = ""


class BurstController:
    """Stateless apart from the model/fallback mode hysteresis."""

    def __init__(self, config: ControllerConfig | None = None) -> None:
        self.config = config or ControllerConfig()
        self.mode = MODE_FALLBACK
        # Fallback self-tuning: burst scale, last fire (time, reading) and
        # the highest reading seen since.
        self.fallback_gain = 1.0
        self._fb_fire: tuple[float, float] | None = None
        self._fb_max: float | None = None

    # ------------------------------------------------------------- helpers
    def _update_mode(self, inp: ControlInput) -> str:
        cfg = self.config
        if not inp.use_model or inp.state is None or inp.params is None:
            self.mode = MODE_FALLBACK
        elif self.mode == MODE_FALLBACK and inp.confidence >= cfg.model_confidence_on:
            self.mode = MODE_MODEL
        elif self.mode == MODE_MODEL and inp.confidence < cfg.model_confidence_off:
            self.mode = MODE_FALLBACK
        return self.mode

    def predict(self, inp: ControlInput, *, delay_s: float, burst_s: float) -> BurstPrediction:
        assert inp.state is not None and inp.params is not None
        pred = simulate_burst(
            inp.state,
            inp.params,
            delay_s=delay_s,
            burst_s=burst_s,
            horizon_s=self.config.horizon_s,
            overrun=inp.overrun if burst_s > 0 else (),
        )
        if burst_s > 0 and inp.peak_correction:
            pred = BurstPrediction(
                peak=pred.peak + inp.peak_correction,
                peak_after_s=pred.peak_after_s,
                minimum=pred.minimum,
                end_temp=pred.end_temp,
                tail_s=pred.tail_s,
            )
        return pred

    def burst_for_peak(
        self, inp: ControlInput, *, delay_s: float, aim: float, already_s: float = 0.0
    ) -> tuple[float, BurstPrediction]:
        """Shortest burst (>= min) whose predicted peak reaches ``aim``.

        ``already_s`` is heat already delivered in the active burst; the
        returned length is the *remaining* heat time.
        """
        cfg = self.config
        lo = max(0.0, cfg.min_burst_s - already_s)
        hi = max(lo, cfg.max_burst_s - already_s)
        pred_lo = self.predict(inp, delay_s=delay_s, burst_s=lo)
        if pred_lo.peak >= aim or hi <= lo:
            return lo, pred_lo
        pred_hi = self.predict(inp, delay_s=delay_s, burst_s=hi)
        if pred_hi.peak < aim:
            return hi, pred_hi
        best = pred_hi
        for _ in range(14):
            mid = 0.5 * (lo + hi)
            pred = self.predict(inp, delay_s=delay_s, burst_s=mid)
            if pred.peak >= aim:
                hi, best = mid, pred
            else:
                lo = mid
            if hi - lo < 15.0:
                break
        return hi, best

    # ------------------------------------------------------------- decide
    def decide(self, inp: ControlInput) -> ControlDecision:
        mode = self._update_mode(inp)
        if mode == MODE_MODEL:
            return self._decide_model(inp)
        return self._decide_fallback(inp)

    def _decide_model(self, inp: ControlInput) -> ControlDecision:
        cfg = self.config
        assert inp.state is not None
        aim = inp.target + cfg.aim_offset
        temp = inp.state.temp
        if inp.heating:
            elapsed = inp.heat_elapsed_s
            stop_now = self.predict(inp, delay_s=0.0, burst_s=1e-3)
            decision = ControlDecision(
                mode=MODE_MODEL,
                predicted_peak=stop_now.peak,
                tail_s=stop_now.tail_s,
                time_to_peak_s=stop_now.peak_after_s,
            )
            if elapsed >= cfg.max_burst_s:
                decision.stop = True
                decision.reason = "max burst"
                return decision
            if elapsed < cfg.min_burst_s:
                decision.reason = "min burst"
                return decision
            if stop_now.peak >= aim:
                decision.stop = True
                decision.reason = "predicted peak reaches target"
                return decision
            remaining, pred = self.burst_for_peak(inp, delay_s=0.0, aim=aim, already_s=elapsed)
            decision.burst_s = elapsed + remaining
            decision.predicted_peak = pred.peak
            decision.tail_s = pred.tail_s
            decision.time_to_peak_s = pred.peak_after_s
            decision.reason = "continue"
            return decision

        # Idle.
        free = self.predict(inp, delay_s=0.0, burst_s=0.0)
        decision = ControlDecision(
            mode=MODE_MODEL, predicted_peak=free.peak, time_to_peak_s=free.peak_after_s
        )
        if temp >= inp.target + inp.deadband:
            decision.reason = "above band"
            return decision
        if inp.last_burst_end is not None and inp.now - inp.last_burst_end < cfg.min_off_s:
            decision.reason = "min off"
            return decision
        emergency = temp < inp.target - cfg.emergency_below
        if not emergency and inp.last_burst_tail_until is not None and inp.now < inp.last_burst_tail_until:
            decision.reason = "waiting for tail"
            return decision
        delay = inp.start_delay_s
        # The shortest allowed burst lifts the room by some amount; centre the
        # oscillation on the target by firing half that rise below it.
        smallest = self.predict(inp, delay_s=delay, burst_s=cfg.min_burst_s)
        rise = max(0.0, smallest.peak - free.peak)
        low = inp.target - max(inp.deadband, min(cfg.max_lower_band, 0.5 * rise))
        _later_len, later = self.burst_for_peak(inp, delay_s=delay + cfg.lookahead_s, aim=aim)
        if later.minimum >= low and temp >= low:
            decision.reason = "not needed yet"
            return decision
        burst, pred = self.burst_for_peak(inp, delay_s=delay, aim=aim)
        decision.start = True
        decision.burst_s = burst
        decision.predicted_peak = pred.peak
        decision.tail_s = pred.tail_s
        decision.time_to_peak_s = pred.peak_after_s
        decision.reason = "fire"
        return decision

    def _decide_fallback(self, inp: ControlInput) -> ControlDecision:
        cfg = self.config
        decision = ControlDecision(mode=MODE_FALLBACK)
        reading = inp.reading
        if reading is not None and self._fb_max is not None:
            self._fb_max = max(self._fb_max, reading)
        if reading is None:
            decision.reason = "no reading"
            if inp.heating:
                decision.stop = True
            return decision
        if inp.heating:
            planned = inp.planned_burst_s or cfg.min_burst_s
            if inp.heat_elapsed_s >= planned:
                decision.stop = True
                decision.reason = "planned length reached"
            elif inp.heat_elapsed_s >= cfg.min_burst_s and reading >= inp.target + inp.deadband:
                decision.stop = True
                decision.reason = "reading above band"
            else:
                decision.reason = "continue"
            decision.burst_s = planned
            return decision
        error = inp.target - reading
        if reading > inp.target - inp.deadband + 1e-6:
            decision.reason = "within band"
            return decision
        if inp.last_burst_end is not None:
            since = inp.now - inp.last_burst_end
            lockout = (
                cfg.fallback_cold_lockout_s if error >= cfg.fallback_cold_error else cfg.fallback_lockout_s
            )
            if since < max(cfg.min_off_s, lockout):
                decision.reason = "lock-out after burst"
                return decision
        self._tune_fallback(inp, reading, error)
        burst = cfg.fallback_base_s + cfg.fallback_per_kelvin_s * max(0.0, error - inp.deadband)
        burst *= self.fallback_gain
        cap = cfg.max_burst_s if error >= cfg.fallback_boost_error else cfg.fallback_max_s
        burst = min(cap, max(cfg.min_burst_s, burst))
        self._fb_fire = (inp.now, reading)
        self._fb_max = reading
        decision.start = True
        decision.burst_s = burst
        decision.reason = "fire"
        return decision

    def _tune_fallback(self, inp: ControlInput, reading: float, error: float) -> None:
        """Adapt the fallback burst scale from the outcome of the previous burst."""
        cfg = self.config
        fire = self._fb_fire
        if fire is None or inp.now - fire[0] > 3 * 3600.0:
            return
        low, high = cfg.fallback_gain_bounds
        if self._fb_max is not None and self._fb_max >= inp.target + inp.deadband + 0.05:
            self.fallback_gain = max(low, self.fallback_gain * cfg.fallback_gain_down)
        elif error >= 0.3 and reading - fire[1] < cfg.fallback_progress * (inp.target - fire[1]):
            # The last burst closed too little of the gap: make bursts longer.
            self.fallback_gain = min(high, self.fallback_gain * cfg.fallback_gain_up)
