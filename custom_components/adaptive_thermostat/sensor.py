"""Sensor platform for Adaptive Thermostat (diagnostics of the zone runtime)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    FAULT_CENTRAL_UNAVAILABLE,
    FAULT_SENSOR_STALE,
    FAULT_SENSOR_UNAVAILABLE,
    FAULT_VALVE_MISMATCH,
    FAULT_VALVE_UNAVAILABLE,
    SIGNAL_ZONE_UPDATE,
)
from .runtime import ZoneRuntime

FAULT_OPTIONS = [
    "none",
    FAULT_SENSOR_STALE,
    FAULT_SENSOR_UNAVAILABLE,
    FAULT_VALVE_MISMATCH,
    FAULT_VALVE_UNAVAILABLE,
    FAULT_CENTRAL_UNAVAILABLE,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Adaptive Thermostat sensors."""
    runtime: ZoneRuntime = entry.runtime_data
    async_add_entities(
        [
            AdaptiveThermostatSlopeSensor(runtime),
            AdaptiveThermostatHourlySlopeSensor(runtime),
            AdaptiveThermostatCycleOnSensor(runtime),
            AdaptiveThermostatCycleTailSensor(runtime),
            AdaptiveThermostatCycleTargetSensor(runtime),
            AdaptiveThermostatConfidenceSensor(runtime),
            AdaptiveThermostatPredictedPeakSensor(runtime),
            AdaptiveThermostatLastFaultSensor(runtime),
        ]
    )


class _ZoneSensor(SensorEntity):
    """Base class: reads a value from the zone runtime after every control step."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    # Name pattern of the sensors that existed before 2.0 ("<zone> <suffix>"),
    # kept so their entity IDs and names do not change.
    _legacy_name = True
    _key = ""

    def __init__(
        self,
        runtime: ZoneRuntime,
        *,
        unique_suffix: str,
        name_suffix: str,
        icon: str | None = None,
        unit: str | None = None,
        device_class: SensorDeviceClass | None = None,
    ) -> None:
        self.runtime = runtime
        entry = runtime.entry
        base_name = entry.title or entry.data.get(CONF_NAME) or "Adaptive Thermostat"
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = f"{base_name} {name_suffix}" if self._legacy_name else name_suffix
        if icon:
            self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=base_name)
        self._last: tuple[Any, ...] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_ZONE_UPDATE}_{self.runtime.entry_id}", self._updated
            )
        )
        self._refresh()

    @callback
    def _updated(self) -> None:
        if self._refresh():
            self.async_write_ha_state()

    def _value(self, diag: dict[str, Any]) -> Any:
        return diag.get(self._key)

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        return {"linked_entity_id": self.runtime.entity_id}

    def _refresh(self) -> bool:
        diag = self.runtime.diagnostics()
        value = self._value(diag)
        attrs = self._attributes(diag)
        snapshot = (self.available, value, tuple(sorted((k, repr(v)) for k, v in attrs.items())))
        if snapshot == self._last:
            return False
        self._last = snapshot
        self._attr_native_value = value
        self._attr_extra_state_attributes = attrs
        return True

    @property
    def available(self) -> bool:
        return self.runtime.core is not None


class AdaptiveThermostatSlopeSensor(_ZoneSensor):
    """Temperature trend (least-squares slope over the last 30 min of reports)."""

    _key = "temperature_slope_instant_per_hour"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="temperature_slope",
            name_suffix="Temperature Slope",
            icon="mdi:chart-line",
            unit="°C/h",
        )

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        attrs["slope_per_hour"] = diag.get("temperature_slope_instant_per_hour")
        attrs["hourly_slope_per_hour"] = diag.get("temperature_slope_per_hour")
        return attrs


class AdaptiveThermostatHourlySlopeSensor(_ZoneSensor):
    """Temperature trend over the last hour."""

    _key = "temperature_slope_per_hour"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="temperature_slope_hourly",
            name_suffix="Hourly Temperature Slope",
            icon="mdi:chart-timeline-variant",
            unit="°C/h",
        )

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        attrs["hourly_slope_per_hour"] = diag.get("temperature_slope_per_hour")
        attrs["instant_slope_per_hour"] = diag.get("temperature_slope_instant_per_hour")
        return attrs


class _CycleSensor(_ZoneSensor):
    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        for key in ("cycle_on_duration_s", "cycle_tail_duration_s", "cycle_time_to_target_s"):
            if key != self._key:
                attrs[key] = diag.get(key)
        return attrs


class AdaptiveThermostatCycleOnSensor(_CycleSensor):
    """Planned heat time of the current/next burst."""

    _key = "cycle_on_duration_s"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="cycle_on_seconds",
            name_suffix="Cycle On Duration",
            icon="mdi:timer-play",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )


class AdaptiveThermostatCycleTailSensor(_CycleSensor):
    """Predicted time from end of heating to the residual peak."""

    _key = "cycle_tail_duration_s"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="cycle_tail_seconds",
            name_suffix="Cycle Tail Duration",
            icon="mdi:timer-stop",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )


class AdaptiveThermostatCycleTargetSensor(_CycleSensor):
    """Predicted time until the room peaks."""

    _key = "cycle_time_to_target_s"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="cycle_time_to_target",
            name_suffix="Cycle Time To Target",
            icon="mdi:timer-sand",
            unit="s",
            device_class=SensorDeviceClass.DURATION,
        )


class AdaptiveThermostatConfidenceSensor(_ZoneSensor):
    """How much the learned model is trusted (model control above ~60 %)."""

    _legacy_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="model_confidence",
            name_suffix="Model confidence",
            icon="mdi:brain",
            unit=PERCENTAGE,
        )

    def _value(self, diag: dict[str, Any]) -> Any:
        conf = diag.get("model_confidence")
        return None if conf is None else round(100 * conf)

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        attrs["control_mode"] = diag.get("control_mode")
        attrs["learning"] = diag.get("learning")
        attrs.update(diag.get("thermal_params") or {})
        return attrs


class AdaptiveThermostatPredictedPeakSensor(_ZoneSensor):
    """Peak temperature the model predicts for the current plan."""

    _legacy_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _key = "predicted_peak"

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="predicted_peak",
            name_suffix="Predicted peak",
            icon="mdi:thermometer-chevron-up",
            unit=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
        )

    def _value(self, diag: dict[str, Any]) -> Any:
        peak = diag.get("predicted_peak")
        return None if peak is None else round(peak, 1)

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        attrs["control_reason"] = diag.get("control_reason")
        return attrs


class AdaptiveThermostatLastFaultSensor(_ZoneSensor):
    """Most recent fault (persists after the fault cleared)."""

    _legacy_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = None
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = FAULT_OPTIONS

    def __init__(self, runtime: ZoneRuntime) -> None:
        super().__init__(
            runtime,
            unique_suffix="last_fault",
            name_suffix="Last fault",
            icon="mdi:alert-circle-outline",
        )

    def _value(self, diag: dict[str, Any]) -> Any:
        return diag.get("last_fault") or "none"

    def _attributes(self, diag: dict[str, Any]) -> dict[str, Any]:
        attrs = super()._attributes(diag)
        attrs["last_fault_at"] = diag.get("last_fault_at")
        attrs["active_fault"] = self.runtime.fault
        attrs["fault_message"] = self.runtime.fault_message
        return attrs
