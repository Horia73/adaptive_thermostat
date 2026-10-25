"""Shared boiler policy: delays, anti short-cycling, overrun, races."""

from __future__ import annotations

from engine.boiler import BoilerConfig, BoilerPolicy, ZoneReport


def _policy() -> BoilerPolicy:
    p = BoilerPolicy(config=BoilerConfig(min_on_s=120, min_off_s=180))
    p.sync_actual(False, -1000.0)
    return p


def _r(demand: bool, *, open_: bool = False, closed: bool = True, forced: bool = False) -> ZoneReport:
    return ZoneReport(
        demand=demand,
        forced_stop=forced,
        valves_open=open_,
        valves_closed=closed,
        turn_on_delay_s=10,
        turn_off_delay_s=120,
    )


def test_turn_on_waits_for_open_valves_and_delay():
    p = _policy()
    p.update_zone("a", _r(True, closed=False))
    assert not p.evaluate(0).on
    p.update_zone("a", _r(True, open_=True, closed=False))
    d = p.evaluate(20)
    assert not d.on and d.next_wake == 30
    assert p.evaluate(30).on


def test_min_off_respected():
    p = _policy()
    p.sync_actual(False, 0.0)
    p.update_zone("a", _r(True, open_=True, closed=False))
    p.evaluate(10)
    assert not p.evaluate(100).on
    assert p.evaluate(180).on


def test_normal_last_stop_honours_min_on_and_holds_valves():
    p = _policy()
    p.update_zone("a", _r(True, open_=True, closed=False))
    p.evaluate(0)
    assert p.evaluate(10).on
    p.update_zone("a", _r(False, open_=True, closed=False))
    d = p.evaluate(60)
    assert d.on and d.reason == "minimum on time"
    assert p.hold_until("a", 60) is not None
    d = p.evaluate(130)
    assert not d.on
    assert p.hold_until("a", 130) == 130 + 120


def test_forced_stop_is_immediate():
    p = _policy()
    p.update_zone("a", _r(True, open_=True, closed=False))
    p.evaluate(0)
    p.evaluate(10)
    p.update_zone("a", _r(False, open_=True, closed=False, forced=True))
    d = p.evaluate(20)
    assert not d.on
    assert p.hold_until("a", 20) == 140  # valves stay open for the pump overrun only


def test_simultaneous_stop_turns_boiler_off():
    p = _policy()
    for z in ("a", "b"):
        p.update_zone(z, _r(True, open_=True, closed=False))
    p.evaluate(0)
    p.evaluate(300)
    assert p.on
    # Both stop in the same instant: processed one after the other.
    p.update_zone("a", _r(False, open_=True, closed=False))
    p.evaluate(600)
    assert p.on and p.hold_until("a", 600) is None  # b still heats: a closes now
    p.update_zone("b", _r(False, open_=True, closed=False))
    d = p.evaluate(600)
    assert not d.on
    assert p.hold_until("b", 600) == 720


def test_new_demand_cancels_idle_holds_and_waits_for_stray_valves():
    p = _policy()
    p.update_zone("a", _r(True, open_=True, closed=False))
    p.evaluate(0)
    p.evaluate(300)
    p.update_zone("a", _r(False, open_=True, closed=False, forced=True))
    p.evaluate(400)
    assert p.hold_until("a", 400) is not None
    p.update_zone("b", _r(True, open_=True, closed=False))
    d = p.evaluate(600)
    assert p.hold_until("a", 600) is None
    assert not d.on and d.reason == "waiting for idle zones to close"
    p.update_zone("a", _r(False, closed=True))
    assert p.evaluate(610).on


def test_stray_valve_wait_is_bounded():
    p = _policy()
    p.update_zone("stuck", _r(False, closed=False))
    p.update_zone("b", _r(True, open_=True, closed=False))
    p.evaluate(0)
    assert not p.evaluate(50).on
    assert p.evaluate(95).on


def test_seed_running_stops_boiler_with_overrun_hold():
    p = BoilerPolicy(config=BoilerConfig())
    p.sync_actual(True, -60.0)
    p.seed_running("a")
    p.update_zone("a", _r(False, open_=True, closed=False))
    d = p.evaluate(0)
    assert not d.on
    assert p.hold_until("a", 0) == 120
