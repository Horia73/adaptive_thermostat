"""Sensor platform for Adaptive Thermostat."""

from __future__ import annotations

import logging
from typing import Any, Optional, Callable

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event, Event # type: ignore
from homeassistant.const import CONF_NAME

from .const import DOMAIN, SIGNAL_THERMOSTAT_READY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Adaptive Thermostat sensors."""
    async_add_entities([AdaptiveThermostatSlopeSensor(hass, entry)])


class AdaptiveThermostatSlopeSensor(SensorEntity):
    """Sensor exposing the instantaneous temperature slope."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:chart-line"
    _attr_native_unit_of_measurement = "°C/min"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the slope sensor."""
        self._hass = hass
        self._entry = entry
        self._climate_entity_id: Optional[str] = None
        self._unsub_dispatcher: Optional[Callable[[], None]] = None
        self._unsub_state: Optional[Callable[[], None]] = None
        self._attr_unique_id = f"{entry.entry_id}_temperature_slope"
        base_name = entry.title or entry.data.get(CONF_NAME) or "Adaptive Thermostat"
        self._attr_name = f"{base_name} Temperature Slope"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=base_name,
        )
        self._attr_native_value: Optional[float] = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Run when the sensor is added to Home Assistant."""
        await super().async_added_to_hass()
        signal = f"{SIGNAL_THERMOSTAT_READY}_{self._entry.entry_id}"
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass, signal, self._handle_thermostat_ready
        )
        self._try_bind_existing_thermostat()

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup when the sensor is removed."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    def _try_bind_existing_thermostat(self) -> None:
        """Attempt to bind immediately if climate entity already registered."""
        domain_data = self.hass.data.get(DOMAIN, {})
        entry_map = domain_data.get("entry_to_entity_id") or {}
        entity_id = entry_map.get(self._entry.entry_id)
        if entity_id:
            self._set_climate_entity(entity_id)

    @callback
    def _handle_thermostat_ready(self, entity_id: Optional[str]) -> None:
        """Handle notification that the thermostat entity is ready or removed."""
        self._set_climate_entity(entity_id)

    def _set_climate_entity(self, entity_id: Optional[str]) -> None:
        """Link the sensor to a thermostat climate entity."""
        if entity_id == self._climate_entity_id:
            return

        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None

        self._climate_entity_id = entity_id

        if not entity_id:
            _LOGGER.debug("[%s] No thermostat entity available, marking slope sensor unavailable", self._entry.entry_id)
            self._attr_available = False
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        _LOGGER.debug(
            "[%s] Binding slope sensor to climate entity %s",
            self._entry.entry_id,
            entity_id,
        )
        self._attr_available = True
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [entity_id],
            self._handle_climate_state_event,
        )
        state = self.hass.states.get(entity_id)
        self._update_from_state(state)
        self.async_write_ha_state()

    @callback
    def _handle_climate_state_event(self, event: Event) -> None:
        """Handle a state change from the linked climate entity."""
        new_state = event.data.get("new_state")
        self._update_from_state(new_state)
        self.async_write_ha_state()

    def _update_from_state(self, state: Optional[Any]) -> None:
        """Update the sensor from a Home Assistant state object."""
        if state is None:
            self._attr_available = False
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_available = True

        attrs = state.attributes or {}
        slope_per_min = attrs.get("raw_temperature_slope_per_min")
        if slope_per_min is None:
            slope_per_min = attrs.get("temperature_slope_per_min")

        native_value: Optional[float]
        if slope_per_min is None:
            native_value = None
        else:
            native_value = round(slope_per_min, 3)

        self._attr_native_value = native_value
        self._attr_extra_state_attributes = {
            "slope_per_min": slope_per_min,
            "linked_entity_id": state.entity_id,
        }
