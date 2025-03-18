import math
from collections import deque

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_ON, STATE_OFF, CONF_NAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import DOMAIN  # Import DOMAIN from __init__.py
from .const import (  # Add this block
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_SENSOR,
    CONF_OUTSIDE_TEMP_OFF,
    CONF_SLEEP_PRESET,
    CONF_HOME_PRESET,
    CONF_AWAY_PRESET,
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate platform."""
    async_add_entities([AdaptiveThermostat(hass, entry)])  # Pass hass and entry


class AdaptiveThermostat(ClimateEntity):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the thermostat."""
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.data.get(CONF_NAME, "Adaptive Thermostat")
        
        # Get config data from entry.data
        config = entry.data
        self._heater = config["heater"]
        self._temp_sensor = config["temp_sensor"]
        self._humidity_sensor = config.get("humidity_sensor")
        self._door_window_sensor = config.get("door_window_sensor")
        self._motion_sensor = config.get("motion_sensor")
        self._outdoor_sensor = config.get("outdoor_sensor")
        self._weather_sensor = config.get("weather_sensor")
        self._outside_temp_off = config["outside_temp_off"]
        self._presets = {
            "sleep": config["sleep_preset"],
            "home": config["home_preset"],
            "away": config["away_preset"],
        }
        self._setpoint = config["home_preset"]  # Default to Home preset
        self._current_preset = "home"
        
        # Change accuracy from 0.5 to 0.1 degrees
        self._delta = 0.1
        
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._temperature = None
        
        # Make these available in the UI card
        self._attr_extra_state_attributes = {
            "humidity_sensor": self._humidity_sensor,
            "outdoor_sensor": self._outdoor_sensor,
            "weather_sensor": self._weather_sensor,
            "motion_sensor": self._motion_sensor, 
            "door_window_sensor": self._door_window_sensor
        }
        
        # Track state changes of input entities
        async_track_state_change_event(
            self._hass,
            [self._temp_sensor, self._heater],
            self._async_state_changed,
        )
        # Initial update
        if self._hass.is_running:
            self._hass.async_create_task(self.async_update())

    @callback
    def _async_state_changed(self, event):
        """Handle state changes of tracked entities."""
        self.async_schedule_update_ha_state(True)

    @property
    def temperature_unit(self):
        return self._hass.config.units.temperature_unit

    @property
    def current_temperature(self):
        return self._temperature

    @property
    def target_temperature(self):
        return self._setpoint

    @property
    def preset_modes(self):
        return list(self._presets.keys())

    @property
    def preset_mode(self):
        return self._current_preset

    @property
    def hvac_action(self):
        """Return the current HVAC action."""
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
            
        # Check if heater is on
        heater_state = self._hass.states.get(self._heater)
        if heater_state and heater_state.state == 'on':
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def humidity(self):
        """Return the current humidity if available."""
        if not self._humidity_sensor:
            return None
            
        humidity_state = self._hass.states.get(self._humidity_sensor)
        if not humidity_state or humidity_state.state in ("unavailable", "unknown"):
            return None
            
        try:
            return float(humidity_state.state)
        except ValueError:
            return None

    @property
    def supported_features(self):
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attr_extra_state_attributes

    def _get_outdoor_temp(self):
        if self._outdoor_sensor:
            state = self._hass.states.get(self._outdoor_sensor)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except ValueError:
                    return None
        elif self._weather_sensor:
            weather_state = self._hass.states.get(self._weather_sensor)
            if weather_state:
                temp = weather_state.attributes.get("temperature")
                if temp is not None:  # Check if temperature attribute exists
                    try:
                        return float(temp)
                    except ValueError:
                        return None
        return None  # Return None if no valid outdoor temperature

    def _update_parameters(self, t_in, t_out, current_time):
        if self._last_off_start and self._attr_hvac_mode == HVACMode.OFF:
            t = (current_time - self._last_off_start) / 3600
            if t > 6 and self._last_off_tin is not None and t_out is not None : # Check for None
                try:
                    tau = -t / math.log((t_in - t_out) / (self._last_off_tin - t_out))
                except (ValueError, ZeroDivisionError):
                    tau = self._tau_avg # use average value
                self._tau_history.append(tau)
                self._tau_avg = sum(self._tau_history) / len(self._tau_history) if self._tau_history else 15.0
                self._last_off_start = None # keep it to avoid re-calculation
        if self._last_on_start and self._attr_hvac_mode == HVACMode.HEAT and t_in > self._setpoint + self._delta:
            t = (current_time - self._last_on_start) / 3600
            if self._last_on_tin is not None and t_out is not None: # Check for None
                rate = (t_in - self._last_on_tin) / t
                q = rate + (self._last_on_tin - t_out) / self._tau_avg
                self._q_history.append(q)
                self._q_avg = sum(self._q_history) / len(self._q_history) if self._q_history else 3.0
            self._last_on_start = None # keep it to avoid re-calculation

    async def async_update(self):
        """Update thermostat state."""
        temp_state = self._hass.states.get(self._temp_sensor)
        if temp_state is None or temp_state.state in ("unavailable", "unknown"):
            self._temperature = None
            return

        try:
            t_in = float(temp_state.state)
        except ValueError:
            self._temperature = None
            return

        # Get current temperature and update state
        self._temperature = t_in
        
        # Only control heating if in HEAT mode
        if self._attr_hvac_mode == HVACMode.HEAT:
            # Turn heating on/off based on temperature thresholds
            # using the more accurate 0.1 degree delta
            if t_in < self._setpoint - self._delta:
                domain = "switch" if "switch" in self._heater else "input_boolean"
                await self._hass.services.async_call(domain, "turn_on", {"entity_id": self._heater})
            elif t_in > self._setpoint + self._delta:
                domain = "switch" if "switch" in self._heater else "input_boolean"
                await self._hass.services.async_call(domain, "turn_off", {"entity_id": self._heater})

        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self._setpoint = temperature
            # Only update the heater if we're in HEAT mode
            if self._attr_hvac_mode == HVACMode.HEAT:
                await self.async_update()
            
            self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode):
        """Set new preset mode."""
        if preset_mode in self._presets:
            self._current_preset = preset_mode
            self._setpoint = self._presets[preset_mode]
            
            # Only update the heater if we're in HEAT mode
            if self._attr_hvac_mode == HVACMode.HEAT:
                await self.async_update()
                
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new HVAC mode."""
        if hvac_mode == HVACMode.HEAT:
            self._attr_hvac_mode = HVACMode.HEAT
            # Update immediately when turning on
            await self.async_update()
        elif hvac_mode == HVACMode.OFF:
            # Turn off heater when explicitly setting to OFF
            domain = "switch" if "switch" in self._heater else "input_boolean"
            await self._hass.services.async_call(domain, "turn_off", {"entity_id": self._heater})
            self._attr_hvac_mode = HVACMode.OFF
        else:
            return  # unsupported mode
        self.async_write_ha_state()
