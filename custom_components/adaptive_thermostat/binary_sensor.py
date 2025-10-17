"""Binary sensor platform for Adaptive Thermostat."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import Event, async_track_state_change_event  # type: ignore

from .const import DOMAIN, SIGNAL_THERMOSTAT_READY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Adaptive Thermostat binary sensors."""
    async_add_entities([AdaptiveThermostatHeaterBinarySensor(hass, entry)])


class AdaptiveThermostatHeaterBinarySensor(BinarySensorEntity):
    """Binary sensor reflecting whether the zone heater is currently on."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        base_name = entry.title or entry.data.get(CONF_NAME) or "Adaptive Thermostat"
        self._attr_unique_id = f"{entry.entry_id}_heater_active"
        self._attr_name = f"{base_name} Heater Active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=base_name,
        )
        self._attr_is_on = False
        self._attr_available = False
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._climate_entity_id: Optional[str] = None
        self._unsub_dispatcher: Optional[Callable[[], None]] = None
        self._unsub_state: Optional[Callable[[], None]] = None

    async def async_added_to_hass(self) -> None:
        """Run when the binary sensor is added to Home Assistant."""
        await super().async_added_to_hass()
        signal = f"{SIGNAL_THERMOSTAT_READY}_{self._entry.entry_id}"
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_thermostat_ready,
        )
        self._try_bind_existing_thermostat()

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup when the binary sensor is removed."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    def _try_bind_existing_thermostat(self) -> None:
        """Attempt to bind immediately if the climate entity is already registered."""
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
        """Link the binary sensor to its thermostat climate entity."""
        if entity_id == self._climate_entity_id:
            return

        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None

        self._climate_entity_id = entity_id

        if not entity_id:
            _LOGGER.debug(
                "[%s] No thermostat entity available, marking heater sensor unavailable",
                self._entry.entry_id,
            )
            self._attr_available = False
            self._attr_is_on = False
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        _LOGGER.debug(
            "[%s] Binding heater sensor to climate entity %s",
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
        """Update the binary sensor from the climate entity state."""
        if state is None:
            self._attr_available = False
            self._attr_is_on = False
            self._attr_extra_state_attributes = {}
            return

        self._attr_available = True
        attrs = state.attributes or {}
        hvac_action = attrs.get("hvac_action")

        zone_heater_on = attrs.get("zone_heater_on")
        if zone_heater_on is None:
            zone_heater_on = hvac_action in ("heating", "heat")

        is_on = bool(zone_heater_on)
        self._attr_is_on = is_on
        self._attr_extra_state_attributes = {
            "linked_entity_id": getattr(state, "entity_id", None),
            "hvac_mode": state.state,
            "hvac_action": hvac_action,
            "zone_heater_on": is_on,
        }
