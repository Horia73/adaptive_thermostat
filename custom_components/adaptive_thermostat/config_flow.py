"""Config flow for Adaptive Thermostat integration."""
import logging
import voluptuous as vol
from typing import Any, Dict

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
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_CONFIG_TYPE,
    CONFIG_TYPE_INDIVIDUAL_ZONE,
    CONFIG_TYPE_CENTRAL_HEATER,
    DEFAULT_NAME,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
)

_LOGGER = logging.getLogger(__name__)

# List of keys that are entity selectors - used for generic processing if needed
BASE_ENTITY_SELECTOR_KEYS = [
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
]

# Keys for OPTIONAL entity selectors in the INDIVIDUAL_ZONE configuration
OPTIONAL_INDIVIDUAL_ZONE_ENTITY_KEYS = [
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_WEATHER_SENSOR, 
    CONF_BACKUP_OUTDOOR_SENSOR,
]

# --- Schemas for Config Flow ---
STEP_USER_SELECT_TYPE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_CONFIG_TYPE, default=CONFIG_TYPE_INDIVIDUAL_ZONE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=CONFIG_TYPE_INDIVIDUAL_ZONE, label="Individual Zone (Heater/Valve)"),
                    selector.SelectOptionDict(value=CONFIG_TYPE_CENTRAL_HEATER, label="Central Heater"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            ),
        ),
    }
)

STEP_INDIVIDUAL_ZONE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "input_boolean", "climate"]),
        ),
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature"),
        ),
        vol.Required(CONF_OUTDOOR_SENSOR): selector.EntitySelector(), 
        vol.Optional(CONF_BACKUP_OUTDOOR_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature", multiple=False),
        ),
        vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="humidity", multiple=False),
        ),
        vol.Optional(CONF_DOOR_WINDOW_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor"], device_class=["door", "window"], multiple=False),
        ),
        vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor"], device_class="motion", multiple=False),
        ),
        vol.Optional(CONF_WEATHER_SENSOR): selector.EntitySelector(), 
        vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
        vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
        vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
    }
)

STEP_CENTRAL_HEATER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["climate", "switch", "input_boolean"]),
        ),
        vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
        vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
        vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box"),
        ),
    }
)


class AdaptiveThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1 

    def __init__(self) -> None:
        """Initialize config flow."""
        super().__init__()
        self.data: Dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return AdaptiveThermostatOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step where the user chooses the configuration type."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            self.data.update(user_input)
            await self.async_set_unique_id(user_input[CONF_NAME])
            self._abort_if_unique_id_configured()

            if user_input[CONF_CONFIG_TYPE] == CONFIG_TYPE_INDIVIDUAL_ZONE:
                return await self.async_step_individual_zone_setup()
            elif user_input[CONF_CONFIG_TYPE] == CONFIG_TYPE_CENTRAL_HEATER:
                return await self.async_step_central_heater_setup()
        
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SELECT_TYPE_SCHEMA,
            errors=errors,
        )

    async def async_step_individual_zone_setup(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the setup for an individual zone."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            self.data.update(user_input)
            processed_data = {**self.data} 
            
            for key in OPTIONAL_INDIVIDUAL_ZONE_ENTITY_KEYS:
                if processed_data.get(key) is None or processed_data.get(key) == "":
                    processed_data.pop(key, None)
            
            _LOGGER.debug(f"Creating entry for Individual Zone. Final data: {processed_data}")
            return self.async_create_entry(title=self.data[CONF_NAME], data=processed_data)

        return self.async_show_form(
            step_id="individual_zone_setup",
            data_schema=STEP_INDIVIDUAL_ZONE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_central_heater_setup(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the setup for a central heater."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            self.data.update(user_input)
            _LOGGER.debug(f"Creating entry for Central Heater. Final data: {self.data}")
            return self.async_create_entry(title=self.data[CONF_NAME], data=self.data)

        return self.async_show_form(
            step_id="central_heater_setup",
            data_schema=STEP_CENTRAL_HEATER_DATA_SCHEMA,
            errors=errors,
        )


class AdaptiveThermostatOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Adaptive Thermostat."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.config_type = self.config_entry.data.get(CONF_CONFIG_TYPE, CONFIG_TYPE_INDIVIDUAL_ZONE)
        self.current_options_and_data = {**self.config_entry.data, **self.config_entry.options}

    def _get_options_schema(self) -> vol.Schema:
        """Return the appropriate schema based on config_type, with suggested values."""
        if self.config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            schema = STEP_INDIVIDUAL_ZONE_DATA_SCHEMA
            fields = {
                vol.Required(CONF_HEATER, default=self.current_options_and_data.get(CONF_HEATER)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["switch", "input_boolean", "climate"])
                ),
                vol.Required(CONF_TEMP_SENSOR, default=self.current_options_and_data.get(CONF_TEMP_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
                ),
                vol.Required(CONF_OUTDOOR_SENSOR, default=self.current_options_and_data.get(CONF_OUTDOOR_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
                ),
                vol.Optional(CONF_BACKUP_OUTDOOR_SENSOR, description={"suggested_value": self.current_options_and_data.get(CONF_BACKUP_OUTDOOR_SENSOR)}): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature", multiple=False)
                ),
                vol.Optional(CONF_HUMIDITY_SENSOR, description={"suggested_value": self.current_options_and_data.get(CONF_HUMIDITY_SENSOR)}): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor"], device_class="humidity", multiple=False)
                ),
                vol.Optional(CONF_DOOR_WINDOW_SENSOR, description={"suggested_value": self.current_options_and_data.get(CONF_DOOR_WINDOW_SENSOR)}): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor"], device_class=["door", "window"], multiple=False)
                ),
                vol.Optional(CONF_MOTION_SENSOR, description={"suggested_value": self.current_options_and_data.get(CONF_MOTION_SENSOR)}): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor"], device_class="motion", multiple=False)
                ),
                vol.Optional(CONF_WEATHER_SENSOR, description={"suggested_value": self.current_options_and_data.get(CONF_WEATHER_SENSOR)}): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["weather", "sensor"], multiple=False)
                ),
                vol.Required(CONF_HOME_PRESET, default=self.current_options_and_data.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
                vol.Required(CONF_SLEEP_PRESET, default=self.current_options_and_data.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
                vol.Required(CONF_AWAY_PRESET, default=self.current_options_and_data.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
            }
            return vol.Schema(fields)
        elif self.config_type == CONFIG_TYPE_CENTRAL_HEATER:
            fields = {
                vol.Required(CONF_HEATER, default=self.current_options_and_data.get(CONF_HEATER)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["climate", "switch", "input_boolean"])
                ),
                vol.Required(CONF_HOME_PRESET, default=self.current_options_and_data.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
                vol.Required(CONF_SLEEP_PRESET, default=self.current_options_and_data.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
                vol.Required(CONF_AWAY_PRESET, default=self.current_options_and_data.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
                ),
            }
            return vol.Schema(fields)
        return vol.Schema({}) 

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            new_options = {}
            current_schema_keys = list(self._get_options_schema().schema.keys())
            
            for key_obj in current_schema_keys:
                key_name = key_obj.schema if isinstance(key_obj.schema, str) else key_obj
                if key_name in user_input:
                    value = user_input[key_name]
                    if value is not None and value != "":
                        new_options[key_name] = value
                    else:
                        _LOGGER.debug(f"Optional field {key_name} was cleared by user. It will be removed from options.")
            
            _LOGGER.debug(f"Options Flow: Updating entry with new options: {new_options}")
            return self.async_create_entry(title="", data=new_options)

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