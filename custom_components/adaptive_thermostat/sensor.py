"""Sensor platform for Adaptive Thermostat."""

from __future__ import annotations

import logging
from typing import Any, Optional, Callable

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event, Event  # type: ignore
from homeassistant.const import CONF_NAME

from .const import DOMAIN, SIGNAL_THERMOSTAT_READY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Adaptive Thermostat sensors."""
    sensors = [
        AdaptiveThermostatSlopeSensor(hass, entry),
        AdaptiveThermostatHourlySlopeSensor(hass, entry),
        AdaptiveThermostatCycleOnSensor(hass, entry),
        AdaptiveThermostatCycleTailSensor(hass, entry),
        AdaptiveThermostatCycleTargetSensor(hass, entry),
    ]
    async_add_entities(sensors)


class _AdaptiveThermostatLinkedSensor(SensorEntity):
    """Base class for sensors linked to the adaptive thermostat climate entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        unique_suffix: str,
        name_suffix: str,
        icon: str,
        unit: str,
        device_class: Optional[SensorDeviceClass] = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._climate_entity_id: Optional[str] = None
        self._unsub_dispatcher: Optional[Callable[[], None]] = None
        self._unsub_state: Optional[Callable[[], None]] = None
        base_name = entry.title or entry.data.get(CONF_NAME) or "Adaptive Thermostat"
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = f"{base_name} {name_suffix}"
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=base_name,
        )
        self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
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
            _LOGGER.debug(
                "[%s] No thermostat entity available, marking %s sensor unavailable",
                self._entry.entry_id,
                self._attr_name,
            )
            self._attr_available = False
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        _LOGGER.debug(
            "[%s] Binding %s sensor to climate entity %s",
            self._entry.entry_id,
            self._attr_name,
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

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        """Extract native value from thermostat attributes."""
        raise NotImplementedError

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        """Return additional state attributes for the sensor."""
        return {
            "linked_entity_id": getattr(state, "entity_id", None),
        }

    def _update_from_state(self, state: Optional[Any]) -> None:
        """Update the sensor from a Home Assistant state object."""
        if state is None:
            self._attr_available = False
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        self._attr_available = True
        attrs = state.attributes or {}
        raw_value = self._extract_value(attrs)
        self._attr_native_value = round(raw_value, 3) if raw_value is not None else None
        self._attr_extra_state_attributes = self._build_extra_attrs(attrs, raw_value, state)


class AdaptiveThermostatSlopeSensor(_AdaptiveThermostatLinkedSensor):
    """Sensor exposing the instantaneous temperature slope."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            unique_suffix="temperature_slope",
            name_suffix="Temperature Slope",
            icon="mdi:chart-line",
            unit="°C/h",
        )

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        slope_per_hour = attrs.get("temperature_slope_instant_per_hour")

        if slope_per_hour is None:
            slope_per_hour = attrs.get("temperature_slope_per_hour")

        if slope_per_hour is None:
            display_per_min = attrs.get("temperature_slope_per_min")
            if display_per_min is not None:
                slope_per_hour = display_per_min * 60.0

        return slope_per_hour

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        extra = super()._build_extra_attrs(attrs, raw_value, state)
        extra.update(
            {
                "slope_per_hour": raw_value,
                "hourly_slope_per_hour": attrs.get("temperature_slope_per_hour"),
            }
        )
        return extra


class AdaptiveThermostatHourlySlopeSensor(_AdaptiveThermostatLinkedSensor):
    """Sensor exposing the long-term hourly temperature slope."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            unique_suffix="temperature_slope_hourly",
            name_suffix="Hourly Temperature Slope",
            icon="mdi:chart-timeline-variant",
            unit="°C/h",
        )

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        hourly_slope = attrs.get("temperature_slope_per_hour")

        if hourly_slope is None:
            slope_per_min = attrs.get("temperature_slope_per_min")
            if slope_per_min is not None:
                hourly_slope = slope_per_min * 60.0

        if hourly_slope is None:
            hourly_slope = attrs.get("temperature_slope_instant_per_hour")

        return hourly_slope

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        extra = super()._build_extra_attrs(attrs, raw_value, state)
        extra.update(
            {
                "hourly_slope_per_hour": raw_value,
                "instant_slope_per_hour": attrs.get("temperature_slope_instant_per_hour"),
            }
        )
        return extra


class AdaptiveThermostatCycleOnSensor(_AdaptiveThermostatLinkedSensor):
    """Sensor exposing the predicted ON duration of the active heating burst."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            unique_suffix="cycle_on_seconds",
            name_suffix="Cycle On Duration",
            icon="mdi:timer-play",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        return attrs.get("cycle_on_duration_s")

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        extra = super()._build_extra_attrs(attrs, raw_value, state)
        extra.update(
            {
                "cycle_tail_duration_s": attrs.get("cycle_tail_duration_s"),
                "cycle_time_to_target_s": attrs.get("cycle_time_to_target_s"),
            }
        )
        return extra


class AdaptiveThermostatCycleTailSensor(_AdaptiveThermostatLinkedSensor):
    """Sensor exposing the predicted residual heating tail length."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            unique_suffix="cycle_tail_seconds",
            name_suffix="Cycle Tail Duration",
            icon="mdi:timer-stop",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        return attrs.get("cycle_tail_duration_s")

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        extra = super()._build_extra_attrs(attrs, raw_value, state)
        extra.update(
            {
                "cycle_on_duration_s": attrs.get("cycle_on_duration_s"),
                "cycle_time_to_target_s": attrs.get("cycle_time_to_target_s"),
            }
        )
        return extra


class AdaptiveThermostatCycleTargetSensor(_AdaptiveThermostatLinkedSensor):
    """Sensor exposing the predicted time to reach target temperature for the burst."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            unique_suffix="cycle_time_to_target",
            name_suffix="Cycle Time To Target",
            icon="mdi:timer-sand",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )

    def _extract_value(self, attrs: dict[str, Any]) -> Optional[float]:
        return attrs.get("cycle_time_to_target_s")

    def _build_extra_attrs(
        self,
        attrs: dict[str, Any],
        raw_value: Optional[float],
        state: Any,
    ) -> dict[str, Any]:
        extra = super()._build_extra_attrs(attrs, raw_value, state)
        extra.update(
            {
                "cycle_on_duration_s": attrs.get("cycle_on_duration_s"),
                "cycle_tail_duration_s": attrs.get("cycle_tail_duration_s"),
            }
        )
        return extra
