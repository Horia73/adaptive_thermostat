"""Closed-loop tests: the real engine controlling simulated rooms.

The simulator's physics is deliberately richer than the controller's model
(asymmetric water-side radiator, shared supply temperature, pump overrun,
pipe gains, mass losses, sun, cooking spikes, a lagging 0.1 °C sensor that
reports on 0.2 °C changes plus 30-minute heartbeats, drop-outs).  Metrics are
taken from day 2 on, after the model has learned from day 1.
"""

from __future__ import annotations

import pytest
from simulator import (
    Plant,
    SensorTruth,
    boiler_metrics,
    burst_overshoot,
    comfort_metrics,
    fast_room,
    leaky_room,
    slow_room,
    typical_room,
    warmup_lower_bound,
)

from engine.boiler import BoilerConfig
from engine.controller import MODE_MODEL
from engine.zone import ZoneConfig

ROOMS = {"typical": typical_room, "fast": fast_room, "slow": slow_room, "leaky": leaky_room}
DAY = 24 * 3600.0


def _single(
    name: str, *, use_model: bool = True, hours: float = 72, seed: int = 1, start: float = 20.5
) -> Plant:
    plant = Plant(
        [(ROOMS[name](name), SensorTruth())],
        target=22.0,
        start_temp=start,
        seed=seed,
        zone_config=ZoneConfig(use_model=use_model),
    )
    plant.run(hours)
    return plant


@pytest.mark.parametrize("name", list(ROOMS))
def test_converges_and_holds_target(name: str) -> None:
    plant = _single(name)
    zone = plant.zones[0]
    start = plant.t0 + DAY
    comfort = comfort_metrics(zone, start)
    overshoot = burst_overshoot(zone, start)
    boiler = boiler_metrics(plant, start)
    # Learned and trusted after a day.
    assert zone.core.controller.mode == MODE_MODEL
    assert zone.core.confidence() >= 0.6
    # Holds the target without systematic offset.
    assert abs(comfort["mean_error"]) <= 0.15
    assert comfort["within_0_3"] >= 0.9
    # No systematic heating-caused overshoot (peak after each burst).
    assert overshoot["bursts"] >= 20
    assert overshoot["p90_excess"] <= 0.35
    # No short-cycling.
    assert comfort["min_burst_s"] >= 110
    assert comfort["bursts_per_hour"] <= 3.0
    assert boiler["min_on_s"] >= 115
    assert boiler["min_off_s"] >= 175


@pytest.mark.parametrize("name", ["typical", "leaky"])
def test_model_control_beats_fallback(name: str) -> None:
    plant_model = _single(name)
    plant_fallback = _single(name, use_model=False)
    m = comfort_metrics(plant_model.zones[0], plant_model.t0 + DAY)
    f = comfort_metrics(plant_fallback.zones[0], plant_fallback.t0 + DAY)
    assert m["mae"] < f["mae"]
    assert m["within_0_2"] > f["within_0_2"]


@pytest.mark.parametrize("name", ["typical", "fast"])
def test_cold_start_warms_up_without_overshoot(name: str) -> None:
    """Fresh install, room at 18 °C, target 22 °C: works from day one.

    A room cannot warm faster than its radiator allows: the bar is 2.5x the
    time it needs with the radiator on continuously (the fallback controller
    leaves room for the tail, valve travel and boiler anti-cycling waits).
    """
    bound = warmup_lower_bound(ROOMS[name](name), 18.0, 21.7)
    assert bound is not None
    plant = Plant([(ROOMS[name](name), SensorTruth())], target=22.0, start_temp=18.0, seed=4)
    plant.run(2.5 * bound / 3600.0 + 2.0)
    zone = plant.zones[0]
    reached = next((t for t, air, *_ in zone.log if air >= 21.7), None)
    assert reached is not None
    assert reached - plant.t0 <= 2.5 * bound
    peak = max(air for _t, air, *_ in zone.log)
    assert peak <= 22.6


def test_shared_boiler_four_zones() -> None:
    plant = Plant(
        [(f(name), SensorTruth()) for name, f in ROOMS.items()],
        target=22.0,
        start_temp=21.0,
        seed=2,
        boiler_config=BoilerConfig(min_on_s=120, min_off_s=180),
    )
    plant.run(48)
    start = plant.t0 + DAY
    boiler = boiler_metrics(plant, start)
    assert boiler["min_on_s"] >= 115
    assert boiler["min_off_s"] >= 175
    for zone in plant.zones:
        comfort = comfort_metrics(zone, start)
        overshoot = burst_overshoot(zone, start)
        assert comfort["mean_error"] >= -0.1, zone.truth.name  # never under-heated
        assert overshoot["p90_excess"] <= 0.5, zone.truth.name
        if zone.truth.name != "fast":
            assert comfort["within_0_3"] >= 0.8, zone.truth.name
    # The small, light room gets passive heat from the shared pipes whenever
    # the other zones fire the boiler (no cooling available): its time above
    # target is passive, while its heating-caused overshoot is bounded above.


def test_zone_switched_off_gets_no_heat() -> None:
    """One zone OFF while the others keep the shared boiler busy."""
    plant = Plant(
        [(typical_room("on"), SensorTruth()), (leaky_room("off"), SensorTruth())],
        target=22.0,
        start_temp=20.0,
        seed=5,
    )
    plant.run(12)
    off_zone = plant.zones[1]
    off_zone.core.set_hvac(plant.t, False)
    switched = plant.t
    plant.run(12)
    heated_after = sum(
        min(b1, plant.t) - max(b0, switched + 60) for b0, b1 in off_zone.bursts if b1 > switched + 60
    )
    assert heated_after <= 0
    assert all(s == "closed" for s in off_zone.valve_state)


def test_disturbances_windows_spikes_dropouts() -> None:
    room = typical_room("busy")
    room.window_rate_per_day = 2.0
    room.window_minutes = 15.0
    room.window_h = 200.0
    room.spike_rate_per_day = 1.5
    sensor = SensorTruth(dropout_rate_per_day=3.0)
    plant = Plant(
        [(room, sensor)],
        target=22.0,
        start_temp=21.0,
        seed=11,
        zone_config=ZoneConfig(window_detection=True),
    )
    plant.run(72)
    zone = plant.zones[0]
    start = plant.t0 + DAY
    comfort = comfort_metrics(zone, start)
    assert comfort["within_0_3"] >= 0.75
    assert comfort["bursts_per_hour"] <= 3.0
    # Little heat goes into open windows (detection latency only).
    window_heat = 0.0
    window_time = 0.0
    for w0, w1, kind in zone.passive_events:
        if kind != "window" or w0 < start:
            continue
        window_time += w1 - w0
        for b0, b1 in zone.bursts:
            window_heat += max(0.0, min(b1, w1) - max(b0, w0 + 600))
    assert window_time > 0
    assert window_heat <= 0.2 * window_time
