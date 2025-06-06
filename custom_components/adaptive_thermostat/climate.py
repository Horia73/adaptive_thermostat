"""Climate platform for the Adaptive Thermostat integration."""

import logging
from typing import Any # Import Any for type hinting

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
    UnitOfTemperature, # Import temperature unit if needed directly
)
from homeassistant.config_entries import ConfigEntry # type: ignore
from homeassistant.core import HomeAssistant, callback # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback # type: ignore
from homeassistant.helpers.event import async_track_state_change_event, Event # type: ignore
# Import PlatformNotReady if implementing stricter setup validation
# from homeassistant.exceptions import PlatformNotReady

from . import DOMAIN # Import domain from __init__

# Import all configuration constants used
from .const import (
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_SLEEP_PRESET,
    CONF_HOME_PRESET,
    CONF_AWAY_PRESET,
    CONF_CONFIG_TYPE,
    CONFIG_TYPE_INDIVIDUAL_ZONE,
    CONFIG_TYPE_CENTRAL_HEATER,
    DEFAULT_NAME, # Import defaults for fallback
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
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
    """Representation of an Adaptive Thermostat."""

    _attr_has_entity_name = True # Use default name generation based on device/config entry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the thermostat."""
        self._hass = hass
        self._attr_unique_id = entry.entry_id
        self._entry_id = entry.entry_id # Store entry_id for logging if needed

        # --- Read configuration: prioritize options, fall back to data ---
        config = {**entry.data, **entry.options}
        _LOGGER.debug("[%s] Initializing with combined config: %s", self._entry_id, config)

        # --- Helper function to handle None or "" ---
        def get_entity_id(key):
            """Return entity_id or None if value is None or empty string."""
            val = config.get(key)
            return val if val else None
        # --- End Helper ---

        # Configuration type handling
        self._config_type = config.get(CONF_CONFIG_TYPE, CONFIG_TYPE_INDIVIDUAL_ZONE)
        _LOGGER.debug("[%s] Configuration type: %s", self._entry_id, self._config_type)

        # Required configuration
        self._attr_name = config.get(CONF_NAME, DEFAULT_NAME) # Name from config or default
        self._heater_entity_id = config.get(CONF_HEATER) # Use direct .get() for required

        # Check required fields
        if not self._heater_entity_id:
            _LOGGER.error("[%s] Heater entity ID is missing from configuration", self._entry_id)

        # Temperature sensor - required for individual zones, optional for central heater
        if self._config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            self._temp_sensor_entity_id = config.get(CONF_TEMP_SENSOR) # Required for individual zones
            if not self._temp_sensor_entity_id:
                _LOGGER.error("[%s] Temperature sensor entity ID is missing from individual zone configuration", self._entry_id)
        else:
            # For central heater, temperature sensor is optional
            self._temp_sensor_entity_id = get_entity_id(CONF_TEMP_SENSOR)
            if not self._temp_sensor_entity_id:
                _LOGGER.info("[%s] No temperature sensor configured for central heater", self._entry_id)

        # --- Use helper for Optional configuration sensors (only for individual zones) ---
        if self._config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            self._humidity_sensor_entity_id = get_entity_id(CONF_HUMIDITY_SENSOR)
            self._door_window_sensor_entity_id = get_entity_id(CONF_DOOR_WINDOW_SENSOR)
            self._motion_sensor_entity_id = get_entity_id(CONF_MOTION_SENSOR)
            self._outdoor_sensor_entity_id = get_entity_id(CONF_OUTDOOR_SENSOR)
            self._backup_outdoor_sensor_entity_id = get_entity_id(CONF_BACKUP_OUTDOOR_SENSOR)
        else:
            # Central heater doesn't use these sensors
            self._humidity_sensor_entity_id = None
            self._door_window_sensor_entity_id = None
            self._motion_sensor_entity_id = None
            self._outdoor_sensor_entity_id = None
            self._backup_outdoor_sensor_entity_id = None
        # --- End Optional Sensor Reading ---

        # Preset temperatures - only for individual zones
        if self._config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            self._presets = {
                "sleep": config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET),
                "home": config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET),
                "away": config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET),
            }
        else:
            # Central heater uses simple on/off without presets
            self._presets = {}

        # Internal state attributes
        self._current_temperature: float | None = None
        self._current_humidity: float | None = None
        self._hvac_mode: HVACMode = HVACMode.OFF
        # _hvac_action is determined by property now, no need to store separately
        
        # Target temperature handling
        if self._config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            self._target_temperature: float = self._presets["home"] # Default target to home preset
            self._current_preset: str = "home" # Default preset mode
        else:
            # Central heater doesn't use target temperature - it's just on/off
            self._target_temperature: float | None = None
            self._current_preset: str | None = None

        # Climate entity attributes
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        
        if self._config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            self._attr_preset_modes = list(self._presets.keys())
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
            )
        else:
            # Central heater only supports on/off
            self._attr_preset_modes = []
            self._attr_supported_features = 0  # No additional features for central heater
            
        self._attr_temperature_unit = self._hass.config.units.temperature_unit

        # Extra state attributes for UI card (values are now correctly None if "" was saved)
        self._attr_extra_state_attributes = {
            "heater_entity_id": self._heater_entity_id,
            "temp_sensor_entity_id": self._temp_sensor_entity_id,
            "humidity_sensor": self._humidity_sensor_entity_id,
            "motion_sensor": self._motion_sensor_entity_id,
            "door_window_sensor": self._door_window_sensor_entity_id,
            "outdoor_sensor": self._outdoor_sensor_entity_id,
            "backup_outdoor_sensor": self._backup_outdoor_sensor_entity_id,
        }
        _LOGGER.debug("[%s] Extra state attributes set: %s", self._entry_id, self._attr_extra_state_attributes)


        # Listener for state changes
        self._unsub_state_listener = None

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        _LOGGER.debug("[%s] Entity added to HASS", self._entry_id)

        # Register state change listeners (handles None values correctly)
        entities_to_track = []
        if self._temp_sensor_entity_id:
            entities_to_track.append(self._temp_sensor_entity_id)
        if self._humidity_sensor_entity_id: # Will only add if not None/""
            entities_to_track.append(self._humidity_sensor_entity_id)
        if self._heater_entity_id:
             entities_to_track.append(self._heater_entity_id)


        if entities_to_track:
            self._unsub_state_listener = async_track_state_change_event(
                self.hass, entities_to_track, self._async_state_changed
            )
            _LOGGER.debug("[%s] Registered state listener for: %s", self._entry_id, entities_to_track)
        else:
            _LOGGER.warning("[%s] No valid sensors/heater configured for state tracking.", self._entry_id)

        # Get initial state by scheduling first update
        # await self.async_update() # Calling update directly can be problematic
        self.async_schedule_update_ha_state(True) # Preferred way


    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from HASS."""
        _LOGGER.debug("[%s] Entity removing from HASS", self._entry_id)
        if self._unsub_state_listener:
            self._unsub_state_listener()
            self._unsub_state_listener = None
            _LOGGER.debug("[%s] Unsubscribed state listener.", self._entry_id)
        await super().async_will_remove_from_hass()

    # --- Properties ---

    @property
    def name(self) -> str:
        """Return the display name of the thermostat."""
        # If _attr_has_entity_name = True, name might be handled differently
        # but returning self._attr_name is safe.
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
        """Return current hvac mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation if supported."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        if not self._heater_entity_id: # Can't determine action without heater
             return HVACAction.IDLE # Or maybe OFF? Depends on desired state.

        # Check the actual state of the heater entity for current action
        heater_state = self.hass.states.get(self._heater_entity_id)
        if heater_state and heater_state.state == STATE_ON:
            return HVACAction.HEATING

        # If mode is HEAT but heater is off, it's IDLE
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        return self._current_preset

    # --- Service Calls ---

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode not in self.hvac_modes:
            _LOGGER.warning("[%s] Unsupported HVAC mode: %s", self._entry_id, hvac_mode)
            return

        # Only update if mode changed
        if hvac_mode == self._hvac_mode:
            return

        self._hvac_mode = hvac_mode
        _LOGGER.debug("[%s] HVAC mode set to %s", self._entry_id, hvac_mode)

        # Turn off heater if mode is set to OFF
        if hvac_mode == HVACMode.OFF:
            await self._async_turn_heater_off() # Ensure heater is turned off

        # Trigger state check and update HA state
        # await self._async_control_heating() # Control heating only runs if mode is HEAT
        self.async_schedule_update_ha_state(True) # Let update handle control logic

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode not in self.preset_modes:
            _LOGGER.warning("[%s] Unsupported preset mode: %s", self._entry_id, preset_mode)
            return
        if preset_mode == self._current_preset:
            return # No change

        self._current_preset = preset_mode
        # Ensure preset exists before accessing
        new_target = self._presets.get(preset_mode)
        if new_target is None:
             _LOGGER.error("[%s] Preset '%s' selected but not found in config!", self._entry_id, preset_mode)
             # Optionally revert preset or set a default target
             self._current_preset = "home" # Revert to home?
             new_target = self._presets.get("home", DEFAULT_HOME_PRESET)

        self._target_temperature = new_target
        _LOGGER.debug(
            "[%s] Preset mode set to '%s', target temperature updated to %s",
            self._entry_id, self._current_preset, self._target_temperature
        )

        # Trigger state check and update HA state
        # await self._async_control_heating() # Let update handle it
        self.async_schedule_update_ha_state(True)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        new_target = float(temperature)
        if new_target == self._target_temperature:
            return # No change

        self._target_temperature = new_target
        _LOGGER.debug("[%s] Target temperature set to %s", self._entry_id, self._target_temperature)
        # Setting temp might implicitly change preset mode if desired, or clear it
        # self._current_preset = None # Example: Clear preset on manual temp change

        # Trigger state check and update HA state
        # await self._async_control_heating() # Let update handle it
        self.async_schedule_update_ha_state(True)

    # --- Update and Control Logic ---

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Handle state changes of tracked entities."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        # Ignore unavailable/unknown states if needed, or handle them in async_update
        if not new_state or new_state.state in ("unavailable", "unknown"):
             _LOGGER.debug("[%s] State change to unavailable/unknown for %s, scheduling update", self._entry_id, entity_id)
             self.async_schedule_update_ha_state(True)
             return

        # Avoid updates if state value is the same (already handled in original code)
        if old_state and new_state.state == old_state.state:
            _LOGGER.debug("[%s] State value unchanged for %s, skipping update", self._entry_id, entity_id)
            return

        _LOGGER.debug("[%s] State change detected for %s, scheduling update.", self._entry_id, entity_id)
        self.async_schedule_update_ha_state(True) # Force refresh to re-evaluate

    async def async_update(self) -> None:
        """Update the state of the thermostat from sensors."""
        _LOGGER.debug("[%s] Starting async_update", self._entry_id)
        old_temp = self._current_temperature
        old_humidity = self._current_humidity

        # Update current temperature
        new_temp = None
        if self._temp_sensor_entity_id:
            temp_state = self.hass.states.get(self._temp_sensor_entity_id)
            if temp_state and temp_state.state not in ("unavailable", "unknown"):
                try:
                    new_temp = float(temp_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Could not parse temp state '%s'", self._entry_id, temp_state.state)
            # else: sensor is unavailable/unknown
        self._current_temperature = new_temp
        if old_temp != new_temp:
             _LOGGER.debug("[%s] Current temperature updated: %s -> %s", self._entry_id, old_temp, new_temp)


        # Update current humidity
        new_humidity = None
        if self._humidity_sensor_entity_id: # Check if configured (not None/"")
            humidity_state = self.hass.states.get(self._humidity_sensor_entity_id)
            if humidity_state and humidity_state.state not in ("unavailable", "unknown"):
                try:
                    new_humidity = float(humidity_state.state)
                except (ValueError, TypeError):
                    _LOGGER.warning("[%s] Could not parse humidity state '%s'", self._entry_id, humidity_state.state)
            # else: sensor is unavailable/unknown
        self._current_humidity = new_humidity
        if old_humidity != new_humidity:
             _LOGGER.debug("[%s] Current humidity updated: %s -> %s", self._entry_id, old_humidity, new_humidity)

        # Control heating based on potentially updated state
        await self._async_control_heating()

        # No need to explicitly call async_write_ha_state here,
        # it will be called automatically if properties change or by service calls.


    async def _async_control_heating(self) -> None:
        """Check temperature and turn heater ON/OFF based on the generic logic."""
        # Check prerequisites
        if not self._heater_entity_id or not self._temp_sensor_entity_id:
            _LOGGER.debug("[%s] Skipping heating control: heater or temp sensor missing.", self._entry_id)
            return
        if self._hvac_mode != HVACMode.HEAT:
            # If mode is not HEAT, ensure heater is off (idempotent)
            await self._async_turn_heater_off()
            _LOGGER.debug("[%s] Skipping heating control: Mode is %s", self._entry_id, self._hvac_mode)
            return
        if self._current_temperature is None:
            _LOGGER.debug("[%s] Skipping heating control: Current temperature unknown.", self._entry_id)
            return

        # --- Generic Thermostat Logic ---
        heater_should_be_on = False
        try:
            # Turn ON condition
            if self._current_temperature < self._target_temperature - HEATING_ON_OFFSET:
                heater_should_be_on = True
            # Turn OFF condition
            elif self._current_temperature >= self._target_temperature:
                heater_should_be_on = False
            # Hysteresis band: Keep current state
            else:
                current_heater_state = self.hass.states.get(self._heater_entity_id)
                heater_should_be_on = current_heater_state and current_heater_state.state == STATE_ON

            # Determine current state
            current_heater_state_obj = self.hass.states.get(self._heater_entity_id)
            is_currently_on = current_heater_state_obj and current_heater_state_obj.state == STATE_ON

            # Call service only if state needs to change
            if heater_should_be_on and not is_currently_on:
                await self._async_turn_heater_on()
            elif not heater_should_be_on and is_currently_on:
                await self._async_turn_heater_off()
            else:
                 _LOGGER.debug("[%s] Heating control: Heater state (%s) matches desired state (%s)",
                               self._entry_id, is_currently_on, heater_should_be_on)

        except Exception as e:
            _LOGGER.error("[%s] Error during heating control logic: %s", self._entry_id, e, exc_info=True) # Add exc_info


    async def _async_turn_heater_on(self) -> None:
        """Turn the heater switch/boolean on."""
        if not self._heater_entity_id: return

        # Check if already ON
        current_state = self.hass.states.get(self._heater_entity_id)
        if current_state and current_state.state == STATE_ON:
             _LOGGER.debug("[%s] Heater %s already ON.", self._entry_id, self._heater_entity_id)
             return

        domain = self._heater_entity_id.split('.')[0]
        _LOGGER.info("[%s] Turning heater ON: %s", self._entry_id, self._heater_entity_id)
        try:
            await self.hass.services.async_call(
                domain, "turn_on", {"entity_id": self._heater_entity_id}, context=self._context
            )
        except Exception as e:
            _LOGGER.error("[%s] Failed to turn on heater %s: %s", self._entry_id, self._heater_entity_id, e)

    async def _async_turn_heater_off(self) -> None:
        """Turn the heater switch/boolean off."""
        if not self._heater_entity_id: return

        # Check if already OFF
        current_state = self.hass.states.get(self._heater_entity_id)
        if current_state and current_state.state == STATE_OFF:
             _LOGGER.debug("[%s] Heater %s already OFF.", self._entry_id, self._heater_entity_id)
             return

        domain = self._heater_entity_id.split('.')[0]
        _LOGGER.info("[%s] Turning heater OFF: %s", self._entry_id, self._heater_entity_id)
        try:
            await self.hass.services.async_call(
                domain, "turn_off", {"entity_id": self._heater_entity_id}, context=self._context
            )
        except Exception as e:
            _LOGGER.error("[%s] Failed to turn off heater %s: %s", self._entry_id, self._heater_entity_id, e)