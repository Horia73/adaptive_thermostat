"""Reading-space band control (2.2): trigger, aim, feasibility, piggyback.

The room sensors show 0.1 °C steps and report single steps only with their
30-minute periodic report, so "22.9" means 22.85 <= T < 22.95 at the time of
the report and little afterwards.  These tests pin down what the controller
does with that.
"""

from __future__ import annotations

import math

from engine.controller import MODE_MODEL, BurstController, ControlInput, ControllerConfig
from engine.estimator import IG, IK, PARAM_INDEXES, ZoneEstimator
from engine.model import ThermalParams, ZoneState
from engine.observer import ZoneObserver
from engine.zone import PendingTrigger, ZoneConfig, ZoneCore

# living_2 as learned by the live system on 09.11.2026.
LIVING = ThermalParams(q_s=2.416, tau_f=5.73 / 60, tau_e=15.7 / 60, tau_r=24.1, tau_am=1.21, rho=4.65)
# A radiator far too big for a 0.1 °C band: two minutes lift the room ~0.5 K.
OVERSIZED = ThermalParams(q_s=14.0, tau_f=0.05, tau_e=0.25, tau_r=24.1, tau_am=1.2, rho=4.0)
T_OUT = 8.0


def _state(temp: float, cooling: float, params: ThermalParams = LIVING) -> ZoneState:
    """Room at ``temp`` in equilibrium with its mass, cooling at ``cooling`` K/h."""
    return ZoneState(
        temp=temp, tm=temp, q=0.0, d=0.0, base=(temp - T_OUT) / params.tau_r - cooling, t_out=T_OUT
    )


def _inp(temp: float, cooling: float = 0.2, params: ThermalParams = LIVING, **kw) -> ControlInput:
    base = dict(
        now=100_000.0,
        target=23.0,
        deadband=0.1,
        reading=round(temp, 1),
        state=_state(temp, cooling, params),
        params=params,
        confidence=1.0,
        use_model=True,
        start_delay_s=25.0,
        overrun=(),
        last_burst_end=None,
        last_burst_tail_until=None,
        heating=False,
        heat_elapsed_s=0.0,
        planned_burst_s=None,
    )
    base.update(kw)
    return ControlInput(**base)


def test_band_is_defined_on_the_displayed_reading():
    ctl = BurstController()
    floor, ceiling, aim = ctl.band(_inp(23.0))
    cfg = ctl.config
    # 23 +/- 0.1 displayed = 22.85 <= T < 23.15, minus a small margin.
    assert math.isclose(floor, 22.85 + cfg.band_margin)
    assert math.isclose(ceiling, 23.15 - cfg.band_margin)
    assert math.isclose(aim, 23.0 + cfg.aim_fraction * 0.1)
    assert floor < aim < ceiling


def test_uncertainty_and_learned_correction_raise_the_trigger():
    ctl = BurstController()
    floor = ctl.band(_inp(23.0))[0]
    assert math.isclose(ctl.trigger_level(_inp(23.0, temp_sd=0.0), floor), floor)
    assert math.isclose(ctl.trigger_level(_inp(23.0, temp_sd=0.06), floor), floor + 0.03)
    capped = ctl.trigger_level(_inp(23.0, temp_sd=1.0), floor)
    assert math.isclose(capped, floor + ctl.config.max_uncertainty_margin)
    corrected = ctl.trigger_level(_inp(23.0, temp_sd=0.0, trigger_correction=0.04), floor)
    assert math.isclose(corrected, floor + 0.04)


def test_waits_inside_the_band_and_fires_before_the_floor():
    ctl = BurstController()
    decision = ctl.decide(_inp(23.02, cooling=0.2, temp_sd=0.03))
    assert ctl.mode == MODE_MODEL
    assert not decision.start and decision.reason == "not needed yet"
    assert decision.feasible
    # Heat is requested while the predicted minimum still stays above the
    # trigger: the reading never has to show 22.8.
    decision = ctl.decide(_inp(22.885, cooling=0.2, temp_sd=0.03))
    assert decision.start and decision.reason == "fire"
    pred = ctl.predict(_inp(22.885, cooling=0.2, temp_sd=0.03), delay_s=25.0, burst_s=decision.burst_s)
    assert pred.minimum >= 22.85
    assert decision.trigger is not None and decision.trigger >= 22.86


