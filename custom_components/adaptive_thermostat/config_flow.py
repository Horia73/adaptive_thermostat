"""Config flow for Adaptive Thermostat integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CENTRAL_HEATER_TURN_OFF_DELAY,
    CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_AUTO_OFF_TEMP,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_AWAY_PRESET,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_CENTRAL_HEATER,
    CONF_CENTRAL_HEATER_MIN_OFF,
    CONF_CENTRAL_HEATER_MIN_ON,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_HEATER,
    CONF_HOME_PRESET,
    CONF_HUMIDITY_SENSOR,
    CONF_LINKED_WINDOW_ZONES,
    CONF_MAX_BURST,
    CONF_MIN_BURST,
    CONF_MODEL_CONTROL,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR_STALE_TIMEOUT,
    CONF_SLEEP_PRESET,
    CONF_TARGET_TOLERANCE,
    CONF_TEMP_SENSOR,
    CONF_WINDOW_DETECTION_ENABLED,
    CONF_WINDOW_SLOPE_THRESHOLD,
    DEFAULT_AUTO_OFF_TEMP,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_AWAY_PRESET,
    DEFAULT_CENTRAL_HEATER_MIN_OFF,
    DEFAULT_CENTRAL_HEATER_MIN_ON,
    DEFAULT_HOME_PRESET,
    DEFAULT_MAX_BURST,
    DEFAULT_MIN_BURST,
    DEFAULT_MODEL_CONTROL,
    DEFAULT_SENSOR_STALE_TIMEOUT,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_WINDOW_DETECTION_ENABLED,
    DEFAULT_WINDOW_SLOPE_THRESHOLD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
    }
)

STEP_ZONE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["switch", "input_boolean", "climate", "valve"], multiple=True
            )
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
            selector.EntitySelectorConfig(
                domain=["binary_sensor"], device_class=["door", "window"], multiple=True
            )
        ),
        vol.Optional(CONF_LINKED_WINDOW_ZONES): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["climate"], integration=DOMAIN, multiple=True)
        ),
        vol.Optional(CONF_MOTION_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor"], device_class="motion", multiple=False)
        ),
    }
)

STEP_TIMING_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_CENTRAL_HEATER_TURN_ON_DELAY, default=CENTRAL_HEATER_TURN_ON_DELAY
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=5, max=60, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="seconds"
            )
        ),
        vol.Required(
            CONF_CENTRAL_HEATER_TURN_OFF_DELAY, default=CENTRAL_HEATER_TURN_OFF_DELAY
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=30, max=300, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="seconds"
            )
        ),
        vol.Required(
            CONF_CENTRAL_HEATER_MIN_ON, default=DEFAULT_CENTRAL_HEATER_MIN_ON
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=900, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="seconds"
            )
        ),
        vol.Required(
            CONF_CENTRAL_HEATER_MIN_OFF, default=DEFAULT_CENTRAL_HEATER_MIN_OFF
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=1800, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="seconds"
            )
        ),
    }
)

STEP_AUTO_ONOFF_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTO_ON_OFF_ENABLED, default=False): selector.BooleanSelector(),
        vol.Required(CONF_AUTO_ON_TEMP, default=DEFAULT_AUTO_ON_TEMP): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-10, max=25, step=0.5, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C"
            )
        ),
        vol.Required(CONF_AUTO_OFF_TEMP, default=DEFAULT_AUTO_OFF_TEMP): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, max=35, step=0.5, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C"
            )
        ),
    }
)

STEP_ADAPTIVE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TARGET_TOLERANCE, default=DEFAULT_TARGET_TOLERANCE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.02, max=0.5, step=0.01, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C"
            )
        ),
        vol.Required(
            CONF_WINDOW_DETECTION_ENABLED, default=DEFAULT_WINDOW_DETECTION_ENABLED
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_WINDOW_SLOPE_THRESHOLD, default=DEFAULT_WINDOW_SLOPE_THRESHOLD
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5, max=15.0, step=0.5, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C/h"
            )
        ),
        vol.Required(
            CONF_SENSOR_STALE_TIMEOUT, default=DEFAULT_SENSOR_STALE_TIMEOUT
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=30, max=1440, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
            )
        ),
        vol.Required(CONF_MIN_BURST, default=DEFAULT_MIN_BURST): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=60, max=900, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="seconds"
            )
        ),
        vol.Required(CONF_MAX_BURST, default=DEFAULT_MAX_BURST): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10, max=120, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
            )
        ),
        vol.Required(CONF_MODEL_CONTROL, default=DEFAULT_MODEL_CONTROL): selector.BooleanSelector(),
    }
)

STEP_PRESETS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOME_PRESET, default=DEFAULT_HOME_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_SLEEP_PRESET, default=DEFAULT_SLEEP_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_AWAY_PRESET, default=DEFAULT_AWAY_PRESET): selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=30, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
    }
)


# Fields stored as a list; 2.0 stored the contact sensor as a single string.
LIST_FIELDS = (CONF_DOOR_WINDOW_SENSOR, CONF_LINKED_WINDOW_ZONES)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _normalize_lists(data: dict) -> dict:
    """Accept a single entity id where a list is expected (older entries, REST)."""
    out = dict(data)
    for key in LIST_FIELDS:
        if key in out and out[key] is not None:
            out[key] = _as_list(out[key])
    return out


def _schema_with_current(
    schema: vol.Schema, current: dict[str, Any], overrides: dict[str, Any] | None = None
) -> vol.Schema:
    """``schema`` with every field defaulting to the entry's current value.

    Required fields get ``default`` (current value, else the schema default),
    so submitting ``{}`` keeps them.  Optional fields get a suggested value:
    an omitted optional field is not touched either, because the step only
    updates the keys it receives.
    """
    schema_dict: dict[Any, Any] = {}
    for key_obj, selector_config in schema.schema.items():
        key_str = key_obj.schema if isinstance(key_obj, vol.Marker) else key_obj
        selector_config = (overrides or {}).get(key_str, selector_config)
        current_value = current.get(key_str)
        if key_str in LIST_FIELDS:
            current_value = _as_list(current_value)
        if isinstance(key_obj, vol.Optional):
            suggested = current_value if current_value is not None else ""
            schema_dict[vol.Optional(key_str, description={"suggested_value": suggested})] = selector_config
            continue
        if current_value is None and hasattr(key_obj, "default") and key_obj.default is not vol.UNDEFINED:
            current_value = key_obj.default() if callable(key_obj.default) else key_obj.default
        schema_dict[vol.Required(key_str, default=current_value)] = selector_config
    return vol.Schema(schema_dict)


def _validate_input(user_input: dict, schema: vol.Schema) -> dict:
    """Validate user input against schema, clean up empty optional fields."""
    validated_input = schema(_normalize_lists(user_input))
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step where the user enters the zone name."""
        errors: dict[str, str] = {}
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
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in user step")
                errors["base"] = "unknown"

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def async_step_zone_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the zone setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_ZONE_DATA_SCHEMA)
                self._config_data.update(validated_input)

                # Check if central heater is configured to determine next step
                central_heater = self._config_data.get(CONF_CENTRAL_HEATER)
                if central_heater and central_heater.strip():  # Only if central heater is actually selected
                    _LOGGER.debug(f"Central heater configured: {central_heater}, showing timing setup")
                    return await self.async_step_timing_setup()
                else:
                    _LOGGER.debug("No central heater configured, skipping timing setup")
                    return await self.async_step_auto_onoff_setup()

            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in zone setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in zone_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="zone_setup",
            data_schema=STEP_ZONE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_timing_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle central heater timing configuration."""
        errors: dict[str, str] = {}
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
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in timing_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="timing_setup",
            data_schema=STEP_TIMING_SCHEMA,
            errors=errors,
        )

    async def async_step_auto_onoff_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle auto on/off configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_AUTO_ONOFF_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_adaptive_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in auto on/off setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in auto_onoff_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="auto_onoff_setup",
            data_schema=STEP_AUTO_ONOFF_SCHEMA,
            errors=errors,
        )

    async def async_step_adaptive_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle adaptive control configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_ADAPTIVE_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_presets_setup()
            except vol.MultipleInvalid as e:
                _LOGGER.error(f"Validation error in adaptive control setup: {e}")
                for error in e.errors:
                    path = error.path[0] if error.path else "base"
                    errors[path if isinstance(path, str) else "base"] = "invalid_input"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in adaptive_setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="adaptive_setup",
            data_schema=STEP_ADAPTIVE_SCHEMA,
            errors=errors,
        )

    async def async_step_presets_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle temperature presets configuration."""
        errors: dict[str, str] = {}
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
            except Exception:  # pylint: disable=broad-except
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

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of the integration.

        Steps: reconfigure_name_setup -> reconfigure_zone_setup ->
        [reconfigure_timing_setup, only with a central heater] ->
        reconfigure_auto_onoff_setup -> reconfigure_adaptive_setup ->
        reconfigure_presets_setup.  Every field defaults to the entry's
        current value, so ``{}`` keeps a step unchanged.
        """
        config_entry = self._get_reconfigure_entry()

        _LOGGER.info(f"[RECONFIGURE] Starting full reconfiguration for {config_entry.title}")

        # Current configuration as the zone runs it (options written by the
        # 1.x options flow override data); saved back into data at the end.
        self._config_data = {**config_entry.data, **config_entry.options}

        _LOGGER.debug(f"[RECONFIGURE] Current config data: {self._config_data}")

        # Always start with name setup for full reconfiguration
        return await self.async_step_reconfigure_name_setup()

    @staticmethod
    def _invalid_errors(step: str, err: vol.MultipleInvalid) -> dict[str, str]:
        _LOGGER.error(f"Validation error in reconfigure {step}: {err}")
        errors: dict[str, str] = {}
        for error in err.errors:
            path = error.path[0] if error.path else "base"
            errors[path if isinstance(path, str) else "base"] = "invalid_input"
        return errors

    async def async_step_reconfigure_name_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the thermostat name."""
        config_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_USER_DATA_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_zone_setup()
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("name setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure name setup")
                errors["base"] = "unknown"

        current = {CONF_NAME: config_entry.title, **self._config_data}
        return self.async_show_form(
            step_id="reconfigure_name_setup",
            data_schema=_schema_with_current(STEP_USER_DATA_SCHEMA, current),
            errors=errors,
        )

    async def async_step_reconfigure_zone_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of zone setup (entities, contacts, linked zones)."""
        config_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                # Ensure unique ID remains the same
                await self.async_set_unique_id(config_entry.unique_id)
                self._abort_if_unique_id_mismatch()

                validated_input = _validate_input(user_input, STEP_ZONE_DATA_SCHEMA)
                self._config_data.update(validated_input)

                # Check if central heater is configured to determine next step
                central_heater = self._config_data.get(CONF_CENTRAL_HEATER)
                if central_heater and central_heater.strip():  # Only if central heater is actually selected
                    return await self.async_step_reconfigure_timing_setup()
                return await self.async_step_reconfigure_auto_onoff_setup()
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("zone setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure zone setup")
                errors["base"] = "unknown"

        # A zone cannot be linked to itself.
        own_entity = self.hass.data.get(DOMAIN, {}).get("entry_to_entity_id", {}).get(config_entry.entry_id)
        linked_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["climate"],
                integration=DOMAIN,
                multiple=True,
                exclude_entities=[own_entity] if own_entity else [],
            )
        )
        return self.async_show_form(
            step_id="reconfigure_zone_setup",
            data_schema=_schema_with_current(
                STEP_ZONE_DATA_SCHEMA, self._config_data, {CONF_LINKED_WINDOW_ZONES: linked_selector}
            ),
            errors=errors,
        )

    async def async_step_reconfigure_timing_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of central heater timing."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_TIMING_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_auto_onoff_setup()
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("timing setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure timing setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_timing_setup",
            data_schema=_schema_with_current(STEP_TIMING_SCHEMA, self._config_data),
            errors=errors,
        )

    async def async_step_reconfigure_auto_onoff_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of auto on/off settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_AUTO_ONOFF_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_adaptive_setup()
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("auto on/off setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure auto on/off setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_auto_onoff_setup",
            data_schema=_schema_with_current(STEP_AUTO_ONOFF_SCHEMA, self._config_data),
            errors=errors,
        )

    async def async_step_reconfigure_adaptive_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of adaptive control settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_ADAPTIVE_SCHEMA)
                self._config_data.update(validated_input)
                return await self.async_step_reconfigure_presets_setup()
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("adaptive setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure adaptive setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_adaptive_setup",
            data_schema=_schema_with_current(STEP_ADAPTIVE_SCHEMA, self._config_data),
            errors=errors,
        )

    async def async_step_reconfigure_presets_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of temperature presets."""
        config_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                validated_input = _validate_input(user_input, STEP_PRESETS_SCHEMA)
                self._config_data.update(validated_input)
                self._config_data = _normalize_lists(self._config_data)

                # Complete reconfiguration; the entry's update listener reloads it.
                if config_entry.options:
                    # Fold legacy options into data so the new values take effect.
                    return self.async_update_and_abort(
                        config_entry,
                        data={**config_entry.data, **self._config_data},
                        options={},
                    )
                return self.async_update_and_abort(
                    config_entry,
                    data_updates=self._config_data,
                )
            except vol.MultipleInvalid as err:
                errors = self._invalid_errors("presets setup", err)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in reconfigure presets setup")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_presets_setup",
            data_schema=_schema_with_current(STEP_PRESETS_SCHEMA, self._config_data),
            errors=errors,
        )
