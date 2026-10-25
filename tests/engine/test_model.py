"""Model equations: exactness, physical limits, burst simulation."""

from __future__ import annotations

import random

from engine.model import (
    DEFAULT_PARAMS,
    ThermalParams,
    ZoneState,
    simulate_burst,
    step,
)


def _rk4(state, u, t_out, p, h, base, tau_d=1.5, n=4000):
    def f(s):
        temp, tm, q, d = s
        return (
            q - (temp - t_out) / p.tau_r - (temp - tm) / p.tau_am + base + d,
            (temp - tm) / (p.rho * p.tau_am),
            u * (p.q_s - q) / p.tau_f - q / p.tau_e,
            -d / tau_d,
        )

    s = state
    dt = h / n
    for _ in range(n):
        k1 = f(s)
        k2 = f(tuple(a + dt / 2 * b for a, b in zip(s, k1)))
        k3 = f(tuple(a + dt / 2 * b for a, b in zip(s, k2)))
        k4 = f(tuple(a + dt * b for a, b in zip(s, k3)))
        s = tuple(a + dt / 6 * (b + 2 * c + 2 * e + g) for a, b, c, e, g in zip(s, k1, k2, k3, k4))
    return s


def test_step_is_exact():
    rng = random.Random(3)
    for _ in range(25):
        p = ThermalParams(
            q_s=rng.uniform(0.5, 10),
            tau_f=rng.uniform(0.02, 0.3),
            tau_e=rng.uniform(0.35, 1.0),
            tau_r=rng.uniform(2, 200),
            tau_am=rng.uniform(0.2, 5),
            rho=rng.uniform(1, 30),
        )
        state = (rng.uniform(18, 24), rng.uniform(18, 24), rng.uniform(0, 3), rng.uniform(-1, 1))
        u = rng.choice([0.0, 0.5, 1.0])
        t_out = rng.uniform(-5, 15)
        h = rng.uniform(0.01, 2)
        base = rng.uniform(-0.3, 0.3)
        exact = step(*state, u, t_out, p, h, base=base)
        ref = _rk4(state, u, t_out, p, h, base)
        assert max(abs(a - b) for a, b in zip(exact, ref)) < 1e-6


def test_steady_state_matches_parameters():
    p = DEFAULT_PARAMS
    temp, tm, q, d = 20.0, 20.0, 0.0, 0.0
    for _ in range(2000):  # whole-room time constant (1 + rho) tau_r = 90 h
        temp, tm, q, d = step(temp, tm, q, d, 1.0, 5.0, p, 1.0, base=0.1)
    assert abs(q - p.q_max) < 1e-6
    assert abs(temp - (5.0 + p.tau_r * (0.1 + p.q_max))) < 1e-3
    assert abs(tm - temp) < 1e-3


BALANCED = (21.0 - 5.0) / 10.0  # base gain that holds 21 °C without heating


def test_longer_bursts_heat_more_and_peak_after_the_burst():
    state = ZoneState(temp=21.0, tm=21.0, q=0.0, d=0.0, base=BALANCED, t_out=5.0)
    peaks = []
    for burst in (120, 300, 600, 1200):
        pred = simulate_burst(state, DEFAULT_PARAMS, delay_s=30, burst_s=burst, horizon_s=4 * 3600)
        peaks.append(pred.peak)
        assert pred.tail_s > 0  # the radiator keeps heating after the valve closes
        assert pred.peak_after_s > 30 + burst
    assert peaks == sorted(peaks)


def test_overrun_adds_heat():
    state = ZoneState(temp=21.0, tm=21.0, q=0.0, d=0.0, base=BALANCED, t_out=5.0)
    plain = simulate_burst(state, DEFAULT_PARAMS, delay_s=0, burst_s=180, horizon_s=3 * 3600)
    overrun = simulate_burst(
        state,
        DEFAULT_PARAMS,
        delay_s=0,
        burst_s=180,
        horizon_s=3 * 3600,
        overrun=((0.0, 0.7), (60.0, 0.3), (120.0, 0.0)),
    )
    assert overrun.peak > plain.peak


def test_free_response_of_warm_room_cools_towards_outdoor():
    state = ZoneState(temp=23.0, tm=23.0, q=0.0, d=0.0, base=0.0, t_out=0.0)
    pred = simulate_burst(state, DEFAULT_PARAMS, delay_s=0, burst_s=0, horizon_s=3600)
    assert pred.end_temp < 23.0
    assert pred.peak == 23.0
