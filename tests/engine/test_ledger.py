"""Cycle ledger: one diagnostic record per burst, driven by the real ZoneCore."""

from __future__ import annotations

from datetime import datetime
import json

from engine import ledger as ledger_module
from engine.ledger import (
    END_NEXT_CYCLE,
    END_NO_HEAT,
    END_OBSERVED,
    END_RESTART,
    FLAG_FORCED_STOP,
    FLAG_MANUAL_OFF,
    FLAG_NO_HEAT,
    FLAG_PEAK_UNCONFIRMED,
    FLAG_READING_UNAVAILABLE,
    FLAG_REHEAT,
    FLAG_RESTART,
    FLAG_SENSOR_GAP,
    FLAG_TARGET_CHANGED,
    FLAG_WINDOW,
    MAX_CYCLES,
    STATE_CLOSED,
    STATE_HEATING,
    STATE_OBSERVING,
    CycleLedger,
    iso,
)
from engine.safety import SafetyConfig
from engine.zone import ZoneConfig, ZoneCore

T0 = 1_790_000_000.0


class Rig:
    """A ZoneCore and its ledger, fed the way the Home Assistant runtime feeds them."""

    def __init__(self, reading: float = 21.85, config: ZoneConfig | None = None) -> None:
        self.t = T0
        self.core = ZoneCore(config or ZoneConfig(), now=T0)
        self.ledger = CycleLedger(None, now=T0)
        self.core.on_outdoor(T0, 5.0)
        self.core.set_target(22.0)
        self.core.set_hvac(T0, True)
        self.read(reading)
        self.valves = 0.0

    def read(self, value: float | None) -> None:
        self.core.on_temperature(self.t, value)
        self.ledger.on_reading(self.t, value)

    def step(self) -> None:
        status = self.core.update(self.t)
        self.ledger.observe(self.t, self.core, status)
        # Valves follow the demand at once, the boiler fires with them.
        valves = 1.0 if status.want_heat else 0.0
        if valves != self.valves:
            self.valves = valves
            self.core.on_actuators(self.t, valves, valves > 0)

    def run(self, seconds: float, readings: dict[float, float | None] | None = None) -> None:
        """Advance in 30 s steps; ``readings`` maps offsets from now to values."""
        pending = sorted((readings or {}).items())
        start = self.t
        while self.t < start + seconds:
            self.t += 30.0
            while pending and start + pending[0][0] <= self.t:
                self.read(pending.pop(0)[1])
            self.step()

    def heat_one_burst(self) -> dict:
        self.run(60)
        record = self.ledger.open_record
        assert record is not None and record["state"] == STATE_HEATING
        while self.core.burst is not None:
            self.run(30)
        return record


def _rising_then_falling(peak_after: float = 900.0) -> dict[float, float]:
    return {300.0: 21.9, peak_after - 300: 22.0, peak_after: 22.1, peak_after + 600: 22.0}


def test_normal_cycle_is_recorded_and_clean() -> None:
    rig = Rig()
    record = rig.heat_one_burst()
    assert record["state"] == STATE_OBSERVING
    stop = record["stop"]
    assert record["start"]["mode"] == "fallback" and record["start"]["reason"] == "fire"
    assert stop["reason"] == "planned length reached" and stop["forced"] is False
    # Planned against actual: heat time only counts while heat flows.
    assert stop["planned_s"] == record["start"]["planned_s"]
    assert abs(stop["heat_s"] - stop["planned_s"]) <= 30
    assert stop["commanded_s"] >= stop["heat_s"]
    assert stop["reading"] == 21.85 and stop["target"] == 22.0
    # The model's peak for the burst as actually run (fallback has no stop prediction).
    assert stop["predicted_peak"] is None and stop["model_peak_as_run"] is not None
    window = record["observed"]["window_s"]
    assert 1800 <= window <= 3 * 3600
    rig.run(window + 120, _rising_then_falling())
    assert record["state"] == STATE_CLOSED and record["end"] == END_OBSERVED
    assert record["flags"] == [] and record["clean"] is True
    obs = record["observed"]
    assert obs["max"] == 22.1 and obs["max_before_perturbation"] == 22.1
    assert 800 <= obs["max_after_stop_s"] <= 1000
    assert obs["readings"] == 4 and obs["min"] == 21.85 and obs["last"] == 22.0
    stopped = datetime.fromisoformat(record["stopped_at"]).timestamp()
    assert record["closed_at"] == iso(stopped + window)
    assert rig.ledger.open_record is None


