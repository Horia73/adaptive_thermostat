"""ZoneCore burst state machine and safety gating."""

from __future__ import annotations

from engine.zone import ZoneConfig, ZoneCore


def _core(now: float = 0.0) -> ZoneCore:
    core = ZoneCore(ZoneConfig(window_detection=True), now=now)
    core.on_outdoor(now, 5.0)
    core.on_temperature(now, 20.0)
    core.set_target(22.0)
    core.set_hvac(now, True)
    return core


def test_burst_counts_only_real_heat_time():
    core = _core()
    status = core.update(60)
    assert status.want_heat
    planned = status.planned_burst_s
    # Valves opening, boiler not yet on: no heat time accrues.
    core.on_actuators(70, 1.0, False)
    core.update(300)
    assert core.burst.heat_elapsed_s == 0
    core.on_actuators(300, 1.0, True)
    t = 300
    while core.burst is not None and t < 300 + planned + 120:
        t += 30
        core.update(t)
    assert core.burst is None
    assert abs(t - (300 + planned)) <= 31
    assert not core.last_forced_stop


def test_off_is_a_forced_stop():
    core = _core()
    core.update(60)
    core.on_actuators(80, 1.0, True)
    core.update(120)
    core.set_hvac(130, False)
    status = core.update(131)
    assert not status.want_heat and status.forced_stop
    assert status.reason == "off"


def test_heat_that_never_starts_is_abandoned():
    core = _core()
    core.update(60)
    status = core.update(60 + 16 * 60)
    assert not status.want_heat
    assert status.reason == "heat did not start"


def test_actuator_fault_blocks_heating():
    core = _core()
    core.set_actuator_fault(("valve_unavailable", "Valve unavailable"))
    status = core.update(60)
    assert not status.want_heat
    assert status.fault == "valve_unavailable"


def test_sensor_fault_has_priority():
    core = _core()
    core.set_actuator_fault(("valve_mismatch", "x"))
    core.on_temperature(10, None)
    status = core.update(400)
    assert status.fault == "sensor_unavailable"


def test_persistence_round_trip():
    core = _core()
    core.update(60)
    data = core.to_dict()
    restored = ZoneCore(ZoneConfig(), stored=data, now=100)
    assert all(abs(a - b) < 1e-5 for a, b in zip(restored.estimator.x[6:], core.estimator.x[6:]))
