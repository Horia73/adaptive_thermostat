"""Online estimation of the zone model (joint extended Kalman filter).

State vector (12)::

    x = [T, T_m, q, d, g, k, ln q_s, ln tau_f, ln tau_e, ln tau_r, ln tau_am, ln rho]

``T`` air temperature (°C), ``T_m`` building-mass temperature, ``q`` emitter
flux (K/h), ``d`` transient
disturbance (K/h, relaxes to zero), ``g`` base gain (K/h), ``k`` shared heat
gain (K/h while the boiler fires for *other* zones: distribution pipes,
heated neighbours) and the logarithms of the physical parameters of
:mod:`.model`.  Log space keeps the parameters positive and makes relative
uncertainty additive.

Inputs: ``u`` (own heat: open valve fraction while the boiler fires, plus
pump overrun) and ``b`` (boiler firing while this zone's valves are closed).

How it copes with the data
--------------------------
* The transition is the exact discrete solution of the linear model for
  piecewise-constant inputs, so irregular sampling (reports every few minutes
  to every few hours) needs no special handling: the filter is advanced to each
  event time, splitting at input changes.
* Reports are 0.1 °C-quantised measurements (``sigma = 0.05 °C``); the observer
  corrects threshold-crossing reports to the bin edge.
* Silence carries information: :meth:`ZoneEstimator.apply_silence_bound`
  projects the estimate back into the band a silent sensor must be in, with an
  inequality pseudo-measurement applied only when the estimate leaves the band,
  so correlated silence is never over-counted.
* Robustness: Huber weighting of large innovations (a narrow gate when the
  zone has not been heated recently, a wide one right after heating, where
  large innovations are informative); a trust region on every parameter step;
  a floor on parameter variance so one disturbed episode cannot lock the
  filter onto wrong values; clamping to physical bounds; re-anchoring the
  temperature after a run of outliers.
* Learning gates: the caller decides when parameters (and the disturbance)
  may learn.  During window events, faults, stale sensors and right after mode
  changes the learned rows of the Kalman gain are zeroed, so the filter still
  tracks the temperature but the model is not poisoned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math
from typing import Any

from .model import (
    DEFAULT_PARAMS,
    DIST_TAU_H,
    PARAM_BOUNDS,
    PARAM_NAMES,
    SECONDS_PER_HOUR,
    ThermalParams,
    ZoneState,
    step,
)

N = 12
IT, IM, IQ, ID, IG, IK, IQS, ITF, ITE, ITR, IAM, IRHO = range(N)
PARAM_INDEXES = (IQS, ITF, ITE, ITR, IAM, IRHO)
LEARNED_INDEXES = (IG, IK, *PARAM_INDEXES)
FIRST_PARAM = IQS

STORAGE_FORMAT = 4


@dataclass
class EstimatorConfig:
    """Noise model and priors (time in hours)."""

    sigma_meas: float = 0.05  # K, report measurement noise
    sigma_bound: float = 0.03  # K, silence-bound pseudo-measurement noise
    process_t: float = 0.04  # K / sqrt(h)
    process_q: float = 0.25  # K/h / sqrt(h)
    dist_sd: float = 0.3  # K/h, stationary spread of the transient disturbance
    dist_tau_h: float = DIST_TAU_H
    process_g: float = 0.01  # K/h / sqrt(h), drift of the base gain
    process_k: float = 0.01  # K/h / sqrt(h), drift of the shared heat gain
    process_params: tuple[float, ...] = (0.015, 0.01, 0.01, 0.01, 0.01, 0.01)
    prior_sd_t: float = 0.1
    prior_sd_tm: float = 0.3
    prior_sd_q: float = 0.3
    prior_g: float = 0.0
    prior_sd_g: float = 0.3
    prior_k: float = 0.1
    prior_sd_k: float = 0.2
    prior_sd_params: tuple[float, ...] = (0.7, 0.6, 0.6, 0.8, 0.7, 0.7)
    gate: float = 3.0  # innovation gate (standard deviations) for Huber weighting
    gate_heating: float = 4.5  # wider gate right after heating (informative data)
    reset_after_outliers: int = 4
    # Lower bound on the log-parameter spread while learning: keeps the filter
    # adaptive (no covariance collapse / lock-in after a bad episode).
    param_sd_floor: tuple[float, ...] = (0.15, 0.2, 0.15, 0.15, 0.2, 0.2)
    # Trust region: largest change of a log-parameter / gain per measurement.
    max_param_step: float = 0.05
    max_gain_step: float = 0.02
    max_substep_h: float = 0.5
    d_bounds: tuple[float, float] = (-3.0, 3.0)
    g_bounds: tuple[float, float] = (-1.0, 1.0)
    k_bounds: tuple[float, float] = (0.0, 1.5)
    reference_burst_s: float = 600.0  # burst used to express model confidence


def _zeros(n: int = N) -> list[list[float]]:
    return [[0.0] * n for _ in range(n)]


def _matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    n, m, p = len(a), len(b), len(b[0])
    out = [[0.0] * p for _ in range(n)]
    for i in range(n):
        ai = a[i]
        oi = out[i]
        for k in range(m):
            aik = ai[k]
            if aik == 0.0:
                continue
            bk = b[k]
            for j in range(p):
                oi[j] += aik * bk[j]
    return out


def _transpose(a: Sequence[Sequence[float]]) -> list[list[float]]:
    return [list(col) for col in zip(*a)]


def _symmetrize(p: list[list[float]]) -> None:
    for i in range(N):
        for j in range(i + 1, N):
            v = 0.5 * (p[i][j] + p[j][i])
            p[i][j] = v
            p[j][i] = v
        p[i][i] = max(p[i][i], 1e-12)


def params_from_logs(logs: Sequence[float]) -> ThermalParams:
    return ThermalParams(*(math.exp(v) for v in logs))


@dataclass
class EstimatorStats:
    """Bookkeeping used for confidence and diagnostics."""

    reports: int = 0
    learned_reports: int = 0
    heat_hours_learned: float = 0.0
    bursts_learned: int = 0
    nis_ewma: float = 1.0
    outliers_in_row: int = 0
    resets: int = 0
    last_innovation: float = 0.0


@dataclass
class ZoneEstimator:
    """Joint state/parameter EKF for one zone."""

    config: EstimatorConfig = field(default_factory=EstimatorConfig)
    x: list[float] = field(default_factory=list)
    p: list[list[float]] = field(default_factory=list)
    t: float | None = None  # epoch seconds of the state
    u: float = 0.0  # current own heat input (0..1)
    b: float = 0.0  # current shared heat input (0..1)
    t_out: float = 10.0  # current (filtered) outdoor temperature
    stats: EstimatorStats = field(default_factory=EstimatorStats)
    _pending_inputs: list[tuple[float, float, float]] = field(default_factory=list)
    _heating_in_burst: bool = False

    def __post_init__(self) -> None:
        if not self.x:
            self.reset_model(DEFAULT_PARAMS)

    # ------------------------------------------------------------------ setup
    def reset_model(self, params: ThermalParams = DEFAULT_PARAMS) -> None:
        """Reset to the prior (keeps the time base and temperature if known)."""
        cfg = self.config
        temp = self.x[IT] if self.x else 20.0
        self.x = [
            temp,
            temp,
            0.0,
            0.0,
            cfg.prior_g,
            cfg.prior_k,
            *(math.log(getattr(params, name)) for name in PARAM_NAMES),
        ]
        self.p = _zeros()
        sds = (
            cfg.prior_sd_t,
            cfg.prior_sd_tm,
            cfg.prior_sd_q,
            cfg.dist_sd,
            cfg.prior_sd_g,
            cfg.prior_sd_k,
            *cfg.prior_sd_params,
        )
        for i, sd in enumerate(sds):
            self.p[i][i] = sd * sd
        self.stats = EstimatorStats()

    @property
    def initialized(self) -> bool:
        return self.t is not None

    def initialize(self, t: float, temp: float, *, q: float | None = None, mass: bool = False) -> None:
        """Anchor the temperature state to a measurement at time ``t``.

        ``mass`` also resets the building-mass state to the same temperature
        (first start, or after the sensor was unusable for a long time).
        """
        first = self.t is None
        if self.t is None or t > self.t:
            self.t = t
        shift = temp - self.x[IT]
        self.x[IT] = temp
        if first or mass:
            self.x[IM] = temp
            for j in range(N):
                self.p[IM][j] = 0.0
                self.p[j][IM] = 0.0
            self.p[IM][IM] = self.config.prior_sd_tm**2
        elif abs(shift) > 0.5:
            # A big re-anchoring means the mass estimate is doubtful too.
            self.x[IM] += 0.5 * shift
        if q is not None:
            self.x[IQ] = q
        # Decorrelate the temperature state from everything else.
        for j in range(N):
            self.p[IT][j] = 0.0
            self.p[j][IT] = 0.0
        self.p[IT][IT] = self.config.prior_sd_t**2

    # ------------------------------------------------------------ accessors
    @property
    def params(self) -> ThermalParams:
        return params_from_logs([self.x[i] for i in PARAM_INDEXES])

    @property
    def temperature(self) -> float:
        return self.x[IT]

    @property
    def flux(self) -> float:
        return self.x[IQ]

    @property
    def disturbance(self) -> float:
        return self.x[ID]

    @property
    def base_gain(self) -> float:
        return self.x[IG]

    @property
    def shared_gain(self) -> float:
        return self.x[IK]

    def zone_state(self, shared_duty: float | None = None) -> ZoneState:
        """Snapshot for prediction/control.

        ``shared_duty`` is the expected fraction of time the boiler fires for
        other zones over the prediction horizon (defaults to the current input).
        """
        duty = self.b if shared_duty is None else shared_duty
        return ZoneState(
            temp=self.x[IT],
            tm=self.x[IM],
            q=self.x[IQ],
            d=self.x[ID],
            base=self.x[IG] + self.x[IK] * duty,
            t_out=self.t_out,
        )

    def param_sd(self) -> dict[str, float]:
        """Return the posterior standard deviation of each log-parameter."""
        return {name: math.sqrt(max(0.0, self.p[idx][idx])) for name, idx in zip(PARAM_NAMES, PARAM_INDEXES)}

    def rate_per_hour(self) -> float:
        """Return the model's current dT/dt estimate (K/h)."""
        prm = self.params
        x = self.x
        return (
            x[IQ]
            - (x[IT] - self.t_out) / prm.tau_r
            - (x[IT] - x[IM]) / prm.tau_am
            + x[IG]
            + x[IK] * self.b
            + x[ID]
        )

    # ------------------------------------------------------------ dynamics
    def _transition(self, x: Sequence[float], u: float, b: float, h: float) -> list[float]:
        prm = params_from_logs([x[i] for i in PARAM_INDEXES])
        temp, tm, q, d = step(
            x[IT],
            x[IM],
            x[IQ],
            x[ID],
            u,
            self.t_out,
            prm,
            h,
            base=x[IG] + x[IK] * b,
            tau_d=self.config.dist_tau_h,
        )
        out = list(x)
        out[IT] = temp
        out[IM] = tm
        out[IQ] = q
        out[ID] = d
        return out

    def _predict_interval(self, h: float, learn_params: bool) -> None:
        """Propagate mean and covariance over ``h`` hours with constant input."""
        if h <= 0.0:
            return
        cfg = self.config
        x0 = self.x
        fx = self._transition(x0, self.u, self.b, h)
        # Finite-difference Jacobian; only T, T_m, q and d depend on other states.
        jac = _zeros()
        for i in range(N):
            jac[i][i] = 1.0
        for j in range(N):
            eps = 1e-4 if j >= FIRST_PARAM else 1e-4 * max(1.0, abs(x0[j]))
            xp = list(x0)
            xp[j] += eps
            fp = self._transition(xp, self.u, self.b, h)
            for i in (IT, IM, IQ, ID):
                jac[i][j] = (fp[i] - fx[i]) / eps
        p_new = _matmul(_matmul(jac, self.p), _transpose(jac))
        # Exact OU discretisation keeps the stationary spread at dist_sd.
        decay = math.exp(-2.0 * h / cfg.dist_tau_h)
        q_diag = [
            cfg.process_t**2 * h,
            (0.1 * cfg.process_t) ** 2 * h,
            cfg.process_q**2 * h * (1.0 + 4.0 * self.u),
            cfg.dist_sd**2 * (1.0 - decay),
            cfg.process_g**2 * h if learn_params else 0.0,
            cfg.process_k**2 * h if learn_params else 0.0,
        ]
        q_diag.extend((s * s * h if learn_params else 0.0) for s in cfg.process_params)
        for i in range(N):
            p_new[i][i] += q_diag[i]
        if learn_params:
            for idx, floor in zip(PARAM_INDEXES, cfg.param_sd_floor):
                p_new[idx][idx] = max(p_new[idx][idx], floor * floor)
        _symmetrize(p_new)
        self.x = fx
        self.p = p_new
        if self.u > 0.0 and learn_params:
            self.stats.heat_hours_learned += h * self.u
        self._clamp()

    def schedule_input(self, t_effective: float, u: float, b: float = 0.0) -> None:
        """Change the inputs at ``t_effective`` (supports transport delay)."""
        self._pending_inputs.append((t_effective, _unit(u), _unit(b)))
        self._pending_inputs.sort(key=lambda item: item[0])

    def replace_future_inputs(self, t_from: float, items: Sequence[tuple[float, float, float]]) -> None:
        """Drop scheduled inputs at or after ``t_from`` and schedule ``items``."""
        self._pending_inputs = [p for p in self._pending_inputs if p[0] < t_from]
        for t_eff, u, b in items:
            self._pending_inputs.append((t_eff, _unit(u), _unit(b)))
        self._pending_inputs.sort(key=lambda item: item[0])

    def set_outdoor(self, t_out: float) -> None:
        self.t_out = t_out

    def advance(self, t: float, *, learn_params: bool = True) -> None:
        """Advance the state to epoch time ``t`` (seconds)."""
        if self.t is None:
            return
        if t <= self.t:
            while self._pending_inputs and self._pending_inputs[0][0] <= self.t:
                self._set_inputs(self._pending_inputs.pop(0), learn_params)
            return
        max_sub = self.config.max_substep_h * SECONDS_PER_HOUR
        while self.t < t:
            target = t
            if self._pending_inputs and self._pending_inputs[0][0] < target:
                target = max(self.t, self._pending_inputs[0][0])
            target = min(target, self.t + max_sub)
            self._predict_interval((target - self.t) / SECONDS_PER_HOUR, learn_params)
            self.t = target
            while self._pending_inputs and self._pending_inputs[0][0] <= self.t:
                self._set_inputs(self._pending_inputs.pop(0), learn_params)

    def _set_inputs(self, item: tuple[float, float, float], learn_params: bool) -> None:
        _, u, b = item
        if u > 0.0 and self.u <= 0.0:
            self._heating_in_burst = True
        if u <= 0.0 and self.u > 0.0 and self._heating_in_burst:
            self._heating_in_burst = False
            if learn_params:
                self.stats.bursts_learned += 1
        self.u = u
        self.b = b

    # --------------------------------------------------------- measurement
    def _update(
        self,
        z: float,
        r: float,
        *,
        learn_params: bool,
        learn_d: bool,
        robust: bool = True,
    ) -> float:
        """Scalar measurement update of the temperature state; returns NIS.

        ``robust`` selects the narrow Huber gate (unheated periods: large
        innovations are unexplained disturbances).  Right after heating the
        wide gate applies: large innovations are the informative data that
        must be allowed to correct the heating parameters.
        """
        cfg = self.config
        innov = z - self.x[IT]
        s = self.p[IT][IT] + r
        nis = innov * innov / s
        gate = cfg.gate if robust else cfg.gate_heating
        if nis > gate**2:
            # Huber weighting: inflate R so the normalised innovation equals the gate.
            r = r + (nis / gate**2 - 1.0) * s
            s = self.p[IT][IT] + r
        gain = [self.p[i][IT] / s for i in range(N)]
        if not learn_params:
            for idx in LEARNED_INDEXES:
                gain[idx] = 0.0
        else:
            # Trust region: scale (not clip) the gain so the Joseph-form
            # covariance stays consistent with the step actually taken.
            for idx in LEARNED_INDEXES:
                cap = cfg.max_gain_step if idx in (IG, IK) else cfg.max_param_step
                delta = abs(gain[idx] * innov)
                if delta > cap:
                    gain[idx] *= cap / delta
        if not learn_d:
            gain[ID] = 0.0
        for i in range(N):
            self.x[i] += gain[i] * innov
        # Joseph form keeps P positive definite with a sub-optimal (masked) gain.
        a = _zeros()
        for i in range(N):
            a[i][i] = 1.0
            a[i][IT] -= gain[i]
        p_new = _matmul(_matmul(a, self.p), _transpose(a))
        for i in range(N):
            gi = gain[i] * r
            if gi == 0.0:
                continue
            row = p_new[i]
            for j in range(N):
                row[j] += gi * gain[j]
        _symmetrize(p_new)
        self.p = p_new
        self._clamp()
        return nis

    def update_report(
        self,
        t: float,
        value: float,
        *,
        learn_params: bool,
        learn_d: bool = True,
        robust: bool = True,
    ) -> float:
        """Process a sensor report at ``t``; returns the innovation (K)."""
        if self.t is None:
            self.initialize(t, value)
            return 0.0
        self.advance(t, learn_params=learn_params)
        innov = value - self.x[IT]
        cfg = self.config
        r = cfg.sigma_meas**2
        s = self.p[IT][IT] + r
        nis = innov * innov / s
        self.stats.reports += 1
        self.stats.last_innovation = innov
        if nis > (2 * cfg.gate) ** 2:
            self.stats.outliers_in_row += 1
        else:
            self.stats.outliers_in_row = 0
        if robust and self.stats.outliers_in_row >= cfg.reset_after_outliers:
            # The temperature state lost track (disturbance, long gap): re-anchor
            # it instead of letting the parameters chase the error.
            self.initialize(t, value)
            self.stats.resets += 1
            self.stats.outliers_in_row = 0
            return innov
        self._update(value, r, learn_params=learn_params, learn_d=learn_d, robust=robust)
        self.stats.nis_ewma = 0.95 * self.stats.nis_ewma + 0.05 * min(nis, 9.0)
        if learn_params:
            self.stats.learned_reports += 1
        return innov

    def apply_silence_bound(
        self,
        t: float,
        displayed: float,
        half_width: float,
        *,
        learn_params: bool,
        learn_d: bool = True,
    ) -> bool:
        """Keep the estimate consistent with a silent sensor showing ``displayed``.

        Returns True if a pseudo-measurement was applied.
        """
        if self.t is None:
            return False
        self.advance(t, learn_params=learn_params)
        upper = displayed + half_width
        lower = displayed - half_width
        if self.x[IT] > upper:
            edge = upper
        elif self.x[IT] < lower:
            edge = lower
        else:
            return False
        self._update(
            edge,
            self.config.sigma_bound**2,
            learn_params=learn_params,
            learn_d=learn_d,
            robust=False,
        )
        return True

    def inflate_uncertainty(self, hours: float) -> None:
        """Add uncertainty to the dynamic states after a gap (restart)."""
        cfg = self.config
        h = max(0.0, hours)
        self.p[IT][IT] += cfg.prior_sd_t**2
        self.p[IQ][IQ] += cfg.process_q**2 * min(h, 4.0)
        self.p[IG][IG] += cfg.process_g**2 * min(h, 24.0)
        self.p[IK][IK] += cfg.process_k**2 * min(h, 24.0)

    def _clamp(self) -> None:
        x = self.x
        cfg = self.config
        for name, idx in zip(PARAM_NAMES, PARAM_INDEXES):
            lo, hi = PARAM_BOUNDS[name]
            x[idx] = min(math.log(hi), max(math.log(lo), x[idx]))
        x[IM] = min(x[IT] + 5.0, max(x[IT] - 5.0, x[IM]))
        if x[ITF] >= x[ITE]:
            # Filling must be faster than discharging (W > H_er).
            x[ITF] = x[ITE] - 0.05
        x[ID] = min(cfg.d_bounds[1], max(cfg.d_bounds[0], x[ID]))
        x[IG] = min(cfg.g_bounds[1], max(cfg.g_bounds[0], x[IG]))
        x[IK] = min(cfg.k_bounds[1], max(cfg.k_bounds[0], x[IK]))
        x[IQ] = min(math.exp(x[IQS]) * 1.2, max(-0.3, x[IQ]))

    # ----------------------------------------------------------- confidence
    def burst_energy(self, burst_s: float | None = None, logs: Sequence[float] | None = None) -> float:
        """Total heat (K of room rise, no losses) delivered by a burst."""
        prm = params_from_logs(logs if logs is not None else [self.x[i] for i in PARAM_INDEXES])
        burst_h = (self.config.reference_burst_s if burst_s is None else burst_s) / SECONDS_PER_HOUR
        b = 1.0 / prm.tau_f + 1.0 / prm.tau_e
        q_inf = prm.q_s / prm.tau_f / b
        decay = 1.0 - math.exp(-b * burst_h)
        during = q_inf * burst_h - q_inf * decay / b
        after = q_inf * decay * prm.tau_e
        return during + after

    def energy_rel_sd(self) -> float:
        """Relative standard deviation of the reference-burst energy."""
        logs = [self.x[i] for i in PARAM_INDEXES]
        base = self.burst_energy(logs=logs)
        if base <= 0:
            return 1.0
        grad = []
        for k in range(len(logs)):
            bumped = list(logs)
            bumped[k] += 1e-4
            grad.append((self.burst_energy(logs=bumped) - base) / 1e-4)
        var = 0.0
        for a_i, i in enumerate(PARAM_INDEXES):
            for b_i, j in enumerate(PARAM_INDEXES):
                var += grad[a_i] * self.p[i][j] * grad[b_i]
        return math.sqrt(max(0.0, var)) / base

    def confidence(self) -> float:
        """Return the parameter-based model confidence in [0, 1].

        Uses the relative uncertainty of the heat delivered by a reference
        burst (propagated from the parameter covariance), how much heating has
        actually been learned from, and innovation consistency.  The zone
        combines this with validated burst predictions.
        """
        rel_sd = self.energy_rel_sd()
        tail_sd = math.sqrt(max(0.0, self.p[ITE][ITE]))
        c_energy = min(1.0, max(0.0, (0.6 - rel_sd) / 0.35))
        c_tail = min(1.0, max(0.0, (0.5 - tail_sd) / 0.3))
        c_data = min(1.0, self.stats.bursts_learned / 4.0) * min(1.0, self.stats.heat_hours_learned / 0.25)
        nis = self.stats.nis_ewma
        c_consistency = 1.0 if nis < 4.0 else max(0.3, 4.0 / nis)
        return round(min(c_energy, 0.5 + 0.5 * c_tail) * c_data * c_consistency, 3)

    # ----------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": STORAGE_FORMAT,
            "x": [round(v, 6) for v in self.x],
            "p": [[float(f"{v:.6g}") for v in row] for row in self.p],
            "t": self.t,
            "u": self.u,
            "b": self.b,
            "t_out": self.t_out,
            "stats": {
                "reports": self.stats.reports,
                "learned_reports": self.stats.learned_reports,
                "heat_hours_learned": round(self.stats.heat_hours_learned, 4),
                "bursts_learned": self.stats.bursts_learned,
                "nis_ewma": round(self.stats.nis_ewma, 4),
                "resets": self.stats.resets,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, config: EstimatorConfig | None = None) -> ZoneEstimator:
        """Restore; anything unusable yields a fresh prior estimator."""
        est = cls(config=config or EstimatorConfig())
        if not isinstance(data, dict) or data.get("format") != STORAGE_FORMAT:
            return est
        try:
            x = [float(v) for v in data["x"]]
            p = [[float(v) for v in row] for row in data["p"]]
        except (KeyError, TypeError, ValueError):
            return est
        if len(x) != N or len(p) != N or any(len(row) != N for row in p):
            return est
        if not all(math.isfinite(v) for v in x) or not all(math.isfinite(v) for row in p for v in row):
            return est
        est.x = x
        est.p = p
        _symmetrize(est.p)
        est._clamp()
        t = data.get("t")
        est.t = float(t) if isinstance(t, (int, float)) else None
        est.u = _unit(data.get("u", 0.0))
        est.b = _unit(data.get("b", 0.0))
        t_out = data.get("t_out")
        est.t_out = float(t_out) if isinstance(t_out, (int, float)) else 10.0
        stats = data.get("stats") or {}
        try:
            est.stats = EstimatorStats(
                reports=int(stats.get("reports", 0)),
                learned_reports=int(stats.get("learned_reports", 0)),
                heat_hours_learned=float(stats.get("heat_hours_learned", 0.0)),
                bursts_learned=int(stats.get("bursts_learned", 0)),
                nis_ewma=float(stats.get("nis_ewma", 1.0)),
                resets=int(stats.get("resets", 0)),
            )
        except (TypeError, ValueError):
            est.stats = EstimatorStats()
        return est


def _unit(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return min(1.0, max(0.0, v))
