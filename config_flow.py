"""Config flow for Adaptive Thermostat integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.const import (
    CONF_NAME,
)

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Configuration options
CONF_HEATER = "heater"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_DOOR_WINDOW_SENSOR = "door_window_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_WEATHER_SENSOR = "weather_sensor"
CONF_OUTSIDE_TEMP_OFF = "outside_temp_off"
CONF_SLEEP_PRESET = "sleep_preset"
CONF_HOME_PRESET = "home_preset"
CONF_AWAY_PRESET = "away_preset"


class AdaptiveThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Adaptive Thermostat."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return AdaptiveThermostatOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Validate user input
            if not user_input.get(CONF_HEATER):
                errors[CONF_HEATER] = "missing_heater"
            if not user_input.get(CONF_TEMP_SENSOR):
                errors[CONF_TEMP_SENSOR] = "missing_temp_sensor"
            if not user_input.get(CONF_WEATHER_SENSOR) and not user_input.get(CONF_OUTDOOR_SENSOR):
                errors[CONF_WEATHER_SENSOR] = "missing_outdoor_data"

            if not errors:
                # Create entry
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )

        # Prepare default values
        default_values = {
            CONF_OUTSIDE_TEMP_OFF: 20,
            CONF_SLEEP_PRESET: 21,
            CONF_HOME_PRESET: 22,
            CONF_AWAY_PRESET: 18,
        }

        # Use provided values or defaults
        suggested_values = user_input or default_values

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_HEATER): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["switch", "input_boolean"]
                        ),
                    ),
                    vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor"],
                            device_class=["temperature"],
                        ),
                    ),
                    vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor"],
                            device_class=["humidity"],
                        ),
                    ),
                    vol.Optional(CONF_DOOR_WINDOW_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            device_class=["door", "window"],
                        ),
                    ),
                    vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            device_class=["motion"],
                        ),
                    ),
                    vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor"],
                            device_class=["temperature"],
                        ),
                    ),
                    vol.Required(CONF_WEATHER_SENSOR, default=suggested_values.get(CONF_WEATHER_SENSOR)): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["weather"],
                        ),
                    ),
                    vol.Required(
                        CONF_OUTSIDE_TEMP_OFF,
                        default=suggested_values.get(CONF_OUTSIDE_TEMP_OFF),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=40,
                            step=0.1,
                            unit_of_measurement="°C",
                        ),
                    ),
                    vol.Required(
                        CONF_SLEEP_PRESET,
                        default=suggested_values.get(CONF_SLEEP_PRESET),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=10,
                            max=30,
                            step=0.1,
                            unit_of_measurement="°C",
                        ),
                    ),
                    vol.Required(
                        CONF_HOME_PRESET,
                        default=suggested_values.get(CONF_HOME_PRESET),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=10,
                            max=30,
                            step=0.1,
                            unit_of_measurement="°C",
                        ),
                    ),
                    vol.Required(
                        CONF_AWAY_PRESET,
                        default=suggested_values.get(CONF_AWAY_PRESET),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=10,
                            max=30,
                            step=0.1,
                            unit_of_measurement="°C",
                        ),
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "name_desc": "name_desc",
                "heater_desc": "heater_desc",
                "temp_sensor_desc": "temp_sensor_desc",
                "humidity_sensor_desc": "humidity_sensor_desc",
                "door_window_sensor_desc": "door_window_sensor_desc",
                "motion_sensor_desc": "motion_sensor_desc",
                "outdoor_sensor_desc": "outdoor_sensor_desc",
                "weather_sensor_desc": "weather_sensor_desc",
                "outside_temp_off_desc": "outside_temp_off_desc",
                "sleep_preset_desc": "sleep_preset_desc",
                "home_preset_desc": "home_preset_desc",
                "away_preset_desc": "away_preset_desc",
            },
        )


class AdaptiveThermostatOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Adaptive Thermostat."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.data)

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Manage the options."""
        errors = {}

        if user_input is not None:
            # Validate user input
            if not user_input.get(CONF_HEATER):
                errors[CONF_HEATER] = "missing_heater"
            if not user_input.get(CONF_TEMP_SENSOR):
                errors[CONF_TEMP_SENSOR] = "missing_temp_sensor"
            if not user_input.get(CONF_WEATHER_SENSOR) and not user_input.get(CONF_OUTDOOR_SENSOR):
                errors[CONF_WEATHER_SENSOR] = "missing_outdoor_data"

            if not errors:
                # Update entry - completely replace the data instead of updating it
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={} # First clear it
                )
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=user_input # Then set the new data
                )
                return self.async_create_entry(title="", data=user_input)

        # Get the data schema with optional fields properly handled
        schema = {
            vol.Required(CONF_NAME, default=self.options.get(CONF_NAME)): str,
            vol.Required(CONF_HEATER, default=self.options.get(CONF_HEATER)): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "input_boolean"]
                ),
            ),
            vol.Required(CONF_TEMP_SENSOR, default=self.options.get(CONF_TEMP_SENSOR)): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["temperature"],
                ),
            ),
        }
        
        # Only add optional fields if they have values
        # Use .get() with None as the default value
        humidity_sensor = self.options.get(CONF_HUMIDITY_SENSOR)
        if humidity_sensor is not None:
            schema[vol.Optional(CONF_HUMIDITY_SENSOR, default=humidity_sensor)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["humidity"],
                ),
            )
        else:
            schema[vol.Optional(CONF_HUMIDITY_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["humidity"],
                ),
            )
            
        door_window_sensor = self.options.get(CONF_DOOR_WINDOW_SENSOR)
        if door_window_sensor is not None:
            schema[vol.Optional(CONF_DOOR_WINDOW_SENSOR, default=door_window_sensor)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor"],
                    device_class=["door", "window"],
                ),
            )
        else:
            schema[vol.Optional(CONF_DOOR_WINDOW_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor"],
                    device_class=["door", "window"],
                ),
            )
            
        motion_sensor = self.options.get(CONF_MOTION_SENSOR)
        if motion_sensor is not None:
            schema[vol.Optional(CONF_MOTION_SENSOR, default=motion_sensor)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor"],
                    device_class=["motion"],
                ),
            )
        else:
            schema[vol.Optional(CONF_MOTION_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor"],
                    device_class=["motion"],
                ),
            )
            
        outdoor_sensor = self.options.get(CONF_OUTDOOR_SENSOR)
        if outdoor_sensor is not None:
            schema[vol.Optional(CONF_OUTDOOR_SENSOR, default=outdoor_sensor)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["temperature"],
                ),
            )
        else:
            schema[vol.Optional(CONF_OUTDOOR_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["temperature"],
                ),
            )
        
        # Add remaining required fields
        schema.update({
            vol.Required(CONF_WEATHER_SENSOR, default=self.options.get(CONF_WEATHER_SENSOR)): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["weather"],
                ),
            ),
            vol.Required(
                CONF_OUTSIDE_TEMP_OFF,
                default=self.options.get(CONF_OUTSIDE_TEMP_OFF),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=40,
                    step=0.1,
                    unit_of_measurement="°C",
                ),
            ),
            vol.Required(
                CONF_SLEEP_PRESET,
                default=self.options.get(CONF_SLEEP_PRESET),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=30,
                    step=0.1,
                    unit_of_measurement="°C",
                ),
            ),
            vol.Required(
                CONF_HOME_PRESET,
                default=self.options.get(CONF_HOME_PRESET),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=30,
                    step=0.1,
                    unit_of_measurement="°C",
                ),
            ),
            vol.Required(
                CONF_AWAY_PRESET,
                default=self.options.get(CONF_AWAY_PRESET),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=30,
                    step=0.1,
                    unit_of_measurement="°C",
                ),
            ),
        })

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "name_desc": "name_desc",
                "heater_desc": "heater_desc",
                "temp_sensor_desc": "temp_sensor_desc",
                "humidity_sensor_desc": "humidity_sensor_desc",
                "door_window_sensor_desc": "door_window_sensor_desc",
                "motion_sensor_desc": "motion_sensor_desc",
                "outdoor_sensor_desc": "outdoor_sensor_desc",
                "weather_sensor_desc": "weather_sensor_desc",
                "outside_temp_off_desc": "outside_temp_off_desc",
                "sleep_preset_desc": "sleep_preset_desc",
                "home_preset_desc": "home_preset_desc",
                "away_preset_desc": "away_preset_desc",
            },
        )