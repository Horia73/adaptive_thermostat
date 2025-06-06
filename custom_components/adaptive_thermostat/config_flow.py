"""Config flow for Adaptive Thermostat integration."""
import logging
from typing import Any, Dict, Optional

import voluptuous as vol # type: ignore

from homeassistant import config_entries # type: ignore
from homeassistant.core import callback # type: ignore
from homeassistant.helpers import selector # type: ignore
from homeassistant.const import CONF_NAME # type: ignore

from .const import (
    DOMAIN,
    CONF_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    # CONF_WEATHER_SENSOR, # Removed as per user request
    CONF_HOME_PRESET,
    CONF_SLEEP_PRESET,
    CONF_AWAY_PRESET,
    CONF_CONFIG_TYPE,
    CONFIG_TYPE_INDIVIDUAL_ZONE,
    CONFIG_TYPE_CENTRAL_HEATER,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SELECT_TYPE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="Adaptive Thermostat"): str,
        vol.Required(CONF_CONFIG_TYPE, default=CONFIG_TYPE_INDIVIDUAL_ZONE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=CONFIG_TYPE_INDIVIDUAL_ZONE, label="Individual Zone (Heater/Valve)"),
                    selector.SelectOptionDict(value=CONFIG_TYPE_CENTRAL_HEATER, label="Central Heater"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

STEP_INDIVIDUAL_ZONE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "input_boolean", "climate"])
        ),
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
        ),
        vol.Required(CONF_OUTDOOR_SENSOR): selector.EntitySelector( # Mandatory
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
        ),
        vol.Optional(CONF_BACKUP_OUTDOOR_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature", multiple=False)
        ),
        vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"], device_class="humidity", multiple=False)
        ),
        vol.Optional(CONF_DOOR_WINDOW_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor"], device_class=["door", "window"], multiple=False)
        ),
        vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor"], device_class="motion", multiple=False)
        ),
        vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
        ),
        vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
        ),
        vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
        ),
    }
)

STEP_CENTRAL_HEATER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["climate", "switch", "input_boolean"])
        ),
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector( # Added as essential
            selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
        ) # Presets are not used for central heater
    }
)

def _validate_input(user_input: dict, schema: vol.Schema) -> dict:
    """Validate user input against schema, clean up empty optional fields."""
    validated_input = schema(user_input) # vol.Schema already handles raising errors
    cleaned_input = validated_input.copy() # Work on a copy

    for key_marker in schema.schema: # Iterate over schema keys (vol.Marker instances)
        key_str = key_marker.schema if isinstance(key_marker, vol.Marker) else key_marker
        
        if key_str in cleaned_input and cleaned_input[key_str] == "":
            # Correctly check if the schema key (marker) is Optional
            if isinstance(key_marker, vol.Optional):
                 cleaned_input[key_str] = None
    return cleaned_input

class AdaptiveThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Adaptive Thermostat."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize the config flow."""
        self._config_data = {}

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle the initial step where the user selects the configuration type."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                # Validate basic input first
                self._config_data.update(user_input)
                # Ensure unique name
                await self.async_set_unique_id(user_input[CONF_NAME])
                self._abort_if_unique_id_configured()

                config_type = user_input[CONF_CONFIG_TYPE]
                if config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
                    return await self.async_step_individual_zone_setup()
                elif config_type == CONFIG_TYPE_CENTRAL_HEATER:
                    return await self.async_step_central_heater_setup()
                else:
                    errors["base"] = "unknown_config_type"

            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in user step: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    if path == CONF_NAME and "already configured" in str(error.msg).lower():
                         errors["base"] = "name_exists"
                    else:
                        errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception as e: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in user step")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SELECT_TYPE_SCHEMA, errors=errors
        )

    async def async_step_individual_zone_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle the setup for an individual zone."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_INDIVIDUAL_ZONE_DATA_SCHEMA)
                self._config_data.update(validated_input)
                return self._async_create_entry()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in individual zone setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in individual_zone_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="individual_zone_setup",
            data_schema=STEP_INDIVIDUAL_ZONE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_central_heater_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle the setup for a central heater."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_CENTRAL_HEATER_DATA_SCHEMA)
                self._config_data.update(validated_input)
                return self._async_create_entry()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in central heater setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in central_heater_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="central_heater_setup",
            data_schema=STEP_CENTRAL_HEATER_DATA_SCHEMA,
            errors=errors,
        )

    def _async_create_entry(self):
        """Create the config entry from the stored data."""
        final_data = {k: v for k, v in self._config_data.items() if v is not None}
        _LOGGER.info(f"Creating Adaptive Thermostat entry: {final_data.get(CONF_NAME)}")
        _LOGGER.debug(f"Final config data for entry: {final_data}")
        return self.async_create_entry(title=final_data[CONF_NAME], data=final_data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return AdaptiveThermostatOptionsFlow(config_entry)


class AdaptiveThermostatOptionsFlow(config_entries.OptionsFlow):
    """Handle an options flow for Adaptive Thermostat."""

    def __init__(self, config_entry: config_entries.ConfigEntry):
        """Initialize options flow."""
        # self.config_entry is automatically set by the base class - no need to set it explicitly 
        self.current_options = dict(config_entry.options)
        self.config_type = config_entry.data.get(CONF_CONFIG_TYPE)
        self.initial_data = dict(config_entry.data)

    def _get_options_schema(self) -> vol.Schema:
        """Return the appropriate schema based on config_type, with suggested values."""
        options_schema_dict = {}

        individual_zone_fields = {
            vol.Required(CONF_HEATER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "input_boolean", "climate"])
            ),
            vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
            ),
            vol.Required(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
            ),
            vol.Optional(CONF_BACKUP_OUTDOOR_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature", multiple=False)
            ),
            vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class="humidity", multiple=False)
            ),
            vol.Optional(CONF_DOOR_WINDOW_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor"], device_class=["door", "window"], multiple=False)
            ),
            vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor"], device_class="motion", multiple=False)
            ),
            vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
            vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
            vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
        }

        central_heater_fields = {
            vol.Required(CONF_HEATER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["climate", "switch", "input_boolean"])
            ),
            vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class="temperature")
            ),
            vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
            vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
            vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode="box")
            ),
        }

        current_fields_def = {}
        if self.config_type == CONFIG_TYPE_INDIVIDUAL_ZONE:
            current_fields_def = individual_zone_fields
        elif self.config_type == CONFIG_TYPE_CENTRAL_HEATER:
            current_fields_def = central_heater_fields
        else:
            _LOGGER.error(f"Options flow: Unknown configuration type: {self.config_type}")
            return vol.Schema({})

        for key_obj, selector_config in current_fields_def.items():
            key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
            
            current_value = self.current_options.get(key_str, self.initial_data.get(key_str))
            
            # Handle default values properly, checking for vol.UNDEFINED
            if current_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                default_val = key_obj.default
                current_value = default_val() if callable(default_val) else default_val

            if isinstance(key_obj, vol.Optional):
                options_schema_dict[vol.Optional(key_str, description={"suggested_value": current_value if current_value is not None else ""})] = selector_config
            else: # vol.Required
                # For required fields, use the current value or the default from the key_obj
                default_value = current_value
                if default_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                    default_value = key_obj.default() if callable(key_obj.default) else key_obj.default
                options_schema_dict[vol.Required(key_str, default=default_value)] = selector_config
        
        return vol.Schema(options_schema_dict)

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Manage the options."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            updated_options = self.current_options.copy()
            try:
                current_schema = self._get_options_schema()
                processed_input = current_schema(user_input) # Validate types and apply defaults from schema

                for key, value in processed_input.items():
                    is_optional_field = False
                    for schema_key_obj in current_schema.schema:
                        schema_key_str = schema_key_obj.schema if isinstance(schema_key_obj, vol.Marker) else schema_key_obj
                        if schema_key_str == key and isinstance(schema_key_obj, vol.Optional):
                            is_optional_field = True
                            break
                    
                    # For optional fields, if value is None or empty string, remove from options
                    if is_optional_field and (value is None or value == ""):
                        updated_options.pop(key, None)
                    else:
                        updated_options[key] = value
                
                _LOGGER.debug(f"Options Flow: Updating entry with new options: {updated_options}")
                return self.async_create_entry(title="", data=updated_options)
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in options step: {e}")
                for error_detail in e.errors:
                    path = error_detail.path[0] if error_detail.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_option_input"
            except Exception as e: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error in options saving")
                errors["base"] = "unknown_options_error"

        options_schema = self._get_options_schema()
        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
            description_placeholders={
                "config_type_name": self.config_entry.data.get(CONF_CONFIG_TYPE, "Unknown").replace("_", " ").title()
            }
        )

           