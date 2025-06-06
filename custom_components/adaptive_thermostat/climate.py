"""Climate platform for the Adaptive Thermostat integration."""

import asyncio
import logging
from typing import Any, Dict, List

from homeassistant.components.climate import ( # type: ignore
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import ( # type: ignore
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_OFF,
    CONF_NAME,
    UnitOfTemperature,
)
from homeassistant.config_entries import ConfigEntry # type: ignore
from homeassistant.core import HomeAssistant, callback # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback # type: ignore
from homeassistant.helpers.event import async_track_state_change_event, Event # type: ignore

from . import DOMAIN

# Import all configuration constants used
from .const import (
    CONF_HEATER,
    CONF_CENTRAL_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_SLEEP_PRESET,
    CONF_HOME_PRESET,
    CONF_AWAY_PRESET,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_AUTO_OFF_TEMP,
    DEFAULT_NAME,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
    CENTRAL_HEATER_TURN_ON_DELAY,
    CENTRAL_HEATER_TURN_OFF_DELAY,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_AUTO_OFF_TEMP,
)

_LOGGER = logging.getLogger(__name__)

# Define the heating threshold offset
HEATING_ON_OFFSET = 0.1

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Adaptive Thermostat climate platform."""
    _LOGGER.debug("Setting up climate entity for entry %s", entry.entry_id)
    async_add_entities([AdaptiveThermostat(hass, entry)])


class AdaptiveThermostat(ClimateEntity):
    """Representation of an Adaptive Thermostat zone."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the thermostat."""
        self._hass = hass
        self._attr_unique_id = entry.entry_id
        self._entry_id = entry.entry_id

        # --- Read configuration: prioritize options, fall back to data ---
        config = {**entry.data, **entry.options}
        _LOGGER.debug("[%s] Initializing with combined config: %s", self._entry_id, config)

        # --- Helper function to handle None or "" ---
        def get_entity_id(key):
            """Return entity_id or None if value is None or empty string."""
            val = config.get(key)
            return val if val else None
        # --- End Helper ---

        # Required configuration
        self._attr_name = config.get(CONF_NAME, DEFAULT_NAME)
        self._heater_entity_id = config.get(CONF_HEATER)
        self._temp_sensor_entity_id = config.get(CONF_TEMP_SENSOR)

        # Central heater configuration (optional)
        self._central_heater_entity_id = get_entity_id(CONF_CENTRAL_HEATER)
        
        # Central heater timing configuration
        self._central_heater_turn_on_delay = config.get(CONF_CENTRAL_HEATER_TURN_ON_DELAY, CENTRAL_HEATER_TURN_ON_DELAY)
        self._central_heater_turn_off_delay = config.get(CONF_CENTRAL_HEATER_TURN_OFF_DELAY, CENTRAL_HEATER_TURN_OFF_DELAY)
        
        # Auto on/off configuration
        self._auto_on_off_enabled = config.get(CONF_AUTO_ON_OFF_ENABLED, False)
        self._auto_on_temp = config.get(CONF_AUTO_ON_TEMP, DEFAULT_AUTO_ON_TEMP)
        self._auto_off_temp = config.get(CONF_AUTO_OFF_TEMP, DEFAULT_AUTO_OFF_TEMP)
        
        # Manual override state - when user manually controls the thermostat
        self._manual_override = False
        self._last_outdoor_temp = None

        # Check required fields
        if not self._heater_entity_id:
            _LOGGER.error("[%s] Heater entity ID is missing from configuration", self._entry_id)
        if not self._temp_sensor_entity_id:
            _LOGGER.error("[%s] Temperature sensor entity ID is missing from configuration", self._entry_id)

        # Optional configuration sensors
        self._humidity_sensor_entity_id = get_entity_id(CONF_HUMIDITY_SENSOR)
        self._door_window_sensor_entity_id = get_entity_id(CONF_DOOR_WINDOW_SENSOR)
        self._motion_sensor_entity_id = get_entity_id(CONF_MOTION_SENSOR)
        self._outdoor_sensor_entity_id = get_entity_id(CONF_OUTDOOR_SENSOR)
        self._backup_outdoor_sensor_entity_id = get_entity_id(CONF_BACKUP_OUTDOOR_SENSOR)

        # Preset temperatures
        self._presets = {
            "sleep": config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET),
            "home": config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET),
            "away": config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET),
        }

        # Internal state attributes
        self._current_temperature: float | None = None
        self._current_humidity: float | None = None
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._target_temperature: float = self._presets["home"]
        self._current_preset: str = "home"

        # Climate entity attributes
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_preset_modes = list(self._presets.keys())
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
        )
        self._attr_temperature_unit = self._hass.config.units.temperature_unit

        # Extra state attributes for UI card
        self._attr_extra_state_attributes = {
            "heater_entity_id": self._heater_entity_id,
            "central_heater_entity_id": self._central_heater_entity_id,
            "temp_sensor_entity_id": self._temp_sensor_entity_id,
            "humidity_sensor": self._humidity_sensor_entity_id,
            "motion_sensor": self._motion_sensor_entity_id,
            "door_window_sensor": self._door_window_sensor_entity_id,
            "outdoor_sensor": self._outdoor_sensor_entity_id,
            "weather_sensor": self._backup_outdoor_sensor_entity_id,  # Renamed for card compatibility
        }
        _LOGGER.debug("[%s] Extra state attributes set: %s", self._entry_id, self._attr_extra_state_attributes)

        # Central heater coordination state
        self._zone_heater_on = False
        self._central_heater_task = None

        # Listener for state changes
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        _LOGGER.debug("[%s] Entity added to HASS", self._entry_id)

        # Register state change listeners
        entities_to_track = []
        if self._temp_sensor_entity_id:
            entities_to_track.append(self._temp_sensor_entity_id)
        if self._humidity_sensor_entity_id:
            entities_to_track.append(self._humidity_sensor_entity_id)
        if self._heater_entity_id:
             entities_to_track.append(self._heater_entity_id)

        if entities_to_track:
            self._remove_listener = async_track_state_change_event(
                self.hass, entities_to_track, self._async_state_changed
            )
            _LOGGER.debug("[%s] Registered state listener for: %s", self._entry_id, entities_to_track)
        else:
            _LOGGER.warning("[%s] No valid sensors/heater configured for state tracking.", self._entry_id)

        # Get initial state by scheduling first update
        self.async_schedule_update_ha_state(True)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _LOGGER.debug("[%s] Removing adaptive thermostat from Home Assistant", self._entry_id)
        
        # Cancel any pending central heater tasks
        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None
            
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
            _LOGGER.debug("[%s] Unsubscribed state listener.", self._entry_id)
        await super().async_will_remove_from_hass()

    # --- Properties ---

    @property
    def name(self) -> str:
        """Return the display name of the thermostat."""
        return self._attr_name

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity."""
        return self._current_humidity

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation ie. heat, cool mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation if supported."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        elif self._zone_heater_on:
            return HVACAction.HEATING
        else:
            return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp."""
        return self._current_preset

    # --- Service calls ---

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.debug("[%s] Setting HVAC mode to: %s", self._entry_id, hvac_mode)
        
        # Mark as manual override when user manually changes HVAC mode
        self._manual_override = True
        
        if hvac_mode == HVACMode.OFF:
            await self._async_turn_heater_off()
            self._hvac_mode = HVACMode.OFF
        elif hvac_mode == HVACMode.HEAT:
            self._hvac_mode = HVACMode.HEAT
            await self._async_control_heating()
        else:
            _LOGGER.warning("[%s] Unsupported HVAC mode: %s", self._entry_id, hvac_mode)
            return

        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode not in self._presets:
            _LOGGER.warning("[%s] Invalid preset mode: %s", self._entry_id, preset_mode)
            return

        _LOGGER.debug("[%s] Setting preset mode to: %s", self._entry_id, preset_mode)
        
        # Mark as manual override when user manually changes preset
        self._manual_override = True
        
        self._current_preset = preset_mode
        self._target_temperature = self._presets[preset_mode]
        
        # Trigger heating control with new target temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating()
        
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        _LOGGER.debug("[%s] Setting target temperature to: %s°C", self._entry_id, temperature)
        
        # Mark as manual override when user manually sets temperature
        self._manual_override = True
        
        self._target_temperature = float(temperature)
        
        # Clear preset mode when manually setting temperature
        self._current_preset = None
        
        # Trigger heating control with new target temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating()
        
        self.async_write_ha_state()

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Handle state changes of tracked entities."""
        _LOGGER.debug("[%s] State changed event: %s", self._entry_id, event.data)
        # Schedule an update which will call async_update
        self.async_schedule_update_ha_state(True)

    async def async_update(self) -> None:
        """Update the entity state."""
        # Update current temperature from sensor
        if self._temp_sensor_entity_id:
            temp_state = self.hass.states.get(self._temp_sensor_entity_id)
            if temp_state and temp_state.state not in ["unknown", "unavailable"]:
                try:
                    self._current_temperature = float(temp_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Invalid temperature from sensor: %s", self._entry_id, temp_state.state)

        # Update current humidity from sensor
        if self._humidity_sensor_entity_id:
            humidity_state = self.hass.states.get(self._humidity_sensor_entity_id)
            if humidity_state and humidity_state.state not in ["unknown", "unavailable"]:
                try:
                    self._current_humidity = float(humidity_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Invalid humidity from sensor: %s", self._entry_id, humidity_state.state)

        # Handle auto on/off based on outdoor temperature
        if self._auto_on_off_enabled and not self._manual_override:
            await self._async_handle_auto_onoff()

        # Trigger heating control if in heat mode
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating()

    async def _async_handle_auto_onoff(self) -> None:
        """Handle automatic on/off based on outdoor temperature."""
        outdoor_temp = None
        
        # Try primary outdoor sensor first
        if self._outdoor_sensor_entity_id:
            outdoor_state = self.hass.states.get(self._outdoor_sensor_entity_id)
            if outdoor_state and outdoor_state.state not in ["unknown", "unavailable"]:
                try:
                    outdoor_temp = float(outdoor_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Invalid outdoor temperature: %s", self._entry_id, outdoor_state.state)
        
        # Fallback to backup sensor if primary failed
        if outdoor_temp is None and self._backup_outdoor_sensor_entity_id:
            backup_state = self.hass.states.get(self._backup_outdoor_sensor_entity_id)
            if backup_state and backup_state.state not in ["unknown", "unavailable"]:
                try:
                    outdoor_temp = float(backup_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Invalid backup outdoor temperature: %s", self._entry_id, backup_state.state)
        
        if outdoor_temp is None:
            return  # No valid outdoor temperature available
        
        # Only process if temperature changed significantly (0.5°C hysteresis)
        if self._last_outdoor_temp is not None and abs(outdoor_temp - self._last_outdoor_temp) < 0.5:
            return
        
        self._last_outdoor_temp = outdoor_temp
        
        _LOGGER.debug("[%s] Auto on/off check - Outdoor temp: %s°C, Current mode: %s", 
                     self._entry_id, outdoor_temp, self._hvac_mode)
        
        # Auto turn on when outdoor temp drops below threshold
        if outdoor_temp < self._auto_on_temp and self._hvac_mode == HVACMode.OFF:
            _LOGGER.info("[%s] Auto turning ON - Outdoor temp %s°C < %s°C", 
                        self._entry_id, outdoor_temp, self._auto_on_temp)
            self._hvac_mode = HVACMode.HEAT
            await self._async_control_heating()
            
        # Auto turn off when outdoor temp rises above threshold
        elif outdoor_temp > self._auto_off_temp and self._hvac_mode == HVACMode.HEAT:
            _LOGGER.info("[%s] Auto turning OFF - Outdoor temp %s°C > %s°C", 
                        self._entry_id, outdoor_temp, self._auto_off_temp)
            await self._async_turn_heater_off()
            self._hvac_mode = HVACMode.OFF
            
        # Clear manual override if auto conditions would naturally change state
        # This allows auto mode to resume after user's manual intervention has become obsolete
        if ((outdoor_temp < self._auto_on_temp and self._hvac_mode == HVACMode.HEAT) or 
            (outdoor_temp > self._auto_off_temp and self._hvac_mode == HVACMode.OFF)):
            self._manual_override = False

    async def _async_control_heating(self) -> None:
        """Control heating based on current vs target temperature."""
        should_heat = await self._async_should_heat()
        
        # If we need to heat and heater is off, turn it on
        if should_heat and not self._zone_heater_on:
            _LOGGER.debug("[%s] Need to heat: current=%s°C, target=%s°C", self._entry_id, self._current_temperature, self._target_temperature)
            await self._async_turn_heater_on()
            self._zone_heater_on = True
            
        # If we don't need to heat and heater is on, turn it off
        elif not should_heat and self._zone_heater_on:
            _LOGGER.debug("[%s] Don't need to heat: current=%s°C, target=%s°C", self._entry_id, self._current_temperature, self._target_temperature)
            await self._async_turn_heater_off()
            self._zone_heater_on = False

    async def _async_should_heat(self) -> bool:
        """Determine if heating is needed based on temperature difference."""
        if self._current_temperature is None or self._target_temperature is None:
            return False
        
        temp_diff = self._target_temperature - self._current_temperature
        return temp_diff > HEATING_ON_OFFSET

    async def _async_turn_on_entity(self, entity_id: str, entity_name: str) -> None:
        """Turn on an entity (switch or climate)."""
        if not entity_id:
            return
            
        domain = "climate" if entity_id.startswith("climate.") else "switch"
        _LOGGER.info("[%s] Turning on %s: %s", self._entry_id, entity_name, entity_id)
        
        await self.hass.services.async_call(
            domain, "turn_on", {"entity_id": entity_id}, blocking=True
        )

    async def _async_turn_off_entity(self, entity_id: str, entity_name: str) -> None:
        """Turn off an entity (switch or climate)."""
        if not entity_id:
            return
            
        domain = "climate" if entity_id.startswith("climate.") else "switch"
        _LOGGER.info("[%s] Turning off %s: %s", self._entry_id, entity_name, entity_id)
        
        await self.hass.services.async_call(
            domain, "turn_off", {"entity_id": entity_id}, blocking=True
        )

    async def _async_check_other_zones_need_heat(self) -> bool:
        """Check if other zones using the same central heater need heat."""
        if not self._central_heater_entity_id:
            return False
            
        # Get all adaptive thermostat entities in HA
        all_entities = []
        for entity_id in self.hass.states.async_entity_ids("climate"):
            if entity_id.startswith("climate.adaptive_thermostat"):
                entity = self.hass.data.get("climate", {}).get(entity_id)
                if entity and hasattr(entity, '_central_heater_entity_id'):
                    all_entities.append(entity)
        
        # Check entities with same central heater
        for entity in all_entities:
            if (entity != self and 
                entity._central_heater_entity_id == self._central_heater_entity_id and
                entity._zone_heater_on):
                _LOGGER.debug("[%s] Other zone %s still needs heat", self._entry_id, entity.entity_id)
                return True
                
        _LOGGER.debug("[%s] No other zones need heat from central heater", self._entry_id)
        return False