"""Home Assistant level scenarios from the field incidents."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util
import pytest

from custom_components.adaptive_thermostat.const import DOMAIN

from .conftest import FakePlant, advance, heat_flowing, setup_zone, zone_data


class HeatAudit:
    """Records every moment a zone receives heat while its mode is OFF."""

    def __init__(self, hass: HomeAssistant, zones: dict[str, str], boiler: str = "switch.centrala") -> None:
        self.hass = hass
        self.zones = zones  # climate entity -> valve
        self.boiler = boiler
        self.violations: list[tuple[float, str]] = []
        self.off_since: dict[str, float] = {}
        self._unsub = hass.bus.async_listen("state_changed", self._changed)

    @callback
    def _changed(self, event: Event) -> None:
        now = dt_util.utcnow().timestamp()
        for climate, valve in self.zones.items():
            state = self.hass.states.get(climate)
            if state is None:
                continue
            if state.state == HVACMode.OFF:
                self.off_since.setdefault(climate, now)
            else:
                self.off_since.pop(climate, None)
            since = self.off_since.get(climate)
            # Allow the actuators' own reaction time (commands are async).
            if since is not None and now - since > 2.0 and heat_flowing(self.hass, valve, self.boiler):
                self.violations.append((now, climate))

    def close(self) -> None:
        self._unsub()


def _base_states(hass: HomeAssistant, temp: str = "20.0") -> None:
    hass.states.async_set("sensor.t1", temp)
    hass.states.async_set("sensor.outdoor", "5.0")
    hass.states.async_set("valve.v1", "closed")
    hass.states.async_set("switch.centrala", "off")


async def _heat(hass: HomeAssistant, entity: str = "climate.dormitor") -> None:
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": "heat"}, blocking=True
    )


async def _off(hass: HomeAssistant, entity: str | list[str] = "climate.dormitor") -> None:
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": "off"}, blocking=True
    )


async def test_off_while_valve_opening(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field incident: OFF during valve travel was ignored, heat started anyway."""
    _base_states(hass)
    audit = HeatAudit(hass, {"climate.dormitor": "valve.v1"})
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)  # let the boiler minimum off time pass
    await _heat(hass)
    await advance(hass, freezer, 5)
    assert hass.states.get("valve.v1").state == "opening"
    await _off(hass)
    state = hass.states.get("climate.dormitor")
    assert state.state == HVACMode.OFF
    assert state.attributes["hvac_action"] == HVACAction.OFF
    await advance(hass, freezer, 30 * 60, step=10)
    assert "turn_on" not in plant.commands("switch.centrala")
    assert hass.states.get("valve.v1").state == "closed"
    assert hass.states.get("climate.dormitor").attributes["zone_heater_on"] is False
    assert audit.violations == []
    audit.close()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_off_after_heat_started(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """OFF while heat flows: boiler off at once (forced, no minimum on time)."""
    _base_states(hass)
    audit = HeatAudit(hass, {"climate.dormitor": "valve.v1"})
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 40)
    assert heat_flowing(hass, "valve.v1")
    await _off(hass)
    await advance(hass, freezer, 5)
    assert hass.states.get("switch.centrala").state == "off"
    await advance(hass, freezer, 200)
    assert hass.states.get("valve.v1").state == "closed"
    assert audit.violations == []
    audit.close()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rapid_set_temperature(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field incident: ~30 +/- taps produced ~30 concurrent turn-ons and leaked timers."""
    _base_states(hass)
    entry = await setup_zone(hass, zone_data("Dressing", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    starts: list[Any] = []
    unsub = hass.bus.async_listen(
        "logbook_entry", lambda e: starts.append(e.data["message"]) if e.data.get("message") else None
    )
    await _heat(hass, "climate.dressing")
    for i in range(30):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.dressing", "temperature": 22.0 + (0.1 if i % 2 else -0.1)},
            blocking=True,
        )
    await advance(hass, freezer, 60)
    assert plant.commands("valve.v1") == ["open_valve"]
    assert plant.commands("switch.centrala").count("turn_on") == 1
    runtime = entry.runtime_data
    assert runtime.status is not None and runtime.status.burst_active
    # Exactly one control timer per zone.
    assert runtime._timer is not None
    # The fallback burst (~22 min) ends, the room has warmed: no new burst.
    await advance(hass, freezer, 22 * 60, step=30)
    hass.states.async_set("sensor.t1", "22.3")
    await advance(hass, freezer, 10 * 60, step=30)
    assert hass.states.get("valve.v1").state == "closed"
    assert plant.commands("valve.v1") == ["open_valve", "close_valve"]
    assert starts.count("started heating") == 1
    assert starts.count("stopped heating") == 1
    unsub()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_restart_with_valve_open(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field incident: restart forced zone_on False but never closed the valve."""
    _base_states(hass, "22.5")
    hass.states.async_set("valve.v1", "open")
    hass.states.async_set("switch.centrala", "on")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 5)
    # Boiler stops at once; the valve stays open for the pump overrun only.
    assert hass.states.get("switch.centrala").state == "off"
    assert hass.states.get("valve.v1").state == "open"
    await advance(hass, freezer, 150, step=5)
    assert hass.states.get("valve.v1").state == "closed"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_with_valve_open_no_boiler(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """A valve open with the boiler off is closed straight away."""
    _base_states(hass, "22.5")
    hass.states.async_set("valve.v1", "open")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 25)
    assert hass.states.get("valve.v1").state == "closed"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_valve_reopens_after_unavailable(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field incident: valve.valva_baie_2 came back open by itself and stayed open 18 h."""
    _base_states(hass, "22.5")
    entry = await setup_zone(hass, zone_data("Baie 2", valves=["valve.v1"], sensor="sensor.t1"))
    await _heat(hass, "climate.baie_2")
    await advance(hass, freezer, 60)
    assert hass.states.get("valve.v1").state == "closed"
    # Gateway blip: unavailable for 60 s, then the valve reports open.
    hass.states.async_set("valve.v1", "unavailable")
    await advance(hass, freezer, 60)
    assert hass.states.get("climate.baie_2").attributes["fault"] is None  # within grace
    hass.states.async_set("valve.v1", "open")
    await advance(hass, freezer, 25)
    assert plant.commands("valve.v1")[-1] == "close_valve"
    assert hass.states.get("valve.v1").state == "closed"
    attrs = hass.states.get("climate.baie_2").attributes
    assert attrs["valve_error"] is not None and "without a command" in attrs["valve_error"]
    # A long outage is a fault and blocks heating.
    hass.states.async_set("valve.v1", "unavailable")
    await advance(hass, freezer, 120)
    attrs = hass.states.get("climate.baie_2").attributes
    assert attrs["fault"] == "valve_unavailable"
    assert attrs["fault_message"]
    hass.states.async_set("valve.v1", "closed")
    await advance(hass, freezer, 10)
    assert hass.states.get("climate.baie_2").attributes["fault"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_sensor_by_time(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "21.0")
    entry = await setup_zone(hass, zone_data("Birou", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    # Idle and silent: report-on-change sensors are often quiet for hours, so
    # an idle zone tolerates twice the timeout (3 h default).
    await advance(hass, freezer, 3 * 3600 + 60, step=300)
    assert hass.states.get("climate.birou").attributes["fault"] is None
    await advance(hass, freezer, 3 * 3600, step=300)
    attrs = hass.states.get("climate.birou").attributes
    assert attrs["fault"] == "sensor_stale"
    assert "No temperature update" in attrs["fault_message"]
    await _heat(hass, "climate.birou")
    await advance(hass, freezer, 120)
    assert "open_valve" not in plant.commands("valve.v1")
    # A heartbeat (same value, Home Assistant only updates last_reported) clears it.
    hass.states.async_set("sensor.t1", "21.0", force_update=False)
    await advance(hass, freezer, 5)
    hass.states.async_set("sensor.t1", "21.0")
    await advance(hass, freezer, 5)
    assert hass.states.get("climate.birou").attributes["fault"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_sensor_by_physics(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field incident: sensor.birou frozen at 22.9 °C for 142 h while heating 12 h/day."""
    _base_states(hass, "22.0")
    entry = await setup_zone(
        hass,
        zone_data("Birou", valves=["valve.v1"], sensor="sensor.t1", home_preset=23.0),
    )
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.birou")
    heated_s = 0.0
    fault = None
    for _ in range(int(4 * 3600 / 30)):
        # The sensor keeps sending the same frozen value (heartbeat).
        hass.states.async_set("sensor.t1", "22.0", force_update=True)
        await advance(hass, freezer, 30, step=30)
        if heat_flowing(hass, "valve.v1"):
            heated_s += 30
        fault = hass.states.get("climate.birou").attributes["fault"]
        if fault:
            break
    assert fault == "sensor_stale"
    assert "stuck" in hass.states.get("climate.birou").attributes["fault_message"]
    # Detected after two unanswered heat episodes (~80 min of heat); the field
    # incident delivered 11.2 heat-hours into the frozen reading.
    assert heated_s <= 90 * 60
    await advance(hass, freezer, 300)
    assert not heat_flowing(hass, "valve.v1")
    # The reading moves again: fault clears and control resumes.
    hass.states.async_set("sensor.t1", "21.8")
    await advance(hass, freezer, 10)
    assert hass.states.get("climate.birou").attributes["fault"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sensor_unavailable(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass)
    entry = await setup_zone(hass, zone_data("Living", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.living")
    await advance(hass, freezer, 40)
    assert heat_flowing(hass, "valve.v1")
    hass.states.async_set("sensor.t1", "unavailable")
    await advance(hass, freezer, 310)
    attrs = hass.states.get("climate.living").attributes
    assert attrs["fault"] == "sensor_unavailable"
    assert not heat_flowing(hass, "valve.v1")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_zones_stop_simultaneously(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Field bug: two zones stopping together kept the shared boiler on."""
    _base_states(hass)
    hass.states.async_set("sensor.t2", "20.0")
    hass.states.async_set("valve.v2", "closed")
    e1 = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    e2 = await setup_zone(hass, zone_data("Birou", valves=["valve.v2"], sensor="sensor.t2"))
    await advance(hass, freezer, 200)
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": ["climate.hol", "climate.birou"], "hvac_mode": "heat"},
        blocking=True,
    )
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1") and heat_flowing(hass, "valve.v2")
    assert plant.commands("switch.centrala").count("turn_on") == 1
    # Both bursts are identical and end in the same control tick.
    await advance(hass, freezer, 25 * 60, step=15)
    assert hass.states.get("switch.centrala").state == "off"
    # The last zone keeps its valve open for the pump overrun, then closes.
    await advance(hass, freezer, 130, step=5)
    assert hass.states.get("valve.v1").state == "closed"
    assert hass.states.get("valve.v2").state == "closed"
    assert plant.commands("switch.centrala").count("turn_off") >= 1
    coordinator = hass.data[DOMAIN]["coordinators"]["switch.centrala"]
    assert coordinator.policy.on is False
    assert await hass.config_entries.async_unload(e1.entry_id)
    assert await hass.config_entries.async_unload(e2.entry_id)
    assert "switch.centrala" not in hass.data[DOMAIN]["coordinators"]


async def test_two_zones_forced_off_together(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass)
    hass.states.async_set("sensor.t2", "20.0")
    hass.states.async_set("valve.v2", "closed")
    audit = HeatAudit(hass, {"climate.hol": "valve.v1", "climate.birou": "valve.v2"})
    e1 = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    e2 = await setup_zone(hass, zone_data("Birou", valves=["valve.v2"], sensor="sensor.t2"))
    await advance(hass, freezer, 200)
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": ["climate.hol", "climate.birou"], "hvac_mode": "heat"},
        blocking=True,
    )
    await advance(hass, freezer, 60)
    await _off(hass, ["climate.hol", "climate.birou"])
    await advance(hass, freezer, 5)
    assert hass.states.get("switch.centrala").state == "off"
    await advance(hass, freezer, 150)
    assert hass.states.get("valve.v1").state == "closed"
    assert hass.states.get("valve.v2").state == "closed"
    assert audit.violations == []
    audit.close()
    assert await hass.config_entries.async_unload(e1.entry_id)
    assert await hass.config_entries.async_unload(e2.entry_id)


async def test_new_demand_cancels_idle_overrun(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """A zone switched OFF must not get heat when another zone starts during its overrun."""
    _base_states(hass)
    hass.states.async_set("sensor.t2", "22.5")
    hass.states.async_set("valve.v2", "closed")
    audit = HeatAudit(hass, {"climate.hol": "valve.v1", "climate.birou": "valve.v2"})
    e1 = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    e2 = await setup_zone(hass, zone_data("Birou", valves=["valve.v2"], sensor="sensor.t2"))
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.hol")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1")
    await _off(hass, "climate.hol")
    await advance(hass, freezer, 5)
    assert hass.states.get("valve.v1").state == "open"  # overrun hold, boiler off
    # Birou now needs heat while Hol is in its overrun hold.
    hass.states.async_set("sensor.t2", "20.0")
    await _heat(hass, "climate.birou")
    await advance(hass, freezer, 400, step=5)
    assert hass.states.get("valve.v1").state == "closed"
    assert heat_flowing(hass, "valve.v2")
    assert audit.violations == []
    audit.close()
    assert await hass.config_entries.async_unload(e1.entry_id)
    assert await hass.config_entries.async_unload(e2.entry_id)


async def test_window_detection(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "21.6")
    entry = await setup_zone(hass, zone_data("Bucatarie", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.bucatarie")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1")
    # Sharp, sustained drop: 21.6 -> 20.8 in 12 minutes, four reports.
    for value in ("21.4", "21.2", "21.0", "20.8"):
        await advance(hass, freezer, 180, step=30)
        hass.states.async_set("sensor.t1", value)
    await advance(hass, freezer, 10)
    attrs = hass.states.get("climate.bucatarie").attributes
    assert attrs["window_open_detected"] is True
    assert attrs["window_alert"].startswith("Open window detected")
    await advance(hass, freezer, 30)
    assert not heat_flowing(hass, "valve.v1")
    # User says it was not a window.
    await hass.services.async_call(
        DOMAIN, "dismiss_window_alert", {"entity_id": "climate.bucatarie"}, blocking=True
    )
    await advance(hass, freezer, 5)
    attrs = hass.states.get("climate.bucatarie").attributes
    assert attrs["window_open_detected"] is False
    assert attrs["window_alert"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_quantisation_step_is_not_a_window(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """A single 0.2 °C step a minute after the last report used to read as 12 °C/h."""
    _base_states(hass, "22.0")
    entry = await setup_zone(hass, zone_data("Bucatarie", valves=["valve.v1"], sensor="sensor.t1"))
    await _heat(hass, "climate.bucatarie")
    await advance(hass, freezer, 600, step=30)
    hass.states.async_set("sensor.t1", "21.8")
    await advance(hass, freezer, 60)
    hass.states.async_set("sensor.t1", "21.9")
    await advance(hass, freezer, 600, step=30)
    assert hass.states.get("climate.bucatarie").attributes["window_open_detected"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_reload_no_leaks(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Unload mid-burst: boiler off, no tasks/timers left (verify_cleanup checks)."""
    _base_states(hass)
    entry = await setup_zone(hass, zone_data("Dormitor", valves=["valve.v1"], sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    await _heat(hass)
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.centrala").state == "off"
    assert "switch.centrala" not in hass.data[DOMAIN]["coordinators"]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("climate.dormitor").state == HVACMode.HEAT
    await advance(hass, freezer, 200)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("climate.dormitor").state == HVACMode.HEAT
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("valves", [["valve.v1", "valve.v2"]])
async def test_two_valve_zone(hass: HomeAssistant, plant: FakePlant, freezer, valves: list[str]) -> None:
    """Dormitor has two valves: heat is confirmed only when both are open."""
    _base_states(hass)
    hass.states.async_set("valve.v2", "closed")
    entry = await setup_zone(hass, zone_data("Dormitor", valves=valves, sensor="sensor.t1"))
    await advance(hass, freezer, 200)
    plant.stuck.add("valve.v2")
    await _heat(hass)
    await advance(hass, freezer, 60)
    # One valve stuck closed: no boiler, and after retries a mismatch fault.
    assert hass.states.get("switch.centrala").state == "off"
    await advance(hass, freezer, 4 * 60, step=10)
    attrs = hass.states.get("climate.dormitor").attributes
    assert attrs["fault"] == "valve_mismatch"
    assert attrs["valve_error"]
    assert hass.states.get("switch.centrala").state == "off"
    plant.stuck.discard("valve.v2")
    assert await hass.config_entries.async_unload(entry.entry_id)
