"""Estimator: learns physically from quantised report-on-change data."""

from __future__ import annotations

import math
import random

from engine.estimator import ZoneEstimator
from engine.model import ThermalParams, step
from engine.observer import ObserverConfig, ZoneObserver

TRUE = ThermalParams(q_s=4.0, tau_f=0.08, tau_e=0.2, tau_r=12.0, tau_am=1.2, rho=6.0)


def _run(observer: ZoneObserver, hours: float, seed: int = 1, learn: bool = True) -> list[float]:
    """Synthetic room following TRUE with bursts, quantised report-on-change sensor."""
    rng = random.Random(seed)
    t = 1_790_000_000.0
    temp = tm = 21.5
    q = d = 0.0
    reported = None
    errors = []
    observer.learn_params = learn
    observer.on_outdoor(t, 5.0)
    burst_end = -1.0
    next_burst = t + 1800
    dt = 30.0
    for _ in range(int(hours * 3600 / dt)):
        heating = t < burst_end
        if t >= next_burst:
            burst_end = t + rng.choice([120, 240, 480, 900])
            next_burst = t + rng.uniform(2400, 5400)
            observer.on_actuators(t, 1.0, True)
        elif not heating and burst_end > 0 and t - dt < burst_end <= t:
            observer.on_actuators(t, 0.0, False)
        temp, tm, q, d = step(temp, tm, q, d, 1.0 if heating else 0.0, 5.0, TRUE, dt / 3600, base=0.15)
        t += dt
        value = round(temp + rng.gauss(0, 0.01), 1)
        if reported is None or abs(value - reported) >= 0.15:
            if reported is not None:
                observer.estimator.advance(t, learn_params=learn)
                errors.append(observer.estimator.temperature - value)
            reported = value
            observer.on_temperature(t, value)
        observer.tick(t)
    return errors


def test_learns_burst_energy_and_becomes_confident():
    obs = ZoneObserver.create(config=ObserverConfig(transport_delay_s=0.0, overrun_tau_s=1.0))
    assert obs.estimator.confidence() == 0.0
    errors = _run(obs, 72)
    est = obs.estimator
    true_energy = ZoneEstimator().burst_energy(600, [math.log(v) for v in (4.0, 0.08, 0.2, 12.0, 1.2, 6.0)])
    learned = est.burst_energy(600)
    assert abs(learned / true_energy - 1.0) < 0.35
    assert est.confidence() > 0.6
    late = errors[len(errors) // 2 :]
    assert sum(abs(e) for e in late) / len(late) < 0.1


def test_learning_gate_freezes_parameters():
    obs = ZoneObserver.create()
    before = list(obs.estimator.x[6:])
    _run(obs, 12, learn=False)
    assert obs.estimator.x[6:] == before
    assert obs.estimator.stats.bursts_learned == 0


def test_persistence_round_trip_and_corruption():
    obs = ZoneObserver.create()
    _run(obs, 12)
    data = obs.estimator.to_dict()
    restored = ZoneEstimator.from_dict(data)
    assert restored.x == data["x"]
    assert restored.stats.bursts_learned == obs.estimator.stats.bursts_learned
    for bad in (
        None,
        {},
        {"format": 99},
        {"format": data["format"], "x": [float("nan")] * 12, "p": data["p"]},
    ):
        fresh = ZoneEstimator.from_dict(bad)
        assert fresh.stats.reports == 0
        assert fresh.confidence() == 0.0


def test_outlier_does_not_move_parameters_much():
    obs = ZoneObserver.create()
    _run(obs, 24)
    est = obs.estimator
    params = list(est.x[6:])
    t = est.t + 600
    obs.recently_heated = lambda _t: False  # unheated: narrow gate
    est.update_report(t, est.temperature + 3.0, learn_params=True, robust=True)
    assert max(abs(a - b) for a, b in zip(params, est.x[6:])) <= 0.05 + 1e-9


def test_silence_bound_projects_into_band():
    est = ZoneEstimator()
    est.initialize(0.0, 22.0)
    est.x[0] = 22.6
    assert est.apply_silence_bound(1.0, 22.0, 0.15, learn_params=False)
    assert est.temperature < 22.6
    est.x[0] = 22.05
    assert not est.apply_silence_bound(2.0, 22.0, 0.15, learn_params=False)