def test_model_prediction_at_stop_gives_peak_error() -> None:
    """With a model prediction at the stop the record carries observed - predicted."""
    rig = Rig()
    rig.run(60)
    record = rig.ledger.open_record
    assert record is not None
    while rig.core.burst is not None:
        rig.run(30)
    # Simulate what the model mode writes at the stop (the controller itself is untouched).
    record["stop"]["predicted_peak"] = 22.05
    rig.run(record["observed"]["window_s"] + 60, _rising_then_falling())
    assert record["observed"]["peak_error"] == 0.05


def test_window_during_tail_freezes_clean_maximum() -> None:
    rig = Rig()
    record = rig.heat_one_burst()
    rig.run(600, {300.0: 21.9})
    rig.core.set_window_contact(rig.t, True)
    rig.run(300, {60.0: 22.2})
    rig.core.set_window_contact(rig.t, False)
    rig.run(record["observed"]["window_s"])
    assert record["end"] == END_OBSERVED and record["clean"] is False
    assert record["flags"][0] == FLAG_WINDOW
    assert record["first_perturbation"] == FLAG_WINDOW
    obs = record["observed"]
    assert obs["max"] == 22.2 and obs["max_before_perturbation"] == 21.9


def test_target_change_is_a_perturbation() -> None:
    rig = Rig()
    record = rig.heat_one_burst()
    rig.run(300)
    rig.core.set_target(23.0)
    rig.run(record["observed"]["window_s"])
    assert FLAG_TARGET_CHANGED in record["flags"] and not record["clean"]
    assert record["target_min"] == 22.0 and record["target_max"] == 23.0
    assert record["stop"]["target"] == 22.0


def test_manual_off_forced_stop() -> None:
    rig = Rig(reading=21.0)
    rig.run(120)
    record = rig.ledger.open_record
    assert record is not None
    rig.ledger.note(rig.t, FLAG_MANUAL_OFF)
    rig.core.set_hvac(rig.t, False)
    rig.run(30)
    assert record["stop"]["reason"] == "off" and record["stop"]["forced"] is True
    assert record["flags"][:2] == [FLAG_MANUAL_OFF, FLAG_FORCED_STOP]
    assert record["stop"]["heat_s"] < record["stop"]["planned_s"]


def test_reheat_closes_the_previous_record() -> None:
    """Room 2 K low: the fallback fires again after its cold lock-out while the reading still rises."""
    rig = Rig(reading=20.0)
    first = rig.heat_one_burst()
    rig.run(1500, {120.0: 20.1, 300.0: 20.2, 480.0: 20.3, 1200.0: 20.4})
    assert first["state"] == STATE_CLOSED and first["end"] == END_NEXT_CYCLE
    assert FLAG_REHEAT in first["flags"] and not first["clean"]
    assert 540 <= first["observed"]["truncated_after_stop_s"] <= 700
    assert first["observed"]["max"] == 20.3  # readings after the new request are not counted
    second = rig.ledger.open_record
    assert second is not None and second["id"] == first["id"] + 1


def test_unconfirmed_maximum_is_flagged() -> None:
    rig = Rig()
    record = rig.heat_one_burst()
    window = record["observed"]["window_s"]
    rising = {offset: round(21.9 + 0.1 * i, 1) for i, offset in enumerate(range(600, window, 600))}
    rising[window - 60] = 23.0
    rig.run(window + 60, rising)
    assert record["end"] == END_OBSERVED
    assert record["flags"] == [FLAG_PEAK_UNCONFIRMED] and record["clean"] is False


def test_unavailable_reading_and_stale_sensor() -> None:
    rig = Rig(config=ZoneConfig(safety=SafetyConfig(stale_timeout_s=600)))
    record = rig.heat_one_burst()
    rig.run(300, {60.0: None, 120.0: 21.9})
    # Then silence: the engine's own sensor_stale fault.
    rig.run(record["observed"]["window_s"])
    assert record["end"] == END_OBSERVED
    assert record["flags"][0] == FLAG_READING_UNAVAILABLE
    assert "fault:sensor_stale" in record["flags"] and not record["clean"]
    # Only the reading at the stop came before the first perturbation.
    assert record["observed"]["max"] == 21.9 and record["observed"]["max_before_perturbation"] == 21.85


