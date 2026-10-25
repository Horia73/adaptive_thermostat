"""Burst controller decisions."""

from __future__ import annotations

from engine.controller import MODE_FALLBACK, MODE_MODEL, BurstController, ControlInput, ControllerConfig
from engine.model import ThermalParams, ZoneState

PARAMS = ThermalParams(q_s=3.0, tau_f=0.08, tau_e=0.2, tau_r=15.0, tau_am=1.2, rho=6.0)


def _inp(temp: float, **kw) -> ControlInput:
    base = dict(
        now=10_000.0,
        target=22.0,
        deadband=0.1,
        reading=round(temp, 1),
        state=ZoneState(temp=temp, tm=temp, q=0.0, d=0.0, base=(temp - 5.0) / 15.0 - 0.1, t_out=5.0),
        params=PARAMS,
        confidence=1.0,
        use_model=True,
        start_delay_s=30.0,
        overrun=(),
        last_burst_end=None,
        last_burst_tail_until=None,
        heating=False,
        heat_elapsed_s=0.0,
        planned_burst_s=None,
    )
    base.update(kw)
    return ControlInput(**base)


def test_model_waits_while_room_is_comfortable():
    ctl = BurstController()
    decision = ctl.decide(_inp(22.15))
    assert ctl.mode == MODE_MODEL
    assert not decision.start


def test_model_fires_with_a_burst_that_lands_the_peak_on_target():
    ctl = BurstController()
    decision = ctl.decide(_inp(21.6))
    assert decision.start
    assert ControllerConfig().min_burst_s <= decision.burst_s <= ControllerConfig().max_burst_s
    assert abs(decision.predicted_peak - 22.0) < 0.1
    assert decision.tail_s > 0


def test_model_waits_for_the_tail_of_the_previous_burst():
    ctl = BurstController()
    decision = ctl.decide(_inp(21.85, last_burst_end=9_000.0, last_burst_tail_until=10_500.0))
    assert not decision.start
    assert decision.reason == "waiting for tail"
    # ... unless the room is far below target.
    decision = ctl.decide(_inp(21.2, last_burst_end=9_000.0, last_burst_tail_until=10_500.0))
    assert decision.start


def test_model_stops_early_when_peak_will_reach_target():
    ctl = BurstController()
    # Radiator fully charged, room already close: the tail alone reaches the target.
    state = ZoneState(temp=21.95, tm=21.8, q=3.0, d=0.0, base=1.1, t_out=5.0)
    decision = ctl.decide(
        _inp(21.95, state=state, heating=True, heat_elapsed_s=600.0, planned_burst_s=1500.0)
    )
    assert decision.stop


def test_model_respects_min_and_max_burst():
    ctl = BurstController()
    state = ZoneState(temp=21.99, tm=21.9, q=3.0, d=0.0, base=0.0, t_out=5.0)
    decision = ctl.decide(_inp(21.99, state=state, heating=True, heat_elapsed_s=30.0, planned_burst_s=120.0))
    assert not decision.stop and decision.reason == "min burst"
    cold = ZoneState(temp=15.0, tm=15.0, q=1.0, d=0.0, base=0.0, t_out=-5.0)
    decision = ctl.decide(
        _inp(15.0, state=cold, heating=True, heat_elapsed_s=45 * 60.0, planned_burst_s=2700.0)
    )
    assert decision.stop and decision.reason == "max burst"


def test_low_confidence_uses_fallback():
    ctl = BurstController()
    decision = ctl.decide(_inp(21.85, confidence=0.2))
    assert ctl.mode == MODE_FALLBACK
    assert decision.start
    assert decision.burst_s == ControllerConfig().fallback_base_s + 600 * 0.05 or decision.burst_s >= 120


def test_fallback_lockout_and_stop():
    cfg = ControllerConfig()
    ctl = BurstController(cfg)
    decision = ctl.decide(_inp(21.8, confidence=0.0, last_burst_end=10_000.0 - 600))
    assert not decision.start and decision.reason == "lock-out after burst"
    decision = ctl.decide(_inp(21.0, confidence=0.0, last_burst_end=10_000.0 - 700))
    assert decision.start  # cold room: short lock-out
    decision = ctl.decide(
        _inp(22.1, confidence=0.0, heating=True, heat_elapsed_s=200.0, planned_burst_s=900.0)
    )
    assert decision.stop


def test_mode_hysteresis():
    ctl = BurstController()
    ctl.decide(_inp(22.0, confidence=0.7))
    assert ctl.mode == MODE_MODEL
    ctl.decide(_inp(22.0, confidence=0.5))
    assert ctl.mode == MODE_MODEL
    ctl.decide(_inp(22.0, confidence=0.3))
    assert ctl.mode == MODE_FALLBACK
