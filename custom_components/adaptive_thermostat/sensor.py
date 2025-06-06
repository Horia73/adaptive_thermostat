"""Sensor platform for the Adaptive Thermostat integration."""

import logging
from typing import Any, Dict, Optional

from homeassistant.components.sensor import ( # type: ignore
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import ( # type: ignore
    UnitOfTemperature,
    PERCENTAGE,
    STATE_ON,
    STATE_OFF,
    CONF_NAME,
)
from homeassistant.config_entries import ConfigEntry # type: ignore
from homeassistant.core import HomeAssistant, callback # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback # type: ignore
from homeassistant.helpers.event import async_track_state_change_event, Event # type: ignore

from .const import (
    DOMAIN,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_HEATER,
    CONF_CENTRAL_HEATER,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Adaptive Thermostat sensor platform."""
    _LOGGER.debug("Setting up sensor entities for entry %s", entry.entry_id)
    
    config = {**entry.data, **entry.options}
    
    sensors = []
    
    # Create temperature difference sensor (target - current)
    sensors.append(AdaptiveThermostatTemperatureDifferenceSensor(hass, entry, config))
    
    # Create heating efficiency sensor (percentage of time heating)
    sensors.append(AdaptiveThermostatHeatingEfficiencySensor(hass, entry, config))
    
    # Create outdoor temperature sensor (for statistics from our reading)
    if config.get(CONF_OUTDOOR_SENSOR):
        sensors.append(AdaptiveThermostatOutdoorTempSensor(hass, entry, config))
    
    async_add_entities(sensors)

class AdaptiveThermostatBaseSensor(SensorEntity):
    """Base class for Adaptive Thermostat sensors."""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: Dict[str, Any], sensor_type: str) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._entry_id = entry.entry_id
        self._config = config
        self._sensor_type = sensor_type
        
        # Create unique ID
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        
        # Set device info to group with climate entity
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": config.get(CONF_NAME, "Adaptive Thermostat"),
            "manufacturer": "Adaptive Thermostat",
            "model": "Smart Zone Controller",
            "via_device": (DOMAIN, entry.entry_id),
        }
        
        # Get the climate entity ID for state tracking
        self._climate_entity_id = f"climate.{entry.entry_id}"
        
        # Initialize state
        self._state = None
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Track climate entity state changes
        self._remove_listener = async_track_state_change_event(
            self.hass, [self._climate_entity_id], self._async_climate_state_changed
        )
        
        # Initial update
        self.async_schedule_update_ha_state(True)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._remove_listener:
            self._remove_listener()

    @callback
    def _async_climate_state_changed(self, event: Event) -> None:
        """Handle climate state changes."""
        self.async_schedule_update_ha_state(True)

class AdaptiveThermostatTemperatureDifferenceSensor(AdaptiveThermostatBaseSensor):
    """Sensor to track temperature difference (target - current)."""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: Dict[str, Any]) -> None:
        """Initialize the temperature difference sensor."""
        super().__init__(hass, entry, config, "temperature_difference")
        
        self._attr_name = f"{config.get(CONF_NAME)} Temperature Difference"
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:thermometer-chevron-up"

    async def async_update(self) -> None:
        """Update the sensor state."""
        climate_state = self.hass.states.get(self._climate_entity_id)
        if not climate_state:
            self._state = None
            return
            
        current_temp = climate_state.attributes.get("current_temperature")
        target_temp = climate_state.attributes.get("temperature")
        
        if current_temp is not None and target_temp is not None:
            self._state = round(target_temp - current_temp, 1)
        else:
            self._state = None

    @property
    def native_value(self) -> float | None:
        """Return the native value of the sensor."""
        return self._state

class AdaptiveThermostatHeatingEfficiencySensor(AdaptiveThermostatBaseSensor):
    """Sensor to track heating efficiency (percentage of time heating)."""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: Dict[str, Any]) -> None:
        """Initialize the heating efficiency sensor."""
        super().__init__(hass, entry, config, "heating_efficiency")
        
        self._attr_name = f"{config.get(CONF_NAME)} Heating Efficiency"
        self._attr_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:fire-circle"
        
        # Track heating time
        self._last_update = None
        self._heating_time = 0
        self._total_time = 0

    async def async_update(self) -> None:
        """Update the sensor state."""
        import time
        
        climate_state = self.hass.states.get(self._climate_entity_id)
        if not climate_state:
            self._state = None
            return
            
        current_time = time.time()
        is_heating = climate_state.attributes.get("hvac_action") == "heating"
        
        if self._last_update is not None:
            time_delta = current_time - self._last_update
            self._total_time += time_delta
            
            if is_heating:
                self._heating_time += time_delta
        
        self._last_update = current_time
        
        # Calculate efficiency as percentage (reset every hour to prevent overflow)
        if self._total_time > 3600:  # Reset every hour
            if self._total_time > 0:
                efficiency = (self._heating_time / self._total_time) * 100
                self._state = round(efficiency, 1)
            else:
                self._state = 0
            
            # Reset counters
            self._heating_time = 0
            self._total_time = 0
        elif self._total_time > 0:
            efficiency = (self._heating_time / self._total_time) * 100
            self._state = round(efficiency, 1)
        else:
            self._state = 0

    @property
    def native_value(self) -> float | None:
        """Return the native value of the sensor."""
        return self._state

class AdaptiveThermostatOutdoorTempSensor(AdaptiveThermostatBaseSensor):
    """Sensor to expose outdoor temperature for statistics."""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: Dict[str, Any]) -> None:
        """Initialize the outdoor temperature sensor."""
        super().__init__(hass, entry, config, "outdoor_temperature")
        
        self._attr_name = f"{config.get(CONF_NAME)} Outdoor Temperature"
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:thermometer"
        
        self._outdoor_sensor_id = config.get(CONF_OUTDOOR_SENSOR)
        self._backup_sensor_id = config.get(CONF_BACKUP_OUTDOOR_SENSOR)

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Also track outdoor sensor changes
        outdoor_sensors = []
        if self._outdoor_sensor_id:
            outdoor_sensors.append(self._outdoor_sensor_id)
        if self._backup_sensor_id:
            outdoor_sensors.append(self._backup_sensor_id)
            
        if outdoor_sensors:
            self._remove_outdoor_listener = async_track_state_change_event(
                self.hass, outdoor_sensors, self._async_outdoor_state_changed
            )

    @callback
    def _async_outdoor_state_changed(self, event: Event) -> None:
        """Handle outdoor sensor state changes."""
        self.async_schedule_update_ha_state(True)

    async def async_update(self) -> None:
        """Update the sensor state."""
        # Try primary outdoor sensor first
        outdoor_temp = None
        if self._outdoor_sensor_id:
            outdoor_state = self.hass.states.get(self._outdoor_sensor_id)
            if outdoor_state and outdoor_state.state not in ["unknown", "unavailable"]:
                try:
                    outdoor_temp = float(outdoor_state.state)
                except (ValueError, TypeError):
                    pass
        
        # Fallback to backup sensor
        if outdoor_temp is None and self._backup_sensor_id:
            backup_state = self.hass.states.get(self._backup_sensor_id)
            if backup_state and backup_state.state not in ["unknown", "unavailable"]:
                try:
                    outdoor_temp = float(backup_state.state)
                except (ValueError, TypeError):
                    pass
        
        self._state = outdoor_temp

    @property
    def native_value(self) -> float | None:
        """Return the native value of the sensor."""
        return self._state 