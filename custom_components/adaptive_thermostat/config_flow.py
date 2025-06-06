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
    CONF_CENTRAL_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_HOME_PRESET,
    CONF_SLEEP_PRESET,
    CONF_AWAY_PRESET,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_AUTO_OFF_TEMP,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
    CENTRAL_HEATER_TURN_ON_DELAY,
    CENTRAL_HEATER_TURN_OFF_DELAY,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_AUTO_OFF_TEMP,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="Adaptive Thermostat"): str,
    }
)

STEP_ZONE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "input_boolean", "climate", "valve"])
        ),
        vol.Optional(CONF_CENTRAL_HEATER): selector.EntitySelector(
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
    }
)

STEP_TIMING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CENTRAL_HEATER_TURN_ON_DELAY, default=CENTRAL_HEATER_TURN_ON_DELAY): selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=60, step=1, mode="box", unit_of_measurement="seconds")
        ),
        vol.Required(CONF_CENTRAL_HEATER_TURN_OFF_DELAY, default=CENTRAL_HEATER_TURN_OFF_DELAY): selector.NumberSelector(
            selector.NumberSelectorConfig(min=30, max=300, step=10, mode="box", unit_of_measurement="seconds")
        ),
    }
)

STEP_AUTO_ONOFF_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTO_ON_OFF_ENABLED, default=False): selector.BooleanSelector(),
        vol.Required(CONF_AUTO_ON_TEMP, default=DEFAULT_AUTO_ON_TEMP): selector.NumberSelector(
            selector.NumberSelectorConfig(min=-10, max=25, step=0.5, mode="box", unit_of_measurement="°C")
        ),
        vol.Required(CONF_AUTO_OFF_TEMP, default=DEFAULT_AUTO_OFF_TEMP): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=35, step=0.5, mode="box", unit_of_measurement="°C")
        ),
    }
)

