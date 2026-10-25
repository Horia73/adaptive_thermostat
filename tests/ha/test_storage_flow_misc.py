"""Storage migration, config flow, services, recorder footprint, auto on/off."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adaptive_thermostat.const import (
    CONF_AUTO_OFF_TEMP,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_CENTRAL_HEATER_MIN_OFF,
    CONF_CENTRAL_HEATER_MIN_ON,
    CONF_MAX_BURST,
    CONF_MIN_BURST,
    CONF_MODEL_CONTROL,
    CONF_SENSOR_STALE_TIMEOUT,
    DOMAIN,
    STORAGE_STATE_KEY,
)

from .conftest import FakePlant, advance, setup_zone, zone_data
from .test_scenarios import _base_states

V1_ENTRY = {
    "hvac_mode": "heat",
    "target_temperature": 22.5,
    "current_preset": "home",
    "manual_override": True,
    "zone_heater_on": True,
    "thermal_state": {
        "params": {"tau_r": 138.53, "tau_th": 408.91, "K": 0.2, "p": 1.0},
        "ema_outdoor": 9.1,
        "ref_outdoor": 9.0,
        "last_good_on": 2700.0,
        "min_on_override": 142.4,
    },
    "preset_targets": {"sleep": 20.5, "home": 22.5, "away": 17.0},
}


async def test_storage_migration_from_v1(
    hass: HomeAssistant, hass_storage: dict[str, Any], plant: FakePlant, freezer
) -> None:
    """1.1.6 state: keep mode/target/presets/override, drop the old learned params."""
    hass_storage[STORAGE_STATE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_STATE_KEY,
        "data": {"entry_birou": V1_ENTRY, "entry_gone": {"hvac_mode": "off"}},
    }
    _base_states(hass, "22.6")
    entry = await setup_zone(hass, zone_data("Birou", valves=["valve.v1"], sensor="sensor.t1"))
    state = hass.states.get("climate.birou")
    assert state.state == HVACMode.HEAT
    assert state.attributes["temperature"] == 22.5
    assert state.attributes["preset_mode"] == "home"
    assert state.attributes["manual_override"] is True
    assert state.attributes["zone_heater_on"] is False
    # The learned model restarts from the prior.
    assert entry.runtime_data.core.estimator.stats.bursts_learned == 0
    assert entry.runtime_data.presets["sleep"] == 20.5
    await advance(hass, freezer, 30)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[STORAGE_STATE_KEY]
    assert saved["version"] == 1
    assert saved["minor_version"] == 2
    zone = saved["data"]["entry_birou"]
    assert "thermal_state" not in zone
    assert "zone_heater_on" not in zone
    assert zone["hvac_mode"] == "heat"
    assert zone["core"]["estimator"]["format"] >= 1
    assert saved["data"]["entry_gone"] == {"hvac_mode": "off", "core": None}


async def test_rollback_old_code_can_read_new_store(
    hass: HomeAssistant, hass_storage: dict[str, Any], plant: FakePlant, freezer
) -> None:
    """A 1.1.x install (Store version 1, minor 1) must still load 2.0 data."""
    _base_states(hass, "22.6")
    entry = await setup_zone(hass, zone_data("Birou", valves=["valve.v1"], sensor="sensor.t1"))
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.birou", "hvac_mode": "heat"}, blocking=True
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_STATE_KEY]["minor_version"] == 2
    old_store: Store[dict[str, Any]] = Store(hass, 1, STORAGE_STATE_KEY)
    data = await old_store.async_load()
    assert data is not None
    # Exactly the keys 1.1.6 reads are present and valid.
    zone = data["entry_birou"]
    assert zone["hvac_mode"] == "heat"
    assert isinstance(zone["target_temperature"], float)
    assert zone["current_preset"] == "home"
    assert zone["manual_override"] is False
    assert set(zone["preset_targets"]) == {"sleep", "home", "away"}


async def test_corrupt_core_state_starts_fresh(
    hass: HomeAssistant, hass_storage: dict[str, Any], plant: FakePlant
) -> None:
    hass_storage[STORAGE_STATE_KEY] = {
        "version": 1,
        "minor_version": 2,
        "key": STORAGE_STATE_KEY,
        "data": {
            "entry_birou": {
                "hvac_mode": "heat",
                "target_temperature": 21.0,
                "core": {"estimator": {"format": 4, "x": ["nan"], "p": []}},
            }
        },
    }
    _base_states(hass, "22.6")
    entry = await setup_zone(hass, zone_data("Birou", valves=["valve.v1"], sensor="sensor.t1"))
    assert hass.states.get("climate.birou").attributes["temperature"] == 21.0
    assert entry.runtime_data.core.confidence() == 0.0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_creates_entry_with_new_defaults(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Coridor"})
    assert result["step_id"] == "zone_setup"
    hass.states.async_set("sensor.t1", "21.0", {"device_class": "temperature"})
    hass.states.async_set("sensor.outdoor", "5.0", {"device_class": "temperature"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "heater": ["valve.v1"],
            "central_heater": "switch.centrala",
            "temp_sensor": "sensor.t1",
            "outdoor_sensor": "sensor.outdoor",
        },
    )
    assert result["step_id"] == "timing_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auto_onoff_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "adaptive_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "presets_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_CENTRAL_HEATER_MIN_ON] == 120
    assert data[CONF_CENTRAL_HEATER_MIN_OFF] == 180
    assert data[CONF_SENSOR_STALE_TIMEOUT] == 180
    assert data[CONF_MIN_BURST] == 120
    assert data[CONF_MAX_BURST] == 45
    assert data[CONF_MODEL_CONTROL] is True
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_old_entry_gets_defaults(hass: HomeAssistant, plant: FakePlant) -> None:
    """An entry created by 1.1.6 has none of the new keys; reconfigure offers defaults."""
    _base_states(hass, "22.6")
    entry = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_name_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Hol"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "heater": ["valve.v1"],
            "central_heater": "switch.centrala",
            "temp_sensor": "sensor.t1",
            "outdoor_sensor": "sensor.outdoor",
        },
    )
    assert result["step_id"] == "reconfigure_timing_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reconfigure_adaptive_setup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MAX_BURST: 30})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_MAX_BURST] == 30
    assert entry.data[CONF_MIN_BURST] == 120
    assert entry.runtime_data.max_burst_s == 30 * 60
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "22.6")
    entry = await setup_zone(
        hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1", **{CONF_AUTO_ON_OFF_ENABLED: True})
    )
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.hol", "hvac_mode": "off"}, blocking=True
    )
    assert hass.states.get("climate.hol").attributes["manual_override"] is True
    await hass.services.async_call(
        DOMAIN, "reset_manual_override", {"entity_id": "climate.hol"}, blocking=True
    )
    assert hass.states.get("climate.hol").attributes["manual_override"] is False
    await hass.services.async_call(
        DOMAIN, "reset_adaptive_profile", {"entity_id": "climate.hol"}, blocking=True
    )
    assert entry.runtime_data.core.estimator.stats.bursts_learned == 0
    # turn_on / turn_off and presets.
    await hass.services.async_call("climate", "turn_on", {"entity_id": "climate.hol"}, blocking=True)
    assert hass.states.get("climate.hol").state == HVACMode.HEAT
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": "climate.hol", "preset_mode": "away"}, blocking=True
    )
    attrs = hass.states.get("climate.hol").attributes
    assert attrs["preset_mode"] == "away" and attrs["temperature"] == 18.0
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.hol", "temperature": 17.5, "hvac_mode": "off"},
        blocking=True,
    )
    attrs = hass.states.get("climate.hol").attributes
    assert attrs["temperature"] == 17.5
    assert hass.states.get("climate.hol").state == HVACMode.OFF
    assert entry.runtime_data.presets["away"] == 17.5
    await advance(hass, freezer, 30)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_card_contract_and_recorder_footprint(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    """Card attributes are present; idle hours cause only a handful of state writes."""
    _base_states(hass, "22.6")
    hass.states.async_set("sensor.h1", "45")
    entry = await setup_zone(
        hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1", humidity_sensor="sensor.h1")
    )
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.hol", "hvac_mode": "heat"}, blocking=True
    )
    state = hass.states.get("climate.hol")
    for key in (
        "current_temperature",
        "hvac_action",
        "hvac_modes",
        "min_temp",
        "max_temp",
        "preset_mode",
        "preset_modes",
        "humidity_sensor",
        "outdoor_sensor",
        "window_open_detected",
        "window_alert",
        "valve_error",
        "temperature",
        "target_temp_step",
        "friendly_name",
        "fault",
        "fault_message",
    ):
        assert key in state.attributes, key
    assert state.attributes["humidity_sensor"] == "sensor.h1"
    assert state.attributes["current_humidity"] == 45
    unrecorded = state.state_info["unrecorded_attributes"]
    for key in (
        "thermal_params",
        "model_confidence",
        "predicted_peak",
        "heater_entity_ids",
        "control_reason",
    ):
        assert key in unrecorded
    for key in ("fault", "fault_message", "valve_error", "window_alert", "temperature"):
        assert key not in unrecorded
    assert "last_updated" not in state.attributes
    writes: list[Any] = []
    unsub = hass.bus.async_listen(
        "state_changed", lambda e: writes.append(e) if e.data["entity_id"] == "climate.hol" else None
    )
    await advance(hass, freezer, 2 * 3600, step=30)
    unsub()
    # 1.1.6 wrote this entity every ~18 s (~400 rows in 2 h); now only diagnostics
    # refreshes every 15 min while nothing card-visible changes.
    assert len(writes) <= 10
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auto_on_off(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "22.6")
    hass.states.async_set("sensor.outdoor", "20.0")
    entry = await setup_zone(
        hass,
        zone_data(
            "Hol",
            valves=["valve.v1"],
            sensor="sensor.t1",
            **{CONF_AUTO_ON_OFF_ENABLED: True, CONF_AUTO_ON_TEMP: 10.0, CONF_AUTO_OFF_TEMP: 18.0},
        ),
    )
    assert hass.states.get("climate.hol").state == HVACMode.OFF
    hass.states.async_set("sensor.outdoor", "8.0")
    await advance(hass, freezer, 3600, step=60)
    assert hass.states.get("climate.hol").state == HVACMode.HEAT
    hass.states.async_set("sensor.outdoor", "19.0")
    await advance(hass, freezer, 3600, step=60)
    assert hass.states.get("climate.hol").state == HVACMode.OFF
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_central_heater_unavailable(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "20.0")
    entry = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.hol", "hvac_mode": "heat"}, blocking=True
    )
    hass.states.async_set("switch.centrala", "unavailable")
    await advance(hass, freezer, 200)
    attrs = hass.states.get("climate.hol").attributes
    assert attrs["fault"] == "central_unavailable"
    assert attrs["valve_error"] == "Central heater unavailable"
    await advance(hass, freezer, 60)
    assert hass.states.get("valve.v1").state == "closed"
    hass.states.async_set("switch.centrala", "off")
    await advance(hass, freezer, 30)
    assert hass.states.get("climate.hol").attributes["fault"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostic_sensors(hass: HomeAssistant, plant: FakePlant, freezer) -> None:
    _base_states(hass, "21.0")
    entry = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    # Existing entity IDs (1.1.6 naming) are unchanged; new ones use clean names.
    for entity_id in (
        "sensor.hol_hol_temperature_slope",
        "sensor.hol_hol_hourly_temperature_slope",
        "sensor.hol_hol_cycle_on_duration",
        "sensor.hol_hol_cycle_tail_duration",
        "sensor.hol_hol_cycle_time_to_target",
        "binary_sensor.hol_hol_heater_active",
        "sensor.hol_model_confidence",
        "sensor.hol_predicted_peak",
        "sensor.hol_last_fault",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    for value in ("21.2", "21.4", "21.6"):
        await advance(hass, freezer, 600, step=60)
        hass.states.async_set("sensor.t1", value)
    await advance(hass, freezer, 5)
    slope = float(hass.states.get("sensor.hol_hol_temperature_slope").state)
    assert 0.5 < slope < 2.5
    assert hass.states.get("sensor.hol_last_fault").state == "none"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unique_ids_unchanged(hass: HomeAssistant, plant: FakePlant) -> None:
    from homeassistant.helpers import entity_registry as er

    _base_states(hass, "21.0")
    entry = await setup_zone(hass, zone_data("Hol", valves=["valve.v1"], sensor="sensor.t1"))
    registry = er.async_get(hass)
    ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    eid = entry.entry_id
    assert {
        eid,
        f"{eid}_temperature_slope",
        f"{eid}_temperature_slope_hourly",
        f"{eid}_cycle_on_seconds",
        f"{eid}_cycle_tail_seconds",
        f"{eid}_cycle_time_to_target",
        f"{eid}_heater_active",
    } <= ids
    climate = registry.async_get("climate.hol")
    assert climate is not None and climate.device_id is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


__all__ = ["MockConfigEntry"]