def test_burst_is_sized_for_the_aim_not_beyond_the_band():
    ctl = BurstController()
    decision = ctl.decide(_inp(22.88, cooling=0.3, temp_sd=0.03))
    assert decision.start
    assert decision.aim is not None
    assert decision.aim - 0.005 <= decision.predicted_peak <= 23.0 + 0.1
    # A different aim inside [target, target + deadband] moves the peak with it.
    centred = BurstController(ControllerConfig(aim_fraction=0.5))
    decision = centred.decide(_inp(22.88, cooling=0.3, temp_sd=0.03))
    assert decision.start
    assert 23.05 - 0.005 <= decision.predicted_peak <= 23.1


def test_stops_once_the_peak_reaches_the_aim():
    ctl = BurstController()
    charged = ZoneState(temp=22.95, tm=22.95, q=1.5, d=0.0, base=0.4, t_out=T_OUT)
    decision = ctl.decide(
        _inp(22.95, state=charged, heating=True, heat_elapsed_s=180.0, planned_burst_s=600.0)
    )
    assert decision.stop and decision.reason == "predicted peak reaches aim"


def test_min_burst_rise_larger_than_the_band_centres_the_oscillation():
    ctl = BurstController()
    cfg = ctl.config
    decision = ctl.decide(_inp(23.05, cooling=0.2, params=OVERSIZED, temp_sd=0.0))
    floor, ceiling, _aim = ctl.band(_inp(23.05))
    assert decision.feasible is False
    assert decision.min_rise is not None and decision.min_rise > ceiling - floor
    # The trigger moves down so that minimum and peak leave the band by the
    # same amount (bounded by max_lower_band).
    assert decision.trigger is not None
    assert 23.0 - cfg.max_lower_band - 1e-9 <= decision.trigger < floor
    below = decision.trigger - 0.005
    fired = ctl.decide(_inp(below, cooling=0.2, params=OVERSIZED, temp_sd=0.0))
    assert fired.start and fired.burst_s == cfg.min_burst_s
    pred = ctl.predict(_inp(below, cooling=0.2, params=OVERSIZED), delay_s=25.0, burst_s=cfg.min_burst_s)
    over_top = pred.peak - ceiling
    under_floor = floor - pred.minimum
    assert over_top > 0 and under_floor > 0
    if decision.trigger > 23.0 - cfg.max_lower_band + 1e-6:
        assert abs(over_top - under_floor) < 0.03


def test_piggyback_joins_a_running_boiler_when_heat_is_due_soon():
    ctl = BurstController(ControllerConfig(piggyback_min_burst_s=None))
    alone = ctl.decide(_inp(22.94, cooling=0.25, temp_sd=0.02))
    assert not alone.start and alone.reason == "not needed yet"
    joined = ctl.decide(_inp(22.94, cooling=0.25, temp_sd=0.02, boiler_firing=True))
    assert joined.start and joined.reason == "piggyback"
    assert joined.predicted_peak is not None and joined.predicted_peak <= ctl.band(_inp(23.0))[1]
    # Not when heat is not due within the window.
    far = ctl.decide(_inp(23.05, cooling=0.05, temp_sd=0.02, boiler_firing=True))
    assert not far.start
    # Not when even the smallest burst would push the room over the ceiling.
    big = ctl.decide(_inp(23.0, cooling=0.4, params=OVERSIZED, temp_sd=0.02, boiler_firing=True))
    assert not big.start


def test_piggyback_bursts_may_be_shorter_on_a_running_boiler():
    ctl = BurstController(ControllerConfig(piggyback_min_burst_s=60.0))
    joined = ctl.decide(_inp(22.94, cooling=0.25, temp_sd=0.02, boiler_firing=True))
    assert joined.start and joined.min_burst_s == 60.0
    assert 60.0 <= joined.burst_s <= ctl.config.max_burst_s
    # Without the boiler running the normal minimum applies.
    alone = ctl.decide(_inp(22.88, cooling=0.25, temp_sd=0.02))
    assert alone.start and alone.min_burst_s == ctl.config.min_burst_s
    # The active burst keeps its own minimum.
    state = ZoneState(temp=22.99, tm=22.95, q=1.2, d=0.0, base=0.3, t_out=T_OUT)
    running = ctl.decide(
        _inp(22.99, state=state, heating=True, heat_elapsed_s=70.0, planned_burst_s=60.0, burst_min_s=60.0)
    )
    assert running.reason != "min burst"


