"""Cycle ledger in Home Assistant: persistence, diagnostics download, isolation."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.adaptive_thermostat.const import LEDGER_STORAGE_KEY
from custom_components.adaptive_thermostat.diagnostics import async_get_config_entry_diagnostics
from custom_components.adaptive_thermostat.engine.ledger import (
    END_RESTART,
    FLAG_FORCED_STOP,
    FLAG_MANUAL_OFF,
    FLAG_RESTART,
    STATE_CLOSED,
    STATE_HEATING,
    STATE_OBSERVING,
    CycleLedger,
)

from .conftest import FakePlant, advance, setup_zone, zone_data
from .test_scenarios import _base_states, _heat, _off


def _key(entry_id: str) -> str:
    return f"{LEDGER_STORAGE_KEY}_{entry_id}"


async def test_diagnostics_download(hass: HomeAssistant, hass_client: Any, plant: FakePlant) -> None:
    """The Home Assistant UI path (no time travel: the HTTP auth token must stay valid)."""
    _base_states(hass, "22.5")
    assert await async_setup_component(hass, "diagnostics", {})
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["schema"] == "adaptive_thermostat.cycle_ledger" and diag["format"] == 1
    assert diag["entity_id"] == "climate.dormitor" and diag["ledger_enabled"] is True
    assert diag["limits"]["max_cycles"] == 96 and diag["limits"]["max_age_days"] == 14.0
    assert diag["next_id"] == 1 and diag["cycles"] == []
    assert diag["config"]["temp_sensor"] == "sensor.t1"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cycle_survives_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any], plant: FakePlant, freezer
) -> None:
    _base_states(hass, "20.0")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 60)
    runtime = entry.runtime_data
    record = runtime.ledger.open_record
    assert record is not None and record["state"] == STATE_HEATING and record["id"] == 1
    assert record["start"]["mode"] == "fallback" and record["start"]["target"] == 22.0
    # Fallback burst runs its planned length, then the tail is observed.
    await advance(hass, freezer, 30 * 60, step=15)
    assert hass.states.get("valve.v1").state == "closed"
    assert record["state"] == STATE_OBSERVING
    stop = record["stop"]
    assert stop["reason"] == "planned length reached" and stop["forced"] is False
    assert abs(stop["heat_s"] - stop["planned_s"]) <= 30 and stop["commanded_s"] > stop["heat_s"]
    hass.states.async_set("sensor.t1", "22.0")  # in band: no new burst
    await advance(hass, freezer, 300, step=30)
    assert record["state"] == STATE_OBSERVING
    assert record["observed"]["max"] == 22.0 and record["observed"]["readings"] == 1

    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag)
    assert [c["id"] for c in diag["cycles"]] == [1]
    assert diag["cycles"][0]["stop"]["reason"] == "planned length reached"
    assert "predicted_peak" in diag["current"] and "control_reason" in diag["current"]

    # Reload (as an options change or a restart would): saved, then closed as interrupted.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[_key(entry.entry_id)]
    assert saved["version"] == 1
    json.dumps(saved)
    restored = entry.runtime_data.ledger
    assert restored is not runtime.ledger
    (old,) = restored.cycles
    assert old["id"] == 1 and old["state"] == STATE_CLOSED and old["end"] == END_RESTART
    assert old["flags"] == [FLAG_RESTART] and old["clean"] is False
    assert old["observed"]["max"] == 22.0
    assert restored.next_id == 2
    await advance(hass, freezer, 30)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await advance(hass, freezer, 30)  # let the fake valve finish its travel


async def test_manual_off_is_recorded(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "20.0")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 120)
    record = entry.runtime_data.ledger.open_record
    await _off(hass)
    await advance(hass, freezer, 30)
    assert record["stop"]["reason"] == "off" and record["stop"]["forced"] is True
    assert record["flags"][:2] == [FLAG_MANUAL_OFF, FLAG_FORCED_STOP]
    assert record["first_perturbation"] == FLAG_MANUAL_OFF
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_the_zone_deletes_its_ledger(
    hass: HomeAssistant, hass_storage: dict[str, Any], plant: FakePlant, freezer
) -> None:
    _base_states(hass, "20.0")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 60)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _key(entry.entry_id) in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _key(entry.entry_id) not in hass_storage


async def test_ledger_failure_never_touches_control(
    hass: HomeAssistant, plant: FakePlant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: CycleLedger, *args: Any) -> None:
        raise RuntimeError("ledger bug")

    monkeypatch.setattr(CycleLedger, "observe", broken)
    _base_states(hass, "20.0")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 60)
    assert entry.runtime_data.ledger is None  # disabled at the first error
    assert hass.states.get("valve.v1").state == "open"
    assert hass.states.get("switch.centrala").state == "on"
    await advance(hass, freezer, 30 * 60, step=15)
    assert hass.states.get("valve.v1").state == "closed"
    assert plant.commands("valve.v1") == ["open_valve", "close_valve"]
    assert await hass.config_entries.async_unload(entry.entry_id)
