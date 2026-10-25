"""Basic setup and a full heating burst."""

from __future__ import annotations

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant

from .conftest import advance, heat_flowing, setup_zone, zone_data


async def test_burst_cycle(hass: HomeAssistant, plant, freezer) -> None:
    hass.states.async_set("sensor.t1", "20.0")
    hass.states.async_set("sensor.outdoor", "5.0")
    hass.states.async_set("valve.v1", "closed")
    hass.states.async_set("switch.centrala", "off")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    state = hass.states.get("climate.dormitor")
    assert state is not None
    assert state.state == HVACMode.OFF
    assert state.attributes["fault"] is None
    assert state.attributes["fault_message"] is None

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.dormitor", "hvac_mode": "heat"}, blocking=True
    )
    # The service returns immediately; the state reflects the intent at once.
    assert hass.states.get("climate.dormitor").state == HVACMode.HEAT
    await hass.async_block_till_done()
    assert plant.commands("valve.v1") == ["open_valve"]
    await advance(hass, freezer, 40)
    assert hass.states.get("valve.v1").state == "open"
    # The boiler was switched off just now: its minimum off time (180 s,
    # anti short-cycling) delays the start; the burst waits for real heat.
    assert hass.states.get("switch.centrala").state == "off"
    await advance(hass, freezer, 160)
    assert hass.states.get("switch.centrala").state == "on"
    assert hass.states.get("climate.dormitor").attributes["hvac_action"] == HVACAction.HEATING
    # Fallback burst for a 2 K deficit is ~21 min; run past it.
    await advance(hass, freezer, 25 * 60, step=30)
    assert not heat_flowing(hass, "valve.v1")
    assert hass.states.get("switch.centrala").state == "off"
    # Pump overrun: the valve closes after the turn-off delay.
    await advance(hass, freezer, 150, step=10)
    assert hass.states.get("valve.v1").state == "closed"
    assert hass.states.get("climate.dormitor").attributes["hvac_action"] == HVACAction.IDLE
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
