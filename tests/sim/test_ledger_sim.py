"""The cycle ledger on the closed-loop simulator: no effect on control, useful records."""

from __future__ import annotations

from typing import Any

from simulator import Plant, SensorTruth, typical_room

from engine.ledger import END_NEXT_CYCLE, END_OBSERVED, MAX_CYCLES, CycleLedger
from engine.zone import ZoneConfig


def _attach(plant: Plant) -> CycleLedger:
    """Feed a ledger exactly as the Home Assistant runtime does (after each update / report)."""
    zone = plant.zones[0]
    core = zone.core
    ledger = CycleLedger(None, now=plant.t)
    update, on_temperature = core.update, core.on_temperature

    def observed_update(t: float, *args: Any, **kwargs: Any) -> Any:
        status = update(t, *args, **kwargs)
        ledger.observe(t, core, status)
        return status

    def observed_temperature(t: float, value: float | None, *args: Any, **kwargs: Any) -> None:
        on_temperature(t, value, *args, **kwargs)
        ledger.on_reading(t, value)

    core.update = observed_update  # type: ignore[method-assign]
    core.on_temperature = observed_temperature  # type: ignore[method-assign]
    return ledger


def _plant() -> Plant:
    return Plant(
        [(typical_room("typical"), SensorTruth())],
        target=22.0,
        start_temp=20.5,
        seed=3,
        zone_config=ZoneConfig(),
    )


def test_ledger_does_not_change_heating_and_records_model_cycles() -> None:
    hours = 40
    reference = _plant()
    reference.run(hours)
    plant = _plant()
    ledger = _attach(plant)
    plant.run(hours)
    ref, zone = reference.zones[0], plant.zones[0]
    # Identical decisions: same bursts, same requests, same trajectory, same boiler.
    assert zone.bursts == ref.bursts and len(zone.bursts) > 20
    assert zone.fires == ref.fires
    assert zone.log == ref.log
    assert plant.boiler_switches == reference.boiler_switches
    assert zone.core.to_dict() == ref.core.to_dict()

    cycles = ledger.cycles
    assert 0 < len(cycles) <= MAX_CYCLES
    ids = [c["id"] for c in cycles]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    model = [c for c in cycles if c["end"] == END_OBSERVED and c["stop"]["mode"] == "model"]
    assert model, "the model takes over within the run"
    for record in model:
        stop = record["stop"]
        assert stop["predicted_peak"] is not None and stop["time_to_peak_s"] is not None
        assert record["observed"]["peak_error"] is not None
        assert abs(record["observed"]["peak_error"]) < 1.0
        assert stop["commanded_s"] >= stop["heat_s"] > 0
    clean = [c for c in model if c["clean"]]
    assert clean
    # The next burst usually comes once the predicted peak has passed: the
    # record ends there, still comparable.
    following = [c for c in cycles if c["end"] == END_NEXT_CYCLE and c["clean"]]
    assert following and all(c["observed"]["truncated_after_stop_s"] > 0 for c in following)
    # Every closed record that is not clean says why: flags or its end.
    for record in cycles:
        if record["state"] == "closed" and not record["clean"]:
            assert record["flags"] or record["end"] not in (END_OBSERVED, END_NEXT_CYCLE)
