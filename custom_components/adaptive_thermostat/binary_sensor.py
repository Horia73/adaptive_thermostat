"""Binary sensor platform for Adaptive Thermostat."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_ZONE_UPDATE
from .runtime import ZoneRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Adaptive Thermostat binary sensors."""
    async_add_entities([AdaptiveThermostatHeaterBinarySensor(entry.runtime_data)])


class AdaptiveThermostatHeaterBinarySensor(BinarySensorEntity):
    """On while the zone heats (valves confirmed open for a burst)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, runtime: ZoneRuntime) -> None:
        self.runtime = runtime
        entry = runtime.entry
        base_name = entry.title or entry.data.get(CONF_NAME) or "Adaptive Thermostat"
        self._attr_unique_id = f"{entry.entry_id}_heater_active"
        self._attr_name = f"{base_name} Heater Active"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=base_name)
        self._attr_is_on = False
        self._attr_extra_state_attributes = {}
        self._was_available: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_ZONE_UPDATE}_{self.runtime.entry_id}", self._updated
            )
        )
        self._updated()

    @callback
    def _updated(self) -> None:
        rt = self.runtime
        is_on = rt.zone_heater_on
        attrs = {
            "linked_entity_id": rt.entity_id,
            "hvac_mode": rt.hvac_mode.value,
            "hvac_action": rt.hvac_action.value,
            "zone_heater_on": is_on,
        }
        if (
            is_on == self._attr_is_on
            and attrs == self._attr_extra_state_attributes
            and self.available == self._was_available
        ):
            return
        self._was_available = self.available
        self._attr_is_on = is_on
        self._attr_extra_state_attributes = attrs
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.runtime.core is not None