# ------------------------------------------------------------- zone level
def _seeded_core(cooling: float, now: float = 0.0) -> ZoneCore:
    """ZoneCore with the live living_2 model, room cooling at ``cooling`` K/h."""
    est = ZoneEstimator()
    for idx, value in zip(
        PARAM_INDEXES, (LIVING.q_s, LIVING.tau_f, LIVING.tau_e, LIVING.tau_r, LIVING.tau_am, LIVING.rho)
    ):
        est.x[idx] = math.log(value)
        est.p[idx][idx] = 0.15**2
    est.x[IG] = (23.0 - T_OUT) / LIVING.tau_r - cooling
    est.x[IK] = 0.0
    data = est.to_dict()
    data["t"] = now - 3600.0
    data["stats"].update(bursts_learned=200, heat_hours_learned=20.0)
    core = ZoneCore(
        ZoneConfig(), stored={"estimator": data, "validation_errors": [0.0, 0.0, 0.0]}, now=now - 7200.0
    )
    core.mode_block_until = 0.0
    core.set_hvac(now - 7200.0, True)
    core.set_target(23.0)
    core.on_outdoor(now - 3600.0, T_OUT)
    core.on_actuators(now - 3600.0, 0.0, False)
    return core


def test_field_case_held_22_9_fires_before_22_8_shows():
    """living_2, 09.11: 22.9 shown for 30 min, heat only after 22.8 appeared.

    The sensor reports single 0.1 steps only every 30 minutes, so the next
    report after the 22.9 would come 30 minutes later.  The controller must
    request heat before that, for slow and fast cooling alike, with a burst
    whose predicted minimum keeps the reading at 22.9.
    """
    for cooling in (0.05, 0.1, 0.2, 0.3):
        core = _seeded_core(cooling)
        core.on_temperature(-600.0, 23.0)
        core.update(-600.0)
        assert core.burst is None
        core.on_temperature(0.0, 22.9)
        fired_at = None
        t = 0.0
        while t <= 1800.0:
            status = core.update(t)
            if core.burst is not None:
                fired_at = t
                break
            t += 60.0
        assert fired_at is not None and fired_at <= 1800.0 - 5 * 60.0, cooling
        assert status.reason == "fire"
        assert status.heat_trigger_below is not None and status.heat_trigger_below > 22.85
        assert status.band_feasible
        decision = core._last_decision
        assert decision is not None and decision.predicted_peak is not None
        assert 23.0 - 0.01 <= decision.predicted_peak <= 23.1


def test_changed_report_keeps_the_estimate_in_the_reported_bin():
    obs = ZoneObserver.create()
    obs.on_outdoor(0.0, 8.0)
    obs.on_temperature(0.0, 23.1)
    obs.estimator.x[0] = 23.12
    obs.on_temperature(600.0, 22.9)
    half = obs.config.report_half_width
    assert half is not None
    assert 22.9 - half - 0.02 <= obs.estimator.temperature <= 22.9 + half + 0.02


def test_trigger_correction_learns_from_readings_below_the_band():
    core = _seeded_core(0.2)
    core.on_temperature(0.0, 23.0)
    core.update(0.0)
    core._pending_trigger = PendingTrigger(until=600.0, floor_reading=22.9)
    core.on_temperature(300.0, 22.8)
    core.update(700.0)
    assert math.isclose(core.trigger_correction, 0.02)
    # A clean burst lowers it slowly.
    core._pending_trigger = PendingTrigger(until=1500.0, floor_reading=22.9)
    core.on_temperature(1000.0, 22.9)
    core.update(1600.0)
    assert math.isclose(core.trigger_correction, 0.018)
    # Persisted with the model and reset with it.
    restored = ZoneCore(ZoneConfig(), stored=core.to_dict(), now=2000.0)
    assert math.isclose(restored.trigger_correction, 0.018)
    restored.reset_model()
    assert restored.trigger_correction == 0.0


def test_status_exposes_the_plan():
    core = _seeded_core(0.2)
    core.on_temperature(0.0, 23.1)
    status = core.update(60.0)
    assert status.control_mode == MODE_MODEL
    assert status.heat_trigger_below is not None and 22.85 < status.heat_trigger_below < 23.0
    assert status.aim_peak is not None and 23.0 <= status.aim_peak <= 23.1
    assert status.min_burst_rise is not None and status.min_burst_rise > 0
    assert status.band_feasible is True
