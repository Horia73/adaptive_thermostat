"""Climate platform for the Adaptive Thermostat integration.

The entity is a thin view of the zone runtime (:mod:`.runtime`): service
handlers record the desired state and return immediately; all control runs
in the runtime's serialized control step.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_TARGET_TEMP, MIN_TARGET_TEMP, SIGNAL_THERMOSTAT_READY
from .runtime import PRESETS, ZoneRuntime

_LOGGER = logging.getLogger(__name__)

# Diagnostics and static configuration: shown as attributes but kept out of
# the recorder (they used to make up most of a ~2.7 kB row written every 18 s).
UNRECORDED_ATTRIBUTES = frozenset(
    {
        "heater_entity_id",
        "heater_entity_ids",
        "central_heater_entity_id",
        "temp_sensor_entity_id",
        "backup_outdoor_sensor",
        "door_window_sensor",
        "motion_sensor",
        "auto_on_off_enabled",
        "window_detection_enabled",
        "window_slope_threshold",
        "control_mode",
        "control_reason",
        "model_confidence",
        "predicted_peak",
        "planned_zone_on_duration",
        "cycle_on_duration_s",
        "cycle_tail_duration_s",
        "cycle_time_to_target_s",
        "heat_elapsed_s",
        "heat_flowing",
        "learning",
        "temperature_slope_per_hour",
        "temperature_slope_instant_per_hour",
        "current_outdoor_temp",
        "valve_error_at",
        "last_fault",
        "last_fault_at",
        "thermal_params",
        "central_heater",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Adaptive Thermostat climate platform."""
    runtime: ZoneRuntime = entry.runtime_data
    async_add_entities([AdaptiveThermostat(runtime)])


class AdaptiveThermostat(ClimateEntity):
    """Climate entity of one heating zone."""

    _attr_should_poll = False
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_preset_modes = list(PRESETS)
    _attr_min_temp = MIN_TARGET_TEMP
    _attr_max_temp = MAX_TARGET_TEMP
    _attr_target_temperature_step = 0.1
    _unrecorded_attributes = UNRECORDED_ATTRIBUTES

    def __init__(self, runtime: ZoneRuntime) -> None:
        self.runtime = runtime
        entry = runtime.entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = runtime.name
        self._attr_temperature_unit = runtime.hass.config.units.temperature_unit or UnitOfTemperature.CELSIUS
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or runtime.name,
        )

    # ---------------------------------------------------------- lifecycle
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.runtime.entity = self
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        domain_data.setdefault("entities", {})[self.entity_id] = self
        domain_data.setdefault("entry_to_entity_id", {})[self.runtime.entry_id] = self.entity_id
        async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self.runtime.entry_id}", self.entity_id)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        domain_data = self.hass.data.get(DOMAIN, {})
        domain_data.get("entities", {}).pop(self.entity_id, None)
        if domain_data.get("entry_to_entity_id", {}).pop(self.runtime.entry_id, None):
            async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self.runtime.entry_id}", None)
        if self.runtime.entity is self:
            self.runtime.entity = None
        await super().async_will_remove_from_hass()

    # ---------------------------------------------------------- properties
    @property
    def hvac_mode(self) -> HVACMode:
        return self.runtime.hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        return self.runtime.hvac_action

    @property
    def current_temperature(self) -> float | None:
        return self.runtime.current_temperature

    @property
    def current_humidity(self) -> float | None:
        return self.runtime.current_humidity

    @property
    def target_temperature(self) -> float | None:
        return self.runtime.target

    @property
    def preset_mode(self) -> str | None:
        return self.runtime.preset

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rt = self.runtime
        now = dt_util.utcnow().timestamp()
        diag = rt.diagnostics()
        valve_error = rt.current_valve_error(now)
        attrs: dict[str, Any] = {
            # Read by the Lovelace card.
            "humidity_sensor": rt.humidity_sensor_id,
            "outdoor_sensor": rt.outdoor_sensor_id,
            "window_open_detected": rt.window_open,
            "window_alert": rt.window_alert,
            "valve_error": valve_error,
            "fault": rt.fault,
            "fault_message": rt.fault_message,
            # State that matters for automations.
            "manual_override": rt.manual_override,
            "zone_heater_on": rt.zone_heater_on,
            # Static configuration (unrecorded).
            "heater_entity_id": rt.heater_ids[0] if rt.heater_ids else None,
            "heater_entity_ids": rt.heater_ids,
            "central_heater_entity_id": rt.central_heater_id,
            "temp_sensor_entity_id": rt.temp_sensor_id,
            "backup_outdoor_sensor": rt.backup_outdoor_sensor_id,
            "door_window_sensor": rt.door_window_sensor_id,
            "motion_sensor": rt.motion_sensor_id,
            "auto_on_off_enabled": rt.auto_on_off,
            "window_detection_enabled": rt.window_detection,
            "window_slope_threshold": rt.window_slope_threshold,
            # Diagnostics (unrecorded).
            "valve_error_at": (
                dt_util.utc_from_timestamp(rt.valve_error_at).isoformat() if rt.valve_error_at else None
            ),
            "planned_zone_on_duration": diag.get("cycle_on_duration_s"),
        }
        attrs.update(diag)
        return attrs

    # ------------------------------------------------------------ services
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.info("[%s] HVAC mode -> %s", self.runtime.name, hvac_mode)
        self.runtime.set_hvac_mode(hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        mode = kwargs.get(ATTR_HVAC_MODE)
        if mode is not None:
            self.runtime.set_hvac_mode(HVACMode(mode))
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self.runtime.set_target(float(temperature))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self.runtime.set_preset(preset_mode)

    async def async_turn_on(self) -> None:
        self.runtime.set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        self.runtime.set_hvac_mode(HVACMode.OFF)

    # Called by the integration services.
    def reset_manual_override(self) -> None:
        self.runtime.reset_manual_override()

    def dismiss_window_alert(self) -> None:
        self.runtime.dismiss_window_alert()

    def reset_adaptive_profile(self) -> None:
        self.runtime.reset_model()