STEP_PRESETS_SCHEMA = vol.Schema(
    {
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

def _validate_input(user_input: dict, schema: vol.Schema) -> dict:
    """Validate user input against schema, clean up empty optional fields."""
    validated_input = schema(user_input)
    cleaned_input = validated_input.copy()

    for key_marker in schema.schema:
        key_str = key_marker.schema if isinstance(key_marker, vol.Marker) else key_marker
        
        if key_str in cleaned_input and cleaned_input[key_str] == "":
            if isinstance(key_marker, vol.Optional):
                 cleaned_input[key_str] = None
    return cleaned_input

class AdaptiveThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Adaptive Thermostat."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self._config_data = {}

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle the initial step where the user enters the zone name."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                self._config_data.update(user_input)
                # Ensure unique name
                await self.async_set_unique_id(user_input[CONF_NAME])
                self._abort_if_unique_id_configured()
                
                return await self.async_step_zone_setup()

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
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_zone_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle the zone setup."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_ZONE_DATA_SCHEMA)
                self._config_data.update(validated_input)
                
                # Check if central heater is configured to determine next step
                if self._config_data.get(CONF_CENTRAL_HEATER):
                    return await self.async_step_timing_setup()
                else:
                    return await self.async_step_auto_onoff_setup()
                
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in zone setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in zone_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="zone_setup",
            data_schema=STEP_ZONE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_timing_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle central heater timing configuration."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_TIMING_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_auto_onoff_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in timing setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in timing_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="timing_setup",
            data_schema=STEP_TIMING_SCHEMA,
            errors=errors,
        )

    async def async_step_auto_onoff_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle auto on/off configuration."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_AUTO_ONOFF_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_presets_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in auto on/off setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in auto_onoff_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="auto_onoff_setup",
            data_schema=STEP_AUTO_ONOFF_SCHEMA,
            errors=errors,
        )

    async def async_step_presets_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle temperature presets configuration."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_PRESETS_SCHEMA)
                self._config_data.update(validated_input)
                return self._async_create_entry()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in presets setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in presets_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="presets_setup",
            data_schema=STEP_PRESETS_SCHEMA,
            errors=errors,
        )

    def _async_create_entry(self):
        """Create the config entry from the stored data."""
        final_data = {k: v for k, v in self._config_data.items() if v is not None}
        _LOGGER.info(f"Creating Adaptive Thermostat entry: {final_data.get(CONF_NAME)}")
        _LOGGER.debug(f"Final config data for entry: {final_data}")
        return self.async_create_entry(title=final_data[CONF_NAME], data=final_data)

    async def async_step_reconfigure(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of the integration."""
        config_entry = self._get_reconfigure_entry()
        
        _LOGGER.info(f"[RECONFIGURE] Starting full reconfiguration for {config_entry.title}")
        
        # Initialize with current config data for reconfiguration
        self._config_data = dict(config_entry.data)
        
        _LOGGER.debug(f"[RECONFIGURE] Current config data: {self._config_data}")
        
        # Always start with name setup for full reconfiguration
        return await self.async_step_reconfigure_name_setup()

    async def async_step_reconfigure_name_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of the thermostat name."""
        config_entry = self._get_reconfigure_entry()
        errors: Dict[str, str] = {}
        
        _LOGGER.info(f"[RECONFIGURE] Name setup step for {config_entry.title}")
        
        if user_input is not None:
            _LOGGER.debug(f"[RECONFIGURE] Name setup received input: {user_input}")
            try:
                validated_input = _validate_input(user_input, STEP_USER_DATA_SCHEMA)
                self._config_data.update(validated_input)
                
                _LOGGER.info(f"[RECONFIGURE] Name updated, proceeding to zone setup")
                # Always continue to zone setup
                return await self.async_step_reconfigure_zone_setup()
                
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in reconfigure name setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure name setup")
                errors["base"] = "unknown"

        # Create simple schema with current name
        current_data = config_entry.data
        current_name = current_data.get(CONF_NAME, config_entry.title)
        
        _LOGGER.debug(f"[RECONFIGURE] Showing name form with current name: {current_name}")
        
        schema_dict = {vol.Required(CONF_NAME, default=current_name): str}

        return self.async_show_form(
            step_id="reconfigure_name_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure_zone_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of zone setup."""
        config_entry = self._get_reconfigure_entry()
        errors: Dict[str, str] = {}
        
        if user_input is not None:
            try:
                # Ensure unique ID remains the same
                await self.async_set_unique_id(config_entry.unique_id)
                self._abort_if_unique_id_mismatch()
                
                validated_input = _validate_input(user_input, STEP_ZONE_DATA_SCHEMA)
                self._config_data.update(validated_input)
                
                # Always continue to timing setup
                return await self.async_step_reconfigure_timing_setup()
                
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in reconfigure zone setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure zone setup")
                errors["base"] = "unknown"

        # Pre-populate form with current values
        current_data = config_entry.data
        schema_dict = {}
        
        for key_obj, selector_config in STEP_ZONE_DATA_SCHEMA.schema.items():
            key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
            current_value = current_data.get(key_str)
            
            if isinstance(key_obj, vol.Optional):
                schema_dict[vol.Optional(key_str, description={"suggested_value": current_value or ""})] = selector_config
            else:
                default_value = current_value
                if default_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                    default_value = key_obj.default() if callable(key_obj.default) else key_obj.default
                schema_dict[vol.Required(key_str, default=default_value)] = selector_config
        
        return self.async_show_form(
            step_id="reconfigure_zone_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure_timing_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of central heater timing."""
        config_entry = self._get_reconfigure_entry()
        errors: Dict[str, str] = {}
        
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_TIMING_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_auto_onoff_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in reconfigure timing setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure timing setup")
                errors["base"] = "unknown"

        # Pre-populate form with current values or defaults
        current_data = config_entry.data
        schema_dict = {}
        for key_obj, selector_config in STEP_TIMING_SCHEMA.schema.items():
            key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
            current_value = current_data.get(key_str)
            
            # Use current value or fall back to schema default
            if current_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                current_value = key_obj.default() if callable(key_obj.default) else key_obj.default
            
            schema_dict[vol.Required(key_str, default=current_value)] = selector_config
        
        return self.async_show_form(
            step_id="reconfigure_timing_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure_auto_onoff_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of auto on/off settings."""
        config_entry = self._get_reconfigure_entry()
        errors: Dict[str, str] = {}
        
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_AUTO_ONOFF_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_presets_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in reconfigure auto on/off setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure auto on/off setup")
                errors["base"] = "unknown"

        # Pre-populate form with current values or defaults
        current_data = config_entry.data
        schema_dict = {}
        for key_obj, selector_config in STEP_AUTO_ONOFF_SCHEMA.schema.items():
            key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
            current_value = current_data.get(key_str)
            
            # Use current value or fall back to schema default
            if current_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                current_value = key_obj.default() if callable(key_obj.default) else key_obj.default
            
            schema_dict[vol.Required(key_str, default=current_value)] = selector_config
        
        return self.async_show_form(
            step_id="reconfigure_auto_onoff_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure_presets_setup(self, user_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle reconfiguration of temperature presets."""
        config_entry = self._get_reconfigure_entry()
        errors: Dict[str, str] = {}
        
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_PRESETS_SCHEMA)
                self._config_data.update(validated_input)
                
                # Complete reconfiguration
                return self.async_update_reload_and_abort(
                    config_entry,
                    data_updates=self._config_data,
                )
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in reconfigure presets setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception: # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure presets setup")
                errors["base"] = "unknown"

        # Pre-populate form with current values or defaults
        current_data = config_entry.data
        schema_dict = {}
        for key_obj, selector_config in STEP_PRESETS_SCHEMA.schema.items():
            key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
            current_value = current_data.get(key_str)
            
            # Use current value or fall back to schema default
            if current_value is None and hasattr(key_obj, 'default') and key_obj.default is not vol.UNDEFINED:
                current_value = key_obj.default() if callable(key_obj.default) else key_obj.default
            
            schema_dict[vol.Required(key_str, default=current_value)] = selector_config
        
        return self.async_show_form(
            step_id="reconfigure_presets_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )