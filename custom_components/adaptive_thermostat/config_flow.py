"""Config flow for Adaptive Thermostat integration."""
import logging
import voluptuous as vol
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.const import CONF_NAME

# Import constants
from .const import (
    DOMAIN,
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_SENSOR,
    CONF_SLEEP_PRESET,
    CONF_HOME_PRESET,
    CONF_AWAY_PRESET,
    DEFAULT_NAME,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
)

_LOGGER = logging.getLogger(__name__)

# List of keys that are entity selectors
ENTITY_SELECTOR_KEYS = [
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_SENSOR,
]

OPTIONAL_ENTITY_SELECTOR_KEYS = [ # Keys for OPTIONAL entity selectors
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_SENSOR,
]


# Schema for the initial user step (remains unchanged)
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=""): str,
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "input_boolean"]),
        ),
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor"], device_class="temperature"
            ),
        ),
        vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor"], device_class="humidity", multiple=False
            ),
        ),
        vol.Optional(CONF_DOOR_WINDOW_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor"], device_class=["door", "window"], multiple=False
            ),
        ),
        vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor"], device_class="motion", multiple=False
            ),
        ),
        vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor"], device_class="temperature", multiple=False
            ),
        ),
        vol.Optional(CONF_WEATHER_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor"], multiple=False
            ),
        ),
        vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, max=30, step=0.1, mode="box"
            ),
        ),
        vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, max=30, step=0.1, mode="box"
            ),
        ),
        vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, max=30, step=0.1, mode="box"
            ),
        ),
    }
)

# Helper function create_options_schema (No change needed)
def create_options_schema(current_config: dict[str, Any]) -> vol.Schema:
    """Create the schema for the options flow, pre-filling defaults."""
    name_default = current_config.get(CONF_NAME, DEFAULT_NAME)
    home_preset_default = current_config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET)
    sleep_preset_default = current_config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET)
    away_preset_default = current_config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET)

    humidity_sensor_default = current_config.get(CONF_HUMIDITY_SENSOR) # Returns None if key missing
    door_sensor_default = current_config.get(CONF_DOOR_WINDOW_SENSOR)
    motion_sensor_default = current_config.get(CONF_MOTION_SENSOR)
    outdoor_sensor_default = current_config.get(CONF_OUTDOOR_SENSOR)
    weather_sensor_default = current_config.get(CONF_WEATHER_SENSOR)

    _LOGGER.debug(f"[Schema Creation] Value used for {CONF_HUMIDITY_SENSOR} suggested_value: {humidity_sensor_default}")

    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=name_default): str,
            vol.Required(CONF_HEATER, default=current_config.get(CONF_HEATER)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "input_boolean"]),
            ),
            vol.Required(CONF_TEMP_SENSOR, default=current_config.get(CONF_TEMP_SENSOR)): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"], device_class="temperature"
                ),
            ),

            vol.Optional(CONF_HUMIDITY_SENSOR, default=humidity_sensor_default): selector.EntitySelector(

                selector.EntitySelectorConfig(
                    domain=["sensor"], device_class="humidity", multiple=False
                ),
            ),
            # ... other optional sensors ...

            vol.Optional(CONF_DOOR_WINDOW_SENSOR, default=door_sensor_default): selector.EntitySelector(

                selector.EntitySelectorConfig(
                    domain=["binary_sensor"], device_class=["door", "window"], multiple=False
                ),
            ),

            vol.Optional(CONF_MOTION_SENSOR, default=motion_sensor_default): selector.EntitySelector(

                selector.EntitySelectorConfig(
                    domain=["binary_sensor"], device_class="motion", multiple=False
                ),

            vol.Optional(CONF_OUTDOOR_SENSOR, default=outdoor_sensor_default): selector.EntitySelector(

                selector.EntitySelectorConfig(
                    domain=["sensor"], device_class="temperature", multiple=False
                ),
            ),

            vol.Optional(CONF_WEATHER_SENSOR, default=weather_sensor_default): selector.EntitySelector(

                selector.EntitySelectorConfig(
                    domain=["sensor"], multiple=False
                ),
            ),
            # ... presets ...
            vol.Required(CONF_HOME_PRESET, default=home_preset_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=30, step=0.1, mode="box"
                ),
            ),
            vol.Required(CONF_SLEEP_PRESET, default=sleep_preset_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=30, step=0.1, mode="box"
                ),
            ),
            vol.Required(CONF_AWAY_PRESET, default=away_preset_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=30, step=0.1, mode="box"
                ),
            ),
        }
    )


# Config Flow Handler (remains unchanged)
class AdaptiveThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        _LOGGER.debug("Mimic VT Flow: Returning instance of AdaptiveThermostatOptionsFlow(%s)", config_entry.entry_id)
        return AdaptiveThermostatOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # (Using Key Deletion logic from previous attempt)
        errors = {}
        if user_input is not None:
            final_data = {}
            for key, value in user_input.items():

                if key in OPTIONAL_ENTITY_SELECTOR_KEYS and (value is None or value == ""):
                    _LOGGER.debug("User Step: Skipping key '%s' due to empty value", key)
                    continue
                elif value is not None and value != "":
                    final_data[key] = value


            if not final_data.get(CONF_HEATER) or not final_data.get(CONF_TEMP_SENSOR):
                 errors["base"] = "heater_or_temp_missing"

            if not errors:
                 _LOGGER.debug("Creating config entry with data: %s", final_data)
                 return self.async_create_entry(
                     title=final_data.get(CONF_NAME, DEFAULT_NAME),
                     data=final_data
                 )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


# Options Flow Handler (Mimicking VT State Management)
class AdaptiveThermostatOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Adaptive Thermostat."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        _LOGGER.warning("Mimic VT Flow: Using DEPRECATED __init__")
        # Store config_entry directly (DEPRECATED)
        self.config_entry = config_entry
        # Initialize _infos with merged data and options
        # We use this dict internally like VT does
        self._infos = {**config_entry.data, **config_entry.options}
        _LOGGER.debug("Mimic VT Flow __init__: Initial self._infos: %s", self._infos)


    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        errors = {}
        _LOGGER.debug("Mimic VT Flow: init step START for %s", self.config_entry.entry_id)

        # --- Process Submitted Input ---
        if user_input is not None:
            _LOGGER.debug("Mimic VT Flow [submit] - Received user input: %s", user_input)

            # --- Update internal self._infos dictionary ---
            # Start with the current state stored in self._infos
            # (This assumes only one step, so _infos holds state from __init__)
            working_options = self._infos.copy()

            # Iterate through user_input provided by the form
            for key, value in user_input.items():

                if key in OPTIONAL_ENTITY_SELECTOR_KEYS:
                    if value is None or value == "":  # User cleared the field
                        if key in working_options:
                            _LOGGER.debug("Mimic VT Flow [submit] - Deleting key '%s' from options", key)
                            del working_options[key]
                    else:
                        working_options[key] = value
                else:
                    # Update non-optional fields provided by the form
                    working_options[key] = value


            # Now self._infos holds the desired final state (with cleared keys removed)
            self._infos = working_options
            _LOGGER.debug("Mimic VT Flow [submit] - Updated self._infos: %s", self._infos)

            # --- Validation (on the updated self._infos) ---
            # Check REQUIRED fields (should always exist, either from data or options)
            if not self._infos.get(CONF_HEATER): errors["base"] = "heater_missing"
            elif not self._infos.get(CONF_TEMP_SENSOR): errors["base"] = "temp_sensor_missing"
            if CONF_HOME_PRESET not in self._infos: errors["base"] = "home_preset_missing"
            # ... other validation ...

            if not errors:
                _LOGGER.debug("Mimic VT Flow [submit] - Saving self._infos as options: %s", self._infos)
                # --- Use standard method to save the *entire* self._infos dict as options ---
                # This replaces the existing options completely.
                return self.async_create_entry(title="", data=self._infos)
            else:
                _LOGGER.warning("Mimic VT Flow [submit] - Validation errors: %s", errors)
        # --- End Process Submitted Input ---


        # --- Show Form ---
        # Use self._infos (which holds current state) to create the schema
        _LOGGER.debug("Mimic VT Flow [init] - Creating schema from self._infos: %s", self._infos)
        humidity_sensor_value_read = self._infos.get(CONF_HUMIDITY_SENSOR) # Read from internal dict
        _LOGGER.warning(f"Mimic VT Flow [init] - Value read for {CONF_HUMIDITY_SENSOR}: '{humidity_sensor_value_read}' (Type: {type(humidity_sensor_value_read)})")

        options_schema = create_options_schema(self._infos) # Pass internal dict
        _LOGGER.debug("Mimic VT Flow [init] - Showing form.")
        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )

