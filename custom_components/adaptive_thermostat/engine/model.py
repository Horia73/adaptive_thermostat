"""Grey-box thermal model of a hydronic radiator zone.

Physics
-------
A radiator (emitter, heat capacity ``C_e``) is charged by hot water while its
zone valve is open *and* the boiler fires, and discharges into the room air
(``C_a``).  The air exchanges heat with the building mass of the room (walls,
floor, furniture, ``C_m``) and loses heat to outdoors::

    C_e dT_e/dt = u W (T_s - T_e) - H_ea (T_e - T_a)
    C_a dT_a/dt = H_ea (T_e - T_a) - H_am (T_a - T_m) - H_ao (T_a - T_o) + Q_g
    C_m dT_m/dt = H_am (T_a - T_m)

``u`` in [0, 1] is the open fraction of the zone's valves times the boiler
state, ``W`` the water-side conductance, ``T_s`` the supply temperature and
``Q_g`` passive gains.  Only ``T_a`` is measured.

The emitter is expressed as the heat flux it delivers to the air in
air-temperature units, ``q = H_ea (T_e - T_a) / C_a`` [K/h].  With
``C_e << C_a`` the ``dT_a/dt`` back-coupling in ``dq/dt`` is a few percent and
is neglected, which gives the identifiable model used everywhere here::

    dq/dt   = u (q_s - q) / tau_f - q / tau_e
    dT/dt   = q - (T - T_o) / tau_r - (T - T_m) / tau_am + g + d
    dT_m/dt = (T - T_m) / (rho * tau_am)
    dd/dt   = -d / tau_d                                  (+ noise)

* ``q_s``  warming rate of a fully charged radiator (K/h),
* ``tau_f = C_e / W`` radiator fill time, ``tau_e = C_e / H_ea`` discharge time,
* ``tau_r = C_a / H_ao`` air-to-outdoor loss time constant,
* ``tau_am = C_a / H_am`` air-to-mass coupling time, ``rho = C_m / C_a``,
* ``g`` slowly varying base gain (neighbouring flats, internal gains),
* ``d`` transient gains (sun, cooking, people) relaxing to zero with ``tau_d``.

Why a mass node: the recorded rooms rise quickly after a burst and then relax
part of the way back within ~30 min while heat soaks into the structure, and
they cool much more slowly than the air alone would.  A single room capacity
cannot reproduce both (replay: heated windows over-predicted, unheated ones
under-predicted).  With ``rho -> 0`` the model reduces to the classic
emitter + room two-state model.

For piecewise-constant inputs the system is linear; :func:`step` implements
the exact solution (closed-form 2x2 matrix exponential plus particular
solutions for the exponentially decaying emitter and disturbance terms).
Time is in hours.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

SECONDS_PER_HOUR = 3600.0
DIST_TAU_H = 1.5  # h, relaxation time of transient disturbances


@dataclass(frozen=True)
class ThermalParams:
    """Physical parameters of a zone (time constants in hours)."""

    q_s: float  # K/h, air warming rate delivered by a fully charged radiator
    tau_f: float  # h, radiator fill time constant while hot water flows
    tau_e: float  # h, radiator discharge time constant into the room
    tau_r: float  # h, air-to-outdoor loss time constant
    tau_am: float = 1.0  # h, air-to-mass coupling time constant
    rho: float = 8.0  # mass-to-air heat capacity ratio

    @property
    def q_max(self) -> float:
        """Steady emitter flux with the valve permanently open (K/h)."""
        return self.q_s * self.tau_e / (self.tau_e + self.tau_f)

    @property
    def steady_rise(self) -> float:
        """Steady room rise above outdoor at 100 % duty, without passive gains (K)."""
        return self.q_max * self.tau_r

    @property
    def lumped_tau_h(self) -> float:
        """Time constant of the whole room (air + mass) to outdoor (h)."""
        return self.tau_r * (1.0 + self.rho)


PARAM_NAMES = ("q_s", "tau_f", "tau_e", "tau_r", "tau_am", "rho")

# Physically plausible bounds; the estimator clamps to these.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "q_s": (0.2, 30.0),
    "tau_f": (1.0 / 60.0, 1.0),
    "tau_e": (0.03, 3.0),
    "tau_r": (1.0, 300.0),
    "tau_am": (0.1, 12.0),
    "rho": (0.5, 60.0),
}

# Priors for a panel radiator in a flat (also the fresh-install values).
DEFAULT_PARAMS = ThermalParams(q_s=3.0, tau_f=0.1, tau_e=0.25, tau_r=10.0, tau_am=1.0, rho=8.0)


def _expm2(a11: float, a12: float, a21: float, a22: float, h: float) -> tuple[float, float, float, float]:
    """exp(A h) for a 2x2 matrix with real eigenvalues (RC networks)."""
    tr = a11 + a22
    det = a11 * a22 - a12 * a21
    disc = max(tr * tr - 4.0 * det, 0.0)
    sq = math.sqrt(disc)
    l1 = 0.5 * (tr + sq)
    l2 = 0.5 * (tr - sq)
    if sq * h < 1e-9:
        # Repeated eigenvalue: exp(Ah) = e^{l h} (I + (A - l I) h)
        e = math.exp(l1 * h)
        return (
            e * (1.0 + (a11 - l1) * h),
            e * a12 * h,
            e * a21 * h,
            e * (1.0 + (a22 - l1) * h),
        )
    e1 = math.exp(l1 * h)
    e2 = math.exp(l2 * h)
    k = 1.0 / (l1 - l2)
    return (
        k * (e1 * (a11 - l2) - e2 * (a11 - l1)),
        k * (e1 - e2) * a12,
        k * (e1 - e2) * a21,
        k * (e1 * (a22 - l2) - e2 * (a22 - l1)),
    )


def _forced(a11: float, a12: float, a21: float, a22: float, beta: float) -> tuple[float, float]:
    """Particular solution w for forcing [1, 0] e^{-beta t}: w = -(A + beta I)^{-1} [1, 0]."""
    for _ in range(3):
        det = (a11 + beta) * (a22 + beta) - a12 * a21
        if abs(det) > 1e-12:
            return -(a22 + beta) / det, a21 / det
        beta *= 1.0 + 1e-6  # resonance: nudge off the eigenvalue
    return 0.0, 0.0


def step(
    temp: float,
    tm: float,
    q: float,
    d: float,
    u: float,
    t_out: float,
    params: ThermalParams,
    h: float,
    *,
    base: float = 0.0,
    tau_d: float = DIST_TAU_H,
) -> tuple[float, float, float, float]:
    """Advance by ``h`` hours with constant inputs (exact).  Returns ``(T, T_m, q, d)``."""
    if h <= 0.0:
        return temp, tm, q, d
    u = min(1.0, max(0.0, u))
    p = params
    a_o = 1.0 / p.tau_r
    a_m = 1.0 / p.tau_am
    m = 1.0 / (p.rho * p.tau_am)
    a11, a12, a21, a22 = -(a_o + a_m), a_m, m, -m
    # Emitter: first order towards q_inf with rate b.
    b = u / p.tau_f + 1.0 / p.tau_e
    q_inf = (u * p.q_s / p.tau_f) / b
    c_q = q - q_inf
    c_d = 1.0 / tau_d
    # Steady state for the constant part of the forcing (air and mass equal).
    t_ss = t_out + p.tau_r * (base + q_inf)
    wq1, wq2 = _forced(a11, a12, a21, a22, b)
    wd1, wd2 = _forced(a11, a12, a21, a22, c_d)
    r1 = temp - t_ss - wq1 * c_q - wd1 * d
    r2 = tm - t_ss - wq2 * c_q - wd2 * d
    e11, e12, e21, e22 = _expm2(a11, a12, a21, a22, h)
    eq = math.exp(-b * h)
    ed = math.exp(-c_d * h)
    t_new = t_ss + wq1 * c_q * eq + wd1 * d * ed + e11 * r1 + e12 * r2
    tm_new = t_ss + wq2 * c_q * eq + wd2 * d * ed + e21 * r1 + e22 * r2
    return t_new, tm_new, q_inf + c_q * eq, d * ed


def derivative(
    temp: float, tm: float, q: float, t_out: float, d: float, params: ThermalParams, base: float = 0.0
) -> float:
    """Return dT/dt of the air in K/h."""
    return q - (temp - t_out) / params.tau_r - (temp - tm) / params.tau_am + base + d


@dataclass(frozen=True)
class ZoneState:
    """Model state used for prediction."""

    temp: float  # air temperature
    tm: float  # building mass temperature
    q: float  # K/h, emitter flux
    d: float  # K/h, transient disturbance
    base: float  # K/h, base gain (+ expected shared heat)
    t_out: float  # outdoor temperature (held over the horizon)


@dataclass(frozen=True)
class BurstPrediction:
    """Outcome of simulating a candidate heating burst."""

    peak: float  # highest predicted temperature from now on
    peak_after_s: float  # seconds from now until the peak
    minimum: float  # lowest temperature up to the peak
    end_temp: float  # temperature at the end of the simulation
    tail_s: float  # seconds from end of heating to the peak (0 if no burst)


def _segments(
    delay_s: float, burst_s: float, overrun: tuple[tuple[float, float], ...]
) -> list[tuple[float, float]]:
    """Input change points ``[(t_s, u)]`` for "wait, heat, overrun, coast"."""
    if burst_s <= 0:
        return [(0.0, 0.0)]
    out = [(0.0, 0.0), (delay_s, 1.0)]
    end = delay_s + burst_s
    for offset, level in overrun:
        out.append((end + offset, level))
    if not overrun or overrun[-1][1] != 0.0:
        out.append((end + (overrun[-1][0] if overrun else 0.0), 0.0))
    return out


def simulate_burst(
    state: ZoneState,
    params: ThermalParams,
    *,
    delay_s: float,
    burst_s: float,
    horizon_s: float,
    dt_s: float = 60.0,
    overrun: tuple[tuple[float, float], ...] = (),
) -> BurstPrediction:
    """Simulate "wait ``delay_s``, heat for ``burst_s``, overrun, then coast".

    ``overrun`` is the pump-overrun input profile after the burst
    (``[(offset_s, level)]``, last level 0).  ``burst_s`` may be zero (free
    response).  The simulation stops early once heating is over, the peak is
    behind us and the room is cooling.
    """
    changes = _segments(delay_s, burst_s, overrun)
    heat_end = changes[-1][0] if burst_s > 0 else 0.0
    t = 0.0
    cur_t, cur_m, cur_q, cur_d = state.temp, state.tm, state.q, state.d
    peak, peak_at = cur_t, 0.0
    minimum = cur_t
    idx = 0
    u = 0.0
    while t < horizon_s - 1e-9:
        while idx < len(changes) and changes[idx][0] <= t + 1e-9:
            u = changes[idx][1]
            idx += 1
        nxt = min(horizon_s, t + dt_s)
        if idx < len(changes) and changes[idx][0] < nxt:
            nxt = changes[idx][0]
        cur_t, cur_m, cur_q, cur_d = step(
            cur_t,
            cur_m,
            cur_q,
            cur_d,
            u,
            state.t_out,
            params,
            (nxt - t) / SECONDS_PER_HOUR,
            base=state.base,
        )
        t = nxt
        if cur_t > peak:
            peak, peak_at = cur_t, t
        if t <= max(peak_at, heat_end) + 1e-9:
            minimum = min(minimum, cur_t)
        if (
            t >= heat_end
            and t - peak_at > max(1800.0, 2 * SECONDS_PER_HOUR * params.tau_e)
            and derivative(cur_t, cur_m, cur_q, state.t_out, cur_d, params, state.base) < 0
        ):
            break
    burst_end = delay_s + burst_s
    tail = max(0.0, peak_at - burst_end) if burst_s > 0 else 0.0
    return BurstPrediction(
        peak=peak,
        peak_after_s=peak_at,
        minimum=minimum,
        end_temp=cur_t,
        tail_s=tail,
    )


def trajectory(
    state: ZoneState,
    params: ThermalParams,
    *,
    delay_s: float,
    burst_s: float,
    horizon_s: float,
    dt_s: float = 120.0,
    overrun: tuple[tuple[float, float], ...] = (),
) -> list[tuple[float, float]]:
    """Return ``[(seconds, temperature)]`` for "wait, heat, overrun, coast"."""
    changes = _segments(delay_s, burst_s, overrun)
    out = [(0.0, state.temp)]
    t = 0.0
    cur_t, cur_m, cur_q, cur_d = state.temp, state.tm, state.q, state.d
    idx = 0
    u = 0.0
    while t < horizon_s - 1e-9:
        while idx < len(changes) and changes[idx][0] <= t + 1e-9:
            u = changes[idx][1]
            idx += 1
        nxt = min(horizon_s, t + dt_s)
        if idx < len(changes) and changes[idx][0] < nxt:
            nxt = changes[idx][0]
        cur_t, cur_m, cur_q, cur_d = step(
            cur_t,
            cur_m,
            cur_q,
            cur_d,
            u,
            state.t_out,
            params,
            (nxt - t) / SECONDS_PER_HOUR,
            base=state.base,
        )
        t = nxt
        out.append((t, cur_t))
    return out