def test_report_gap_is_flagged(monkeypatch) -> None:
    monkeypatch.setattr(ledger_module, "OBSERVE_MIN_S", 3 * 3600.0)
    rig = Rig()
    record = rig.heat_one_burst()
    assert record["observed"]["window_s"] == 3 * 3600
    rig.run(3 * 3600 + 60, {600.0: 22.0, 3600.0: 22.1})  # in band: no new burst
    assert record["end"] == END_OBSERVED
    assert record["observed"]["max_report_gap_s"] >= 3 * 3600 - 3600 - 30
    # The last report was the maximum: nothing after it confirms the peak.
    assert record["flags"] == [FLAG_SENSOR_GAP, FLAG_PEAK_UNCONFIRMED] and not record["clean"]


def test_heat_that_never_starts() -> None:
    rig = Rig(reading=21.0)
    # Valves open but the boiler never fires: no heat flows.
    rig.core.on_actuators(rig.t, 1.0, False)
    t_end = rig.t + 20 * 60
    while rig.t < t_end:
        rig.t += 30
        status = rig.core.update(rig.t)
        rig.ledger.observe(rig.t, rig.core, status)
    record = rig.ledger.cycles[0]
    assert record["end"] == END_NO_HEAT and FLAG_NO_HEAT in record["flags"]
    assert record["heat_started_at"] is None and record["stop"]["reason"] == "heat did not start"


def test_restart_closes_open_record_and_keeps_history() -> None:
    rig = Rig()
    done = rig.heat_one_burst()
    rig.run(done["observed"]["window_s"] + 60, _rising_then_falling())
    rig.run(4 * 3600)  # fallback lock-out over, reading back in band: idle
    rig.read(21.8)
    rig.run(120)
    observing = rig.ledger.open_record
    assert observing is not None
    rig.ledger.flush(rig.t, rig.core)
    stored = json.loads(json.dumps(rig.ledger.to_dict()))
    restored = CycleLedger(stored, now=rig.t + 600)
    assert restored.take_dirty()
    first, second = restored.cycles
    assert first == json.loads(json.dumps(done))
    assert second["state"] == STATE_CLOSED and second["end"] == END_RESTART
    assert second["flags"][-1] == FLAG_RESTART and not second["clean"]
    assert second["stop"]["commanded_s"] is not None
    assert restored.next_id == second["id"] + 1


def test_retention_is_bounded_by_count_and_age() -> None:
    now = T0 + 30 * 86400
    old = [{"id": i, "state": STATE_CLOSED, "closed_at": iso(T0 + i * 60)} for i in range(1, 51)]
    recent = [{"id": 100 + i, "state": STATE_CLOSED, "closed_at": iso(now - 3600 + i)} for i in range(150)]
    ledger = CycleLedger({"format": 1, "next_id": 7, "cycles": old + recent}, now=now)
    assert len(ledger.cycles) == MAX_CYCLES
    assert all(r["id"] >= 100 for r in ledger.cycles)
    assert ledger.cycles[-1]["id"] == 249 and ledger.next_id == 250
    # Bounded while running too.
    rig = Rig(reading=20.0)
    rig.ledger = CycleLedger(None, now=rig.t, max_cycles=3)
    for i in range(16):
        rig.read(20.0 + 0.1 * (i % 2))  # keeps the sensor from looking stuck
        rig.run(1800)
    assert len(rig.ledger.cycles) == 3
    assert rig.ledger.next_id > 4
    assert len(json.dumps(rig.ledger.to_dict())) < 3 * 2500


def test_corrupt_store_is_ignored() -> None:
    ledger = CycleLedger({"cycles": "nonsense", "next_id": "x"}, now=T0)
    assert ledger.cycles == [] and ledger.next_id == 1
    ledger = CycleLedger({"cycles": [1, None, {"id": 4, "state": STATE_CLOSED}]}, now=T0)
    assert [r["id"] for r in ledger.cycles] == [4] and ledger.next_id == 5
