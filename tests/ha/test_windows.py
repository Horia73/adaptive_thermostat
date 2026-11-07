"""Window contacts and linked window zones through Home Assistant (2.1)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.adaptive_thermostat.const import (
    CONF_DOOR_WINDOW_SENSOR,
    CONF_LINKED_WINDOW_ZONES,
    CONF_MAX_BURST,
    CONF_WINDOW_DETECTION_ENABLED,
)

from .conftest import FakePlant, advance, heat_flowing, setup_zone, zone_data
from .test_scenarios import _base_states, _heat


def _attrs(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    assert state is not None
    return dict(state.attributes)


def _zone(name: str, n: int, **extra: Any) -> dict[str, Any]:
    return zone_data(name, valves=[f"valve.v{n}"], sensor=f"sensor.t{n}", **extra)


def _states(hass: HomeAssistant, *ns: int) -> None:
    _base_states(hass)
    for n in ns:
        hass.states.async_set(f"sensor.t{n}", "20.0")
        hass.states.async_set(f"valve.v{n}", "closed")


async def test_multiple_contacts_open_close(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _states(hass, 1)
    hass.states.async_set("binary_sensor.w1", "off")
    hass.states.async_set("binary_sensor.w2", "off")
    entry = await setup_zone(
        hass,
        _zone("Living", 1, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1", "binary_sensor.w2"]}),
    )
    rt = entry.runtime_data
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.living")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1")
    attrs = _attrs(hass, "climate.living")
    assert attrs["window_source"] == "contact"
    assert attrs["door_window_sensors"] == ["binary_sensor.w1", "binary_sensor.w2"]
    assert attrs["door_window_sensor"] == "binary_sensor.w1"

    # The second contact opens: demand drops at once, the boiler stops.
    hass.states.async_set("binary_sensor.w2", "on")
    await advance(hass, freezer, 2, step=1)
    assert not rt.status.want_heat
    assert hass.states.get("switch.centrala").state == "off"
    attrs = _attrs(hass, "climate.living")
    assert attrs["window_open_detected"] is True
    assert attrs["window_alert"] is None  # nothing to dismiss
    assert attrs["control_reason"] == "window open (contact)"
    assert attrs["learning"] is False
    await advance(hass, freezer, 200, step=10)
    assert hass.states.get("valve.v1").state == "closed"
    # Still open long after the detector's 2 h auto-expiry would have fired.
    await advance(hass, freezer, 2.5 * 3600, step=60)
    assert _attrs(hass, "climate.living")["window_open_detected"] is True
    assert hass.states.get("valve.v1").state == "closed"

    # One closes, the other is still open: still blocked.
    hass.states.async_set("binary_sensor.w1", "on")
    hass.states.async_set("binary_sensor.w2", "off")
    await advance(hass, freezer, 5, step=1)
    assert _attrs(hass, "climate.living")["window_open_detected"] is True
    # All closed: control resumes right away (the valve is commanded open).
    hass.states.async_set("binary_sensor.w1", "off")
    await advance(hass, freezer, 2, step=1)
    assert _attrs(hass, "climate.living")["window_open_detected"] is False
    assert rt.status.want_heat
    assert plant.commands("valve.v1")[-1] == "open_valve"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_contact_independent_of_window_detection(
    hass: HomeAssistant, plant: FakePlant, freezer
) -> None:
    _states(hass, 1)
    hass.states.async_set("binary_sensor.w1", "off")
    # A 2.0 entry: single string, autodetect off.
    entry = await setup_zone(
        hass, _zone("Baie", 1, window=False, **{CONF_DOOR_WINDOW_SENSOR: "binary_sensor.w1"})
    )
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.baie")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v1")
    hass.states.async_set("binary_sensor.w1", "on")
    await advance(hass, freezer, 2, step=1)
    assert not entry.runtime_data.status.want_heat
    assert hass.states.get("switch.centrala").state == "off"
    assert _attrs(hass, "climate.baie")["window_open_detected"] is True
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_contacts_fall_back_to_autodetect(
    hass: HomeAssistant, plant: FakePlant, freezer
) -> None:
    _base_states(hass, "21.6")
    hass.states.async_set("binary_sensor.w1", "off")
    hass.states.async_set("binary_sensor.w2", "unknown")
    entry = await setup_zone(
        hass,
        _zone("Bucatarie", 1, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1", "binary_sensor.w2"]}),
    )
    await _heat(hass, "climate.bucatarie")
    await advance(hass, freezer, 60)
    attrs = _attrs(hass, "climate.bucatarie")
    assert attrs["window_source"] == "contact"
    assert attrs["window_contacts_unavailable"] is False
    hass.states.async_set("binary_sensor.w1", "unavailable")
    await advance(hass, freezer, 5)
    attrs = _attrs(hass, "climate.bucatarie")
    assert attrs["window_source"] == "autodetect"
    assert attrs["window_contacts_unavailable"] is True
    assert attrs["window_open_detected"] is False
    # The slope detector works again: a sharp, sustained drop.
    for value in ("21.4", "21.2", "21.0", "20.8"):
        await advance(hass, freezer, 180, step=30)
        hass.states.async_set("sensor.t1", value)
    await advance(hass, freezer, 10)
    attrs = _attrs(hass, "climate.bucatarie")
    assert attrs["window_open_detected"] is True
    assert attrs["window_alert"].startswith("Open window detected")
    assert attrs["control_reason"] == "window open (autodetect)"
    # A contact reports again (closed): it is authoritative.
    hass.states.async_set("binary_sensor.w2", "off")
    await advance(hass, freezer, 5)
    attrs = _attrs(hass, "climate.bucatarie")
    assert attrs["window_open_detected"] is False
    assert attrs["window_source"] == "contact"
    assert attrs["window_contacts_unavailable"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_contacts_without_autodetect(
    hass: HomeAssistant, plant: FakePlant, freezer
) -> None:
    _states(hass, 1)
    hass.states.async_set("binary_sensor.w1", "unavailable")
    entry = await setup_zone(
        hass, _zone("Baie", 1, window=False, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1"]})
    )
    await advance(hass, freezer, 30)
    attrs = _attrs(hass, "climate.baie")
    assert attrs["window_source"] == "none"
    assert attrs["window_contacts_unavailable"] is True
    assert attrs["window_open_detected"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_linked_window_open_close(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _states(hass, 1, 2)
    hass.states.async_set("binary_sensor.w1", "off")
    living = await setup_zone(hass, _zone("Living", 1, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1"]}))
    coridor = await setup_zone(hass, _zone("Coridor", 2, **{CONF_LINKED_WINDOW_ZONES: ["climate.living"]}))
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.coridor")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v2")
    attrs = _attrs(hass, "climate.coridor")
    assert attrs["linked_window_open"] is False
    assert attrs["linked_window_open_zones"] == []
    assert attrs["linked_window_zones"] == ["climate.living"]

    hass.states.async_set("binary_sensor.w1", "on")
    await advance(hass, freezer, 2, step=1)
    assert not coridor.runtime_data.status.want_heat
    assert hass.states.get("switch.centrala").state == "off"
    attrs = _attrs(hass, "climate.coridor")
    assert attrs["linked_window_open"] is True
    assert attrs["linked_window_open_zones"] == ["climate.living"]
    assert attrs["window_open_detected"] is False  # own state stays own
    assert attrs["control_reason"] == "linked window open: climate.living"
    assert attrs["learning"] is False
    assert _attrs(hass, "climate.living")["window_open_detected"] is True
    await advance(hass, freezer, 200, step=10)
    assert hass.states.get("valve.v2").state == "closed"

    hass.states.async_set("binary_sensor.w1", "off")
    await advance(hass, freezer, 2, step=1)
    attrs = _attrs(hass, "climate.coridor")
    assert attrs["linked_window_open"] is False
    # Normal control again; the burst that was cut short keeps its usual
    # lock-out before the next one.
    assert coridor.runtime_data.status.reason == "lock-out after burst"
    await advance(hass, freezer, 35 * 60, step=30)
    assert coridor.runtime_data.status.want_heat or heat_flowing(hass, "valve.v2")
    assert await hass.config_entries.async_unload(coridor.entry_id)
    assert await hass.config_entries.async_unload(living.entry_id)


async def test_linked_state_is_not_transitive(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """A -> B -> C: A's window blocks B only; B and C link each other (a cycle)."""
    _states(hass, 1, 2, 3)
    hass.states.async_set("binary_sensor.w1", "off")
    a = await setup_zone(hass, _zone("Living", 1, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1"]}))
    b = await setup_zone(
        hass, _zone("Coridor", 2, **{CONF_LINKED_WINDOW_ZONES: ["climate.living", "climate.hol"]})
    )
    c = await setup_zone(hass, _zone("Hol", 3, **{CONF_LINKED_WINDOW_ZONES: ["climate.coridor"]}))
    await advance(hass, freezer, 30)
    hass.states.async_set("binary_sensor.w1", "on")
    await advance(hass, freezer, 5, step=1)
    assert _attrs(hass, "climate.coridor")["linked_window_open"] is True
    hol = _attrs(hass, "climate.hol")
    assert hol["linked_window_open"] is False
    assert hol["window_open_detected"] is False
    hass.states.async_set("binary_sensor.w1", "off")
    await advance(hass, freezer, 5, step=1)
    assert _attrs(hass, "climate.coridor")["linked_window_open"] is False
    for entry in (c, b, a):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_linked_zone_missing_at_startup(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _states(hass, 1, 2)
    hass.states.async_set("binary_sensor.w1", "on")
    # The linked zone is not loaded yet (restart order) or unavailable: ignored.
    hass.states.async_set("climate.living", "unavailable", {"window_open_detected": True})
    coridor = await setup_zone(hass, _zone("Coridor", 2, **{CONF_LINKED_WINDOW_ZONES: ["climate.living"]}))
    hass.states.async_remove("climate.living")
    await advance(hass, freezer, 200)
    await _heat(hass, "climate.coridor")
    await advance(hass, freezer, 60)
    assert heat_flowing(hass, "valve.v2")
    assert _attrs(hass, "climate.coridor")["linked_window_open"] is False
    # It loads later with its window open: picked up within seconds.
    living = await setup_zone(hass, _zone("Living", 1, **{CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1"]}))
    await advance(hass, freezer, 2, step=1)
    assert _attrs(hass, "climate.coridor")["linked_window_open_zones"] == ["climate.living"]
    assert not coridor.runtime_data.status.want_heat
    # Unloaded again (unavailable): treated as closed.
    assert await hass.config_entries.async_unload(living.entry_id)
    await advance(hass, freezer, 2, step=1)
    assert _attrs(hass, "climate.coridor")["linked_window_open"] is False
    assert not coridor.runtime_data.status.reason.startswith("linked")
    assert await hass.config_entries.async_unload(coridor.entry_id)


async def test_setup_flow_with_window_fields(hass: HomeAssistant) -> None:
    from homeassistant import config_entries

    from custom_components.adaptive_thermostat.const import DOMAIN

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Coridor"})
    hass.states.async_set("sensor.t1", "21.0", {"device_class": "temperature"})
    hass.states.async_set("sensor.outdoor", "5.0", {"device_class": "temperature"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "heater": ["valve.v1"],
            "temp_sensor": "sensor.t1",
            "outdoor_sensor": "sensor.outdoor",
            CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1", "binary_sensor.w2"],
            CONF_LINKED_WINDOW_ZONES: ["climate.living", "climate.bucatarie"],
        },
    )
    assert result["step_id"] == "auto_onoff_setup"
    for _ in range(3):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DOOR_WINDOW_SENSOR] == ["binary_sensor.w1", "binary_sensor.w2"]
    assert result["data"][CONF_LINKED_WINDOW_ZONES] == ["climate.living", "climate.bucatarie"]
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_only_window_fields(hass: HomeAssistant, plant: FakePlant) -> None:
    """REST-style reconfigure: ``{}`` everywhere except the window fields keeps the rest."""
    _states(hass, 1)
    hass.states.async_set("sensor.h1", "45")
    entry = await setup_zone(
        hass,
        _zone(
            "Hol",
            1,
            humidity_sensor="sensor.h1",
            backup_outdoor_sensor="sensor.outdoor2",
            **{CONF_DOOR_WINDOW_SENSOR: "binary_sensor.old", CONF_MAX_BURST: 33},
        ),
    )
    before = dict(entry.data)
    result = await entry.start_reconfigure_flow(hass)
    seen: list[str] = []
    while result["type"] is FlowResultType.FORM:
        seen.append(result["step_id"])
        user_input: dict[str, Any] = {}
        if result["step_id"] == "reconfigure_zone_setup":
            user_input = {
                CONF_DOOR_WINDOW_SENSOR: ["binary_sensor.w1", "binary_sensor.w2"],
                CONF_LINKED_WINDOW_ZONES: ["climate.living"],
            }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert seen == [
        "reconfigure_name_setup",
        "reconfigure_zone_setup",
        "reconfigure_timing_setup",
        "reconfigure_auto_onoff_setup",
        "reconfigure_adaptive_setup",
        "reconfigure_presets_setup",
    ]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    after = dict(entry.data)
    assert after.pop(CONF_DOOR_WINDOW_SENSOR) == ["binary_sensor.w1", "binary_sensor.w2"]
    assert after.pop(CONF_LINKED_WINDOW_ZONES) == ["climate.living"]
    before.pop(CONF_DOOR_WINDOW_SENSOR)
    # Everything else is unchanged; steps may add defaults for keys the entry lacked.
    for key, value in before.items():
        assert after[key] == value, key
    assert after[CONF_MAX_BURST] == 33
    assert after[CONF_WINDOW_DETECTION_ENABLED] is True
    rt = entry.runtime_data
    assert rt.door_window_sensor_ids == ["binary_sensor.w1", "binary_sensor.w2"]
    assert rt.linked_window_zone_ids == ["climate.living"]

    # A second untouched pass changes nothing at all.
    snapshot = dict(entry.data)
    result = await entry.start_reconfigure_flow(hass)
    while result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert dict(entry.data) == snapshot
    # Lists can be cleared explicitly.
    result = await entry.start_reconfigure_flow(hass)
    while result["type"] is FlowResultType.FORM:
        user_input = {}
        if result["step_id"] == "reconfigure_zone_setup":
            user_input = {CONF_LINKED_WINDOW_ZONES: []}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    assert entry.data[CONF_LINKED_WINDOW_ZONES] == []
    assert entry.runtime_data.linked_window_zone_ids == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_folds_legacy_options(hass: HomeAssistant, plant: FakePlant) -> None:
    """Options written by the 1.x options flow override data; reconfigure keeps and folds them."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.adaptive_thermostat.const import DOMAIN

    _states(hass, 1)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_zone("Hol", 1),
        options={CONF_MAX_BURST: 20, CONF_DOOR_WINDOW_SENSOR: "binary_sensor.w1"},
        title="Hol",
        unique_id="Hol",
        entry_id="entry_hol",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.max_burst_s == 20 * 60
    result = await entry.start_reconfigure_flow(hass)
    while result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert dict(entry.options) == {}
    assert entry.data[CONF_MAX_BURST] == 20
    assert entry.data[CONF_DOOR_WINDOW_SENSOR] == ["binary_sensor.w1"]
    assert entry.runtime_data.max_burst_s == 20 * 60
    assert await hass.config_entries.async_unload(entry.entry_id)
