"""Climate platform for the Adaptive Thermostat integration."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.components.climate import (  # type: ignore
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (  # type: ignore
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.core import HomeAssistant, callback  # type: ignore
from homeassistant.helpers.device_registry import DeviceInfo  # type: ignore
from homeassistant.helpers.dispatcher import async_dispatcher_send  # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback  # type: ignore
from homeassistant.helpers.event import (  # type: ignore
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
    Event,
)
from homeassistant.helpers.storage import Store  # type: ignore
from homeassistant.util import dt as dt_util  # type: ignore

from . import DOMAIN
from .const import (
    CONF_AWAY_PRESET,
    CONF_AUTO_OFF_TEMP,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_CENTRAL_HEATER,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_HEATER,
    CONF_HOME_PRESET,
    CONF_HUMIDITY_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_SLEEP_PRESET,
    CONF_TEMP_SENSOR,
    CONF_WINDOW_DETECTION_ENABLED,
    CONF_WINDOW_SLOPE_THRESHOLD,
    DEFAULT_AWAY_PRESET,
    DEFAULT_AUTO_OFF_TEMP,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_HOME_PRESET,
    DEFAULT_NAME,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_WINDOW_DETECTION_ENABLED,
    DEFAULT_WINDOW_SLOPE_THRESHOLD,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    SIGNAL_THERMOSTAT_READY,
    STORAGE_STATE_KEY,
    STORAGE_VERSION,
)
from .thermal_controller import ThermalController

_LOGGER = logging.getLogger(__name__)

DEFAULT_DEADBAND = 0.1  # °C band for adaptive controller
STATE_SAVE_DELAY_SECONDS = 2.0
CONTROL_TICK_SECONDS = 30
SLOPE_CHANGE_EPSILON = 0.001  # °C change required to treat as a new slope sample
HOURLY_SLOPE_WINDOW_SECONDS = 3600.0
HOURLY_HISTORY_BUFFER_SECONDS = 4200.0
WINDOW_RECOVERY_BUFFER_SECONDS = 600.0  # 10 min before/after window events
THERMAL_SAMPLE_HISTORY_SECONDS = 6 * 3600.0  # retain up to 6 hours of samples for model updates
WINDOW_CONFIRMATION_SECONDS = 120.0  # seconds window slope must persist before auto-confirm
WINDOW_CONFIRMATION_DROP = 0.15  # °C additional drop required to confirm immediately
WINDOW_CANDIDATE_RESET_SECONDS = 240.0  # seconds before discarding an unconfirmed candidate
WINDOW_FALSE_POSITIVE_TOLERANCE = 0.1  # °C slack to auto-clear noisy detections
WINDOW_AUTO_CLEAR_SECONDS = 900.0  # seconds before clearing stale window alerts
WINDOW_POST_RECOVERY_DAMPEN_CYCLES = 1  # heating cycles to soften after window close
WINDOW_POST_RECOVERY_DAMPEN_SCALE = 0.6  # fractional runtime for the first post-window cycle


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp value within bounds."""
    return max(low, min(high, value))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Adaptive Thermostat climate platform."""
    thermostat = AdaptiveThermostat(hass, entry)
    async_add_entities([thermostat], True)


class AdaptiveThermostat(ClimateEntity):
    """A simple hysteresis thermostat with window detection and auto on/off."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the thermostat."""
        self._hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = entry.entry_id

        config = {**entry.data, **entry.options}

        # Helper to normalize entity IDs
        def get_entity_id(key: str) -> Optional[str]:
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value
            return None

        # Required configuration
        self._attr_name = config.get(CONF_NAME, DEFAULT_NAME)
        heater_config = config.get(CONF_HEATER)
        if isinstance(heater_config, list):
            self._heater_entity_ids = [entity for entity in heater_config if isinstance(entity, str)]
        elif isinstance(heater_config, str):
            self._heater_entity_ids = [heater_config]
        elif heater_config:
            self._heater_entity_ids = [str(heater_config)]
        else:
            self._heater_entity_ids = []
        self._heater_entity_id = self._heater_entity_ids[0] if self._heater_entity_ids else None
        self._temp_sensor_entity_id = get_entity_id(CONF_TEMP_SENSOR)

        # Optional sensors
        self._humidity_sensor_entity_id = get_entity_id(CONF_HUMIDITY_SENSOR)
        self._door_window_sensor_entity_id = get_entity_id(CONF_DOOR_WINDOW_SENSOR)
        self._motion_sensor_entity_id = get_entity_id(CONF_MOTION_SENSOR)
        self._outdoor_sensor_entity_id = get_entity_id(CONF_OUTDOOR_SENSOR)
        self._backup_outdoor_sensor_entity_id = get_entity_id(CONF_BACKUP_OUTDOOR_SENSOR)
        self._central_heater_entity_id = get_entity_id(CONF_CENTRAL_HEATER)

        # Timing configuration
        self._min_on_time = 0.0
        self._min_off_time = 0.0
        self._central_heater_turn_on_delay = float(
            config.get(CONF_CENTRAL_HEATER_TURN_ON_DELAY, 0)
        )
        self._central_heater_turn_off_delay = float(
            config.get(CONF_CENTRAL_HEATER_TURN_OFF_DELAY, 0)
        )

        # Window detection
        self._window_detection_enabled = bool(
            config.get(CONF_WINDOW_DETECTION_ENABLED, DEFAULT_WINDOW_DETECTION_ENABLED)
        )
        self._window_slope_threshold = max(
            0.5, float(config.get(CONF_WINDOW_SLOPE_THRESHOLD, DEFAULT_WINDOW_SLOPE_THRESHOLD))
        )

        # Auto on/off
        self._auto_on_off_enabled = bool(config.get(CONF_AUTO_ON_OFF_ENABLED, False))
        self._auto_on_temp = float(config.get(CONF_AUTO_ON_TEMP, DEFAULT_AUTO_ON_TEMP))
        self._auto_off_temp = float(config.get(CONF_AUTO_OFF_TEMP, DEFAULT_AUTO_OFF_TEMP))

        # Presets
        self._presets = {
            "sleep": _clamp(float(config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP),
            "home": _clamp(float(config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP),
            "away": _clamp(float(config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP),
        }

        # Climate state
        self._current_temperature: Optional[float] = None
        self._filtered_temperature: Optional[float] = None
        self._current_humidity: Optional[float] = None
        self._target_temperature: float = self._presets["home"]
        self._current_preset: str | None = "home"
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._zone_heater_on: bool = False
        self._manual_override: bool = False

        # Advanced thermal controller
        self._thermal_controller = ThermalController(
            target=self._target_temperature,
            deadband=DEFAULT_DEADBAND,
            window_s=600,
            min_on_s=60,
            min_off_s=120,
        )
        self._thermal_samples: deque[Tuple[float, float, bool]] = deque()
        self._planned_heater_off_ts: Optional[float] = None
        self._planned_on_duration: Optional[float] = None
        self._planned_off_unsub: Optional[Callable[[], None]] = None
        self._window_data_reenable_at: Optional[float] = None
        self._window_heat_reenable_at: Optional[float] = None
        self._window_open_since_ts: Optional[float] = None
        self._active_cycle: Optional[Dict[str, Any]] = None
        self._pending_cycle_eval: Optional[Dict[str, Any]] = None
        self._last_cycle_diagnostics: Optional[Dict[str, float]] = None

        # Slope tracking
        self._last_measurement_ts: Optional[float] = None
        self._last_measurement_temp: Optional[float] = None
        self._prev_measurement_temp: Optional[float] = None
        self._raw_temperature_slope: float = 0.0
        self._display_temperature_slope: float = 0.0
        self._hourly_temperature_slope: float = 0.0
        self._temperature_history: deque[Tuple[float, float]] = deque()

        # Window detection
        self._open_window_detected: bool = False
        self._last_window_event_ts: Optional[float] = None
        self._window_alert: Optional[str] = None
        self._open_window_baseline_temp: Optional[float] = None
        self._window_candidate: Optional[Dict[str, Any]] = None
        self._post_window_dampen_cycles: int = 0
        self._window_last_closed_ts: Optional[float] = None

        # Outdoor tracking
        self._current_outdoor_temp: Optional[float] = None
        self._last_outdoor_temp: Optional[float] = None

        # Control bookkeeping
        self._last_update_ts: Optional[float] = None
        self._last_command_timestamp: Optional[float] = None
        self._central_heater_task: Optional[asyncio.Task] = None
        self._delayed_valve_off_task: Optional[asyncio.Task] = None
        self._control_tick_unsub = None
        self._remove_listener = None

        # Persistent runtime state
        self._state_store: Store | None = None
        self._state_cache_ref: Dict[str, Any] | None = None
        self._state_dirty: bool = False
        self._state_save_unsub = None

        # Home Assistant metadata
        self._attr_temperature_unit = hass.config.units.temperature_unit or UnitOfTemperature.CELSIUS
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE

        self._attr_extra_state_attributes: Dict[str, Any] = {
            "heater_entity_id": self._heater_entity_id,
            "heater_entity_ids": self._heater_entity_ids,
            "central_heater_entity_id": self._central_heater_entity_id,
            "temp_sensor_entity_id": self._temp_sensor_entity_id,
            "humidity_sensor": self._humidity_sensor_entity_id,
            "motion_sensor": self._motion_sensor_entity_id,
            "door_window_sensor": self._door_window_sensor_entity_id,
            "outdoor_sensor": self._outdoor_sensor_entity_id,
            "backup_outdoor_sensor": self._backup_outdoor_sensor_entity_id,
            "auto_on_off_enabled": self._auto_on_off_enabled,
            "manual_override": False,
            "window_detection_enabled": self._window_detection_enabled,
            "window_open_detected": False,
            "window_slope_threshold": self._window_slope_threshold,
            "window_alert": None,
            "zone_heater_on": False,
            "heat_on_delta": self._thermal_controller.deadband,
            "heat_off_delta": self._thermal_controller.deadband,
            "window_candidate_active": False,
            "post_window_dampen_cycles": 0,
            "window_recovery_until": None,
            "window_data_block_until": None,
            "planned_zone_off_time": None,
            "planned_zone_on_duration": None,
            "thermal_samples_cached": 0,
            "thermal_params": self._current_thermal_param_payload(),
            "cycle_on_duration_s": None,
            "cycle_tail_duration_s": None,
            "cycle_time_to_target_s": None,
        }

    @property
    def name(self) -> str:
        """Return the display name of the thermostat."""
        return self._attr_name

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the entity."""
        return self._attr_unique_id

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current action."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._zone_heater_on:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> Optional[float]:
        """Return current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self) -> Optional[float]:
        """Return target temperature."""
        return self._target_temperature

    @property
    def current_humidity(self) -> Optional[float]:
        """Return current humidity."""
        return self._current_humidity

    @property
    def preset_modes(self) -> Optional[List[str]]:
        """Return available preset modes."""
        return list(self._presets.keys())

    @property
    def preset_mode(self) -> Optional[str]:
        """Return current preset mode."""
        return self._current_preset

    @property
    def min_temp(self) -> float:
        """Return minimum allowable target temperature."""
        return MIN_TARGET_TEMP

    @property
    def max_temp(self) -> float:
        """Return maximum allowable target temperature."""
        return MAX_TARGET_TEMP

    @property
    def target_temperature_step(self) -> float:
        """Return target temperature step."""
        return 0.1

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return extra state attributes."""
        return self._attr_extra_state_attributes

    async def async_added_to_hass(self) -> None:
        """Handle being added to Home Assistant."""
        await super().async_added_to_hass()
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        entry_map = domain_data.setdefault("entry_to_entity_id", {})
        entities = domain_data.setdefault("entities", {})

        if "state_store" not in domain_data:
            domain_data["state_store"] = Store(self.hass, STORAGE_VERSION, STORAGE_STATE_KEY)
        if "state_cache" not in domain_data:
            domain_data["state_cache"] = {}

        self._state_store = domain_data["state_store"]
        self._state_cache_ref = domain_data["state_cache"]

        if self.entity_id:
            entities[self.entity_id] = self

        await self._async_load_runtime_state()

        # Register listeners
        entities_to_track = [
            entity_id
            for entity_id in [
                self._temp_sensor_entity_id,
                self._humidity_sensor_entity_id,
                self._outdoor_sensor_entity_id,
                self._backup_outdoor_sensor_entity_id,
                self._door_window_sensor_entity_id,
                self._motion_sensor_entity_id,
            ]
            if entity_id
        ]
        entities_to_track.extend(self._heater_entity_ids)

        if entities_to_track:
            self._remove_listener = async_track_state_change_event(
                self.hass,
                entities_to_track,
                self._async_state_changed,
            )

        self._control_tick_unsub = async_track_time_interval(
            self.hass, self._async_control_tick, timedelta(seconds=CONTROL_TICK_SECONDS)
        )

        entry_map[self._entry_id] = self.entity_id
        async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self._entry_id}", self.entity_id)

        await self.async_update()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Handle removal from Home Assistant."""
        if self._control_tick_unsub:
            self._control_tick_unsub()
            self._control_tick_unsub = None

        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None

        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        if self._planned_off_unsub:
            self._planned_off_unsub()
            self._planned_off_unsub = None

        if self._state_save_unsub:
            self._state_save_unsub()
            self._state_save_unsub = None
        if self._state_dirty:
            await self._async_persist_runtime_state()

        domain_data = self.hass.data.get(DOMAIN, {})
        entities = domain_data.get("entities", {})
        if self.entity_id in entities:
            entities.pop(self.entity_id, None)

        entry_map = domain_data.get("entry_to_entity_id", {})
        if self._entry_id in entry_map:
            entry_map.pop(self._entry_id, None)
            async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self._entry_id}", None)

        await super().async_will_remove_from_hass()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        _LOGGER.info("[%s] User set HVAC mode to %s", self._entry_id, hvac_mode)
        if hvac_mode == HVACMode.OFF:
            self._hvac_mode = HVACMode.OFF
            if self._zone_heater_on:
                await self._async_turn_heater_off()
            self._manual_override = self._auto_on_off_enabled
            self._attr_extra_state_attributes["manual_override"] = self._manual_override
        elif hvac_mode == HVACMode.HEAT:
            if self._manual_override:
                _LOGGER.debug("[%s] Manual override cleared", self._entry_id)
            self._manual_override = False
            self._attr_extra_state_attributes["manual_override"] = False
            self._hvac_mode = HVACMode.HEAT
            await self._async_control_heating(dt_util.utcnow().timestamp())
        else:
            _LOGGER.warning("[%s] Unsupported HVAC mode: %s", self._entry_id, hvac_mode)
            return
        self._mark_state_dirty()
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set preset mode."""
        if preset_mode not in self._presets:
            _LOGGER.warning("[%s] Invalid preset mode: %s", self._entry_id, preset_mode)
            return

        self._current_preset = preset_mode
        self._target_temperature = self._presets[preset_mode]
        self._thermal_controller.target = self._target_temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating(dt_util.utcnow().timestamp())
        self._mark_state_dirty()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle target temperature changes."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        target = _clamp(float(temperature), MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        self._target_temperature = target
        self._current_preset = None
        self._thermal_controller.target = self._target_temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating(dt_util.utcnow().timestamp())
        self._mark_state_dirty()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch latest data and control the heater."""
        try:
            now = dt_util.utcnow()
            now_ts = dt_util.as_timestamp(now)
            dt = 0.0 if self._last_update_ts is None else max(0.0, now_ts - self._last_update_ts)

            raw_temp, temp_ts = self._read_temperature(now_ts)
            humidity = self._read_humidity()
            outdoor_temp, backup_outdoor_temp = self._read_outdoor_temperatures()
            door_window_open = self._read_binary_sensor(self._door_window_sensor_entity_id)
            motion_active = self._read_binary_sensor(self._motion_sensor_entity_id)
            heater_states = self._gather_heater_states()
            central_state = self._gather_central_state()

            if raw_temp is not None:
                self._current_temperature = raw_temp
                self._update_temperature_metrics(raw_temp, temp_ts, now_ts)

            if humidity is not None:
                self._current_humidity = humidity

            if outdoor_temp is not None:
                self._current_outdoor_temp = outdoor_temp
            elif backup_outdoor_temp is not None and self._current_outdoor_temp is None:
                self._current_outdoor_temp = backup_outdoor_temp

            if outdoor_temp is not None:
                self._thermal_controller.update_outdoor(outdoor_temp)
            elif backup_outdoor_temp is not None:
                self._thermal_controller.update_outdoor(backup_outdoor_temp)

            await self._async_update_window_detection(now_ts, door_window_open)
            self._update_sensor_attributes(
                now,
                motion_active,
                door_window_open,
                heater_states,
                central_state,
            )

            if self._auto_on_off_enabled:
                if self._manual_override:
                    _LOGGER.debug("[%s] Auto on/off suppressed by manual override", self._entry_id)
                else:
                    await self._async_handle_auto_onoff(outdoor_temp, backup_outdoor_temp)

            if self._hvac_mode == HVACMode.HEAT:
                await self._async_control_heating(now_ts)

            self._last_update_ts = now_ts
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("[%s] Update failed: %s", self._entry_id, err, exc_info=True)

    def _read_temperature(self, fallback_ts: float) -> Tuple[Optional[float], Optional[float]]:
        """Read temperature sensor."""
        if not self._temp_sensor_entity_id:
            return self._current_temperature, fallback_ts

        state = self.hass.states.get(self._temp_sensor_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None, None

        try:
            value = float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("[%s] Invalid temperature from %s: %s", self._entry_id, self._temp_sensor_entity_id, state.state)
            return None, None

        state_dt = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
        measurement_ts = dt_util.as_timestamp(state_dt) if state_dt else fallback_ts
        return value, measurement_ts

    def _read_humidity(self) -> Optional[float]:
        """Read humidity sensor."""
        if not self._humidity_sensor_entity_id:
            return None
        state = self.hass.states.get(self._humidity_sensor_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("[%s] Invalid humidity from %s: %s", self._entry_id, self._humidity_sensor_entity_id, state.state)
            return None

    def _read_binary_sensor(self, entity_id: Optional[str]) -> Optional[bool]:
        """Read binary sensor value as boolean."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        return state.state == STATE_ON

    def _read_outdoor_temperatures(self) -> Tuple[Optional[float], Optional[float]]:
        """Read outdoor sensors."""
        def safe_float(entity_id: Optional[str]) -> Optional[float]:
            if not entity_id:
                return None
            state = self.hass.states.get(entity_id)
            if not state or state.state in ("unknown", "unavailable"):
                return None
            try:
                return float(state.state)
            except (ValueError, TypeError):
                _LOGGER.warning("[%s] Invalid numeric value from %s: %s", self._entry_id, entity_id, state.state)
                return None

        return safe_float(self._outdoor_sensor_entity_id), safe_float(self._backup_outdoor_sensor_entity_id)

    def _update_temperature_metrics(
        self,
        raw_temp: float,
        measurement_ts: Optional[float],
        now_ts: float,
    ) -> None:
        """Update slope calculations."""
        sample_ts = measurement_ts if measurement_ts is not None else now_ts
        prev_ts = self._last_measurement_ts or sample_ts
        dt = max(0.0, sample_ts - prev_ts)
        if dt <= 0.0:
            dt = max(0.0, now_ts - prev_ts)
        if dt <= 0.0:
            dt = 1e-6

        prev_temp = self._last_measurement_temp
        if prev_temp is None:
            slope = 0.0
        else:
            temp_delta = raw_temp - prev_temp
            if abs(temp_delta) >= SLOPE_CHANGE_EPSILON:
                slope = temp_delta / dt
            else:
                slope = self._raw_temperature_slope

        self._prev_measurement_temp = prev_temp
        self._last_measurement_ts = sample_ts
        self._last_measurement_temp = raw_temp
        self._raw_temperature_slope = slope
        self._display_temperature_slope = slope
        self._filtered_temperature = raw_temp
        self._update_hourly_temperature_slope(sample_ts, raw_temp)
        self._record_thermal_sample(sample_ts, raw_temp)

    def _update_hourly_temperature_slope(self, sample_ts: float, temp: float) -> None:
        """Maintain a rolling history to compute long term slope."""
        if self._temperature_history and sample_ts <= self._temperature_history[-1][0]:
            self._temperature_history[-1] = (sample_ts, temp)
        else:
            self._temperature_history.append((sample_ts, temp))

        cutoff = sample_ts - HOURLY_SLOPE_WINDOW_SECONDS
        prune_before = sample_ts - HOURLY_HISTORY_BUFFER_SECONDS

        while self._temperature_history and self._temperature_history[0][0] < prune_before:
            self._temperature_history.popleft()

        if not self._temperature_history:
            self._hourly_temperature_slope = 0.0
            return

        reference_temp = self._temperature_history[0][1]
        reference_ts = self._temperature_history[0][0]
        for ts, temp_value in self._temperature_history:
            if ts >= cutoff:
                reference_temp = temp_value
                reference_ts = ts
                break

        dt_hist = sample_ts - reference_ts
        if dt_hist <= 0:
            self._hourly_temperature_slope = 0.0
            return

        slope_per_hour = (temp - reference_temp) / dt_hist * 3600.0
        self._hourly_temperature_slope = slope_per_hour

    def _record_thermal_sample(self, sample_ts: float, temp: float) -> None:
        """Record a sample for the thermal model, respecting window quiescence."""
        if self._open_window_detected:
            return
        if self._window_data_reenable_at is not None and sample_ts < self._window_data_reenable_at:
            return

        self._thermal_samples.append((sample_ts, temp, self._zone_heater_on))

        while self._thermal_samples and sample_ts - self._thermal_samples[0][0] > THERMAL_SAMPLE_HISTORY_SECONDS:
            self._thermal_samples.popleft()

        self._attr_extra_state_attributes["thermal_samples_cached"] = len(self._thermal_samples)

    def _purge_recent_samples(self, keep_before_ts: float) -> None:
        """Drop recent samples collected during window disturbances."""
        if not self._thermal_samples:
            return

        removed = 0
        while self._thermal_samples and self._thermal_samples[-1][0] >= keep_before_ts:
            self._thermal_samples.pop()
            removed += 1

        if removed:
            _LOGGER.debug("[%s] Purged %d thermal samples after window detection", self._entry_id, removed)

        self._attr_extra_state_attributes["thermal_samples_cached"] = len(self._thermal_samples)

    def _handle_window_state_transition(self, now_ts: float, opened: bool, *, enforce_recovery: bool = True) -> None:
        """Apply bookkeeping when a window disturbance starts or ends."""
        if opened:
            self._window_candidate = None
            self._window_open_since_ts = now_ts
            self._purge_recent_samples(now_ts - WINDOW_RECOVERY_BUFFER_SECONDS)
            self._window_data_reenable_at = None
            self._window_heat_reenable_at = None
            if self._planned_off_unsub:
                self._planned_off_unsub()
                self._planned_off_unsub = None
            self._planned_heater_off_ts = None
            self._planned_on_duration = None
            self._attr_extra_state_attributes["planned_zone_off_time"] = None
            self._attr_extra_state_attributes["planned_zone_on_duration"] = None
            self._attr_extra_state_attributes["window_data_block_until"] = None
            self._attr_extra_state_attributes["window_recovery_until"] = None
            self._post_window_dampen_cycles = 0
            self._attr_extra_state_attributes["post_window_dampen_cycles"] = 0
            self._attr_extra_state_attributes["window_candidate_active"] = False
            self._attr_extra_state_attributes["cycle_on_duration_s"] = None
            self._attr_extra_state_attributes["cycle_tail_duration_s"] = None
            self._attr_extra_state_attributes["cycle_time_to_target_s"] = None
        else:
            self._window_open_since_ts = None
            if enforce_recovery:
                recovery_ts = now_ts + WINDOW_RECOVERY_BUFFER_SECONDS
                self._window_data_reenable_at = recovery_ts
                self._window_heat_reenable_at = recovery_ts
                iso_value = self._iso_or_none(recovery_ts)
                self._attr_extra_state_attributes["window_data_block_until"] = iso_value
                self._attr_extra_state_attributes["window_recovery_until"] = iso_value
                self._post_window_dampen_cycles = WINDOW_POST_RECOVERY_DAMPEN_CYCLES
                self._window_last_closed_ts = now_ts
            else:
                self._window_data_reenable_at = None
                self._window_heat_reenable_at = None
                self._attr_extra_state_attributes["window_data_block_until"] = None
                self._attr_extra_state_attributes["window_recovery_until"] = None
                self._post_window_dampen_cycles = 0
                self._window_last_closed_ts = None
            self._open_window_baseline_temp = None
            self._attr_extra_state_attributes["post_window_dampen_cycles"] = self._post_window_dampen_cycles
            self._attr_extra_state_attributes["window_candidate_active"] = False

    def _current_thermal_param_payload(self) -> Dict[str, float]:
        """Return rounded thermal parameter snapshot for diagnostics."""
        params = self._thermal_controller.get_params()
        return {
            "tau_r": round(params.tau_r, 2),
            "tau_th": round(params.tau_th, 2),
            "K": round(params.K, 3),
            "p": round(params.p, 3),
        }

    def _iso_or_none(self, ts: Optional[float]) -> Optional[str]:
        """Return ISO timestamp or None."""
        if ts is None:
            return None
        return dt_util.utc_from_timestamp(ts).isoformat()

    async def _async_handle_planned_turn_off(self, _now: datetime) -> None:
        """Handle scheduled heater turn-off initiated by the model."""
        self._planned_off_unsub = None
        self._planned_heater_off_ts = None
        if self._zone_heater_on:
            _LOGGER.debug("[%s] Model-triggered OFF reached", self._entry_id)
            await self._async_turn_heater_off()

    async def _async_update_window_detection(self, now_ts: float, door_window_open: Optional[bool]) -> None:
        """Detect rapid cooling or open window events."""
        if not self._window_detection_enabled:
            if self._open_window_detected:
                self._open_window_detected = False
                self._window_alert = None
                self._last_window_event_ts = now_ts
            if self._window_candidate:
                self._window_candidate = None
                self._attr_extra_state_attributes["window_candidate_active"] = False
            return

        current_temp = (
            self._filtered_temperature
            if self._filtered_temperature is not None
            else self._current_temperature
        )
        current_measurement_ts = self._last_measurement_ts

        if door_window_open:
            if not self._open_window_detected:
                self._open_window_detected = True
                self._open_window_baseline_temp = float(current_temp) if current_temp is not None else None
                self._window_alert = "Door/window sensor reported open"
                self._last_window_event_ts = now_ts
                self._handle_window_state_transition(now_ts, True)
                _LOGGER.warning("[%s] Window sensor open - disabling heating", self._entry_id)
                if self._zone_heater_on:
                    await self._async_turn_heater_off()
            self._window_candidate = None
            self._attr_extra_state_attributes["window_candidate_active"] = False
            return

        slope_base = self._display_temperature_slope
        if slope_base is None:
            slope_base = self._raw_temperature_slope
        slope_per_hour = (slope_base or 0.0) * 3600.0
        threshold = self._window_slope_threshold

        candidate = self._window_candidate
        if candidate and current_measurement_ts is not None:
            last_measurement = candidate.get("last_measurement_ts")
            if last_measurement != current_measurement_ts:
                candidate["last_measurement_ts"] = current_measurement_ts
                candidate["sample_count"] = candidate.get("sample_count", 1) + 1
                if current_temp is not None:
                    candidate["last_temp"] = float(current_temp)

        if not self._open_window_detected and slope_per_hour <= -threshold:
            if candidate is None:
                start_temp = None
                if self._prev_measurement_temp is not None:
                    start_temp = float(self._prev_measurement_temp)
                elif current_temp is not None:
                    start_temp = float(current_temp)
                self._window_candidate = {
                    "start_ts": now_ts,
                    "start_temp": start_temp,
                    "last_temp": float(current_temp) if current_temp is not None else None,
                    "start_measurement_ts": current_measurement_ts,
                    "last_measurement_ts": current_measurement_ts,
                    "sample_count": 1,
                }
                candidate = self._window_candidate
                self._attr_extra_state_attributes["window_candidate_active"] = True
                _LOGGER.debug("[%s] Window candidate started (slope=%.2f°C/h)", self._entry_id, slope_per_hour)
            else:
                if current_temp is not None:
                    candidate["last_temp"] = float(current_temp)

            start_temp = candidate.get("start_temp")
            last_temp = candidate.get("last_temp")
            drop = None
            if start_temp is not None and last_temp is not None:
                drop = start_temp - last_temp

            confirm_drop = drop is not None and drop >= WINDOW_CONFIRMATION_DROP
            sample_count = candidate.get("sample_count", 1)
            confirm_duration = (
                sample_count >= 2
                and now_ts - candidate["start_ts"] >= WINDOW_CONFIRMATION_SECONDS
                and slope_per_hour <= -threshold * 0.5
            )

            if confirm_drop or confirm_duration:
                baseline = start_temp if start_temp is not None else last_temp
                self._open_window_detected = True
                self._open_window_baseline_temp = baseline
                self._window_alert = f"Open window detected (drop {abs(slope_per_hour):.2f}°C/h)"
                self._last_window_event_ts = now_ts
                self._window_candidate = None
                self._attr_extra_state_attributes["window_candidate_active"] = False
                self._handle_window_state_transition(now_ts, True)
                _LOGGER.warning(
                    "[%s] Temperature drop detected (%.2f°C/h). Disabling heating.",
                    self._entry_id,
                    slope_per_hour,
                )
                if self._zone_heater_on:
                    await self._async_turn_heater_off()
                return
            if sample_count < 2 and now_ts - candidate["start_ts"] >= WINDOW_CANDIDATE_RESET_SECONDS:
                _LOGGER.debug(
                    "[%s] Window candidate expired without confirmation (elapsed=%.0fs)",
                    self._entry_id,
                    now_ts - candidate["start_ts"],
                )
                self._window_candidate = None
                self._attr_extra_state_attributes["window_candidate_active"] = False
        else:
            if candidate:
                elapsed = now_ts - candidate.get("start_ts", now_ts)
                if slope_per_hour > -threshold * 0.2 or elapsed >= WINDOW_CANDIDATE_RESET_SECONDS:
                    _LOGGER.debug(
                        "[%s] Window candidate cleared (slope=%.2f°C/h elapsed=%.0fs)",
                        self._entry_id,
                        slope_per_hour,
                        elapsed,
                    )
                    self._window_candidate = None
                    self._attr_extra_state_attributes["window_candidate_active"] = False

        if self._open_window_detected:
            baseline = self._open_window_baseline_temp
            false_positive = (
                baseline is not None
                and current_temp is not None
                and baseline - current_temp < WINDOW_FALSE_POSITIVE_TOLERANCE
                and self._last_window_event_ts is not None
                and now_ts - self._last_window_event_ts >= WINDOW_CONFIRMATION_SECONDS
            )
            stale = (
                self._last_window_event_ts is not None
                and now_ts - self._last_window_event_ts >= WINDOW_AUTO_CLEAR_SECONDS
                and slope_per_hour > -threshold
            )
            if slope_per_hour >= -threshold * 0.4 or false_positive or stale:
                self._open_window_detected = False
                self._window_alert = None
                self._last_window_event_ts = now_ts
                enforce_recovery = not false_positive
                self._handle_window_state_transition(now_ts, False, enforce_recovery=enforce_recovery)
                if false_positive:
                    _LOGGER.info("[%s] Window alert cleared (no sustained drop)", self._entry_id)
                elif stale:
                    _LOGGER.info(
                        "[%s] Window alert timed out after %.0fs without further cooling",
                        self._entry_id,
                        WINDOW_AUTO_CLEAR_SECONDS,
                    )
                else:
                    _LOGGER.info("[%s] Window cooling resolved (%.2f°C/h)", self._entry_id, slope_per_hour)

    def _update_sensor_attributes(
        self,
        now: datetime,
        motion_active: Optional[bool],
        door_window_open: Optional[bool],
        heater_states: List[Dict[str, Any]],
        central_state: Optional[Dict[str, Any]],
    ) -> None:
        """Update extra state attributes for diagnostics and the card."""
        last_window_event = (
            dt_util.utc_from_timestamp(self._last_window_event_ts).isoformat()
            if self._last_window_event_ts
            else None
        )

        instant_slope_per_hour = self._display_temperature_slope * 3600.0
        hourly_slope_per_hour = self._hourly_temperature_slope
        raw_slope_per_hour = self._raw_temperature_slope * 3600.0

        effective_temp = self._filtered_temperature or self._current_temperature
        heat_on_threshold = (
            self._target_temperature - self._thermal_controller.deadband
            if self._target_temperature is not None
            else None
        )
        heat_off_threshold = (
            self._target_temperature + self._thermal_controller.deadband
            if self._target_temperature is not None
            else None
        )
        window_recovery_iso = self._iso_or_none(self._window_heat_reenable_at)
        data_block_iso = self._iso_or_none(self._window_data_reenable_at)
        planned_off_iso = self._iso_or_none(self._planned_heater_off_ts)
        thermal_params = self._current_thermal_param_payload()
        cycle_diag = {}
        if self._last_cycle_diagnostics:
            cycle_diag = {
                "last_cycle_ratio": round(self._last_cycle_diagnostics["ratio_actual_to_pred"], 3),
                "last_cycle_peak_predicted": round(self._last_cycle_diagnostics["predicted_peak"], 3),
                "last_cycle_peak_actual": round(self._last_cycle_diagnostics["actual_peak"], 3),
                "last_cycle_tau_on": round(self._last_cycle_diagnostics["tau_on"], 1),
                "last_cycle_overshoot": round(self._last_cycle_diagnostics["overshoot"], 3),
                "last_cycle_undershoot": round(self._last_cycle_diagnostics["undershoot"], 3),
            }

        self._attr_extra_state_attributes.update(
            {
                "current_temperature": self._current_temperature,
                "filtered_temperature": self._filtered_temperature,
                "current_humidity": self._current_humidity,
                "current_outdoor_temp": self._current_outdoor_temp,
                "heater_states": heater_states,
                "central_heater_state": central_state,
                "zone_heater_on": self._zone_heater_on,
                "temperature_slope_instant_per_hour": round(instant_slope_per_hour, 3),
                "temperature_slope_per_hour": round(hourly_slope_per_hour, 3),
                "raw_temperature_slope_per_hour": round(raw_slope_per_hour, 3),
                "window_open_detected": self._open_window_detected,
                "window_alert": self._window_alert,
                "last_window_event": last_window_event,
                "window_candidate_active": bool(self._window_candidate),
                "door_window_open": door_window_open,
                "motion_active": motion_active,
                "manual_override": self._manual_override,
                "heat_on_threshold": heat_on_threshold,
                "heat_off_threshold": heat_off_threshold,
                "heat_on_delta": self._thermal_controller.deadband,
                "heat_off_delta": self._thermal_controller.deadband,
                "effective_control_temperature": effective_temp,
                "last_updated": now.isoformat(),
                "window_recovery_until": window_recovery_iso,
                "window_data_block_until": data_block_iso,
                "planned_zone_off_time": planned_off_iso,
                "planned_zone_on_duration": self._planned_on_duration,
                "thermal_params": thermal_params,
                "post_window_dampen_cycles": self._post_window_dampen_cycles,
            }
        )
        if cycle_diag:
            self._attr_extra_state_attributes.update(cycle_diag)

    def _update_cycle_tracking(self, now_ts: float, effective_temp: float) -> None:
        """Update heating cycle diagnostics and trigger post-cycle learning."""
        if self._active_cycle:
            cycle = self._active_cycle
            cycle["last_temp"] = effective_temp
            prev_peak = cycle.get("peak_temp", effective_temp)
            cycle["peak_temp"] = max(effective_temp, prev_peak)

        if not self._pending_cycle_eval:
            return

        cycle = self._pending_cycle_eval
        prev_peak = cycle.get("peak_temp", effective_temp)
        cycle["peak_temp"] = max(effective_temp, prev_peak)

        off_ts = float(cycle.get("off_ts", now_ts))
        time_since_off = now_ts - off_ts
        target = float(cycle.get("target", self._target_temperature))
        below_target = effective_temp <= target
        slope_cooling = self._raw_temperature_slope <= 0.0

        duration_ref = 0.0
        for key in ("actual_on_duration", "planned_duration"):
            value = cycle.get(key)
            if isinstance(value, (int, float)):
                duration_ref = float(value)
                break
        long_wait = duration_ref > 0.0 and time_since_off >= max(180.0, duration_ref)

        if below_target or (slope_cooling and time_since_off >= 60.0) or long_wait or self._zone_heater_on:
            self._finalize_cycle_evaluation(now_ts, force_peak=effective_temp)

    def _finalize_cycle_evaluation(self, now_ts: float, *, force_peak: Optional[float] = None) -> None:
        """Finalize pending cycle diagnostics and feed the model."""
        if not self._pending_cycle_eval:
            return

        cycle = self._pending_cycle_eval
        peak_temp = cycle.get("peak_temp")
        if force_peak is not None:
            peak_temp = max(force_peak, peak_temp) if isinstance(peak_temp, (int, float)) else force_peak
        if peak_temp is None:
            fallback = cycle.get("off_temp") or cycle.get("last_temp") or cycle.get("start_temp")
            peak_temp = float(fallback) if isinstance(fallback, (int, float)) else None

        start_temp = cycle.get("start_temp")
        tau_on = cycle.get("actual_on_duration") or cycle.get("planned_duration")
        target = cycle.get("target", self._target_temperature)

        self._pending_cycle_eval = None
        self._attr_extra_state_attributes["cycle_on_duration_s"] = None
        self._attr_extra_state_attributes["cycle_tail_duration_s"] = None
        self._attr_extra_state_attributes["cycle_time_to_target_s"] = None
        if start_temp is None or tau_on is None or peak_temp is None:
            self.async_write_ha_state()
            return

        diagnostics = self._thermal_controller.register_cycle_result(
            float(start_temp),
            float(peak_temp),
            float(tau_on),
            temp_target=float(target),
        )
        self._attr_extra_state_attributes["thermal_params"] = self._current_thermal_param_payload()
        if diagnostics:
            self._last_cycle_diagnostics = diagnostics
            self._attr_extra_state_attributes["last_cycle_observed_at"] = self._iso_or_none(now_ts)
            self._mark_state_dirty()
        self.async_write_ha_state()

    async def _async_control_heating(self, now_ts: float) -> None:
        """Apply model-based control with window-aware gating."""
        if self._target_temperature is None:
            return

        effective_temp = (
            self._filtered_temperature
            if self._filtered_temperature is not None
            else self._current_temperature
        )
        if effective_temp is None:
            return

        self._update_cycle_tracking(now_ts, effective_temp)

        if self._open_window_detected:
            if self._zone_heater_on:
                await self._async_turn_heater_off()
            return

        min_on_active = (
            self._zone_heater_on
            and self._last_command_timestamp is not None
            and now_ts - self._last_command_timestamp < self._thermal_controller.min_on_s
        )
        min_off_active = (
            not self._zone_heater_on
            and self._last_command_timestamp is not None
            and now_ts - self._last_command_timestamp < self._thermal_controller.min_off_s
        )

        deadband = self._thermal_controller.deadband
        upper_band = self._target_temperature + deadband
        lower_band = self._target_temperature - deadband

        if self._zone_heater_on:
            should_turn_off = False
            if self._planned_heater_off_ts is not None and now_ts >= self._planned_heater_off_ts and not min_on_active:
                should_turn_off = True
            elif effective_temp >= self._target_temperature + deadband and not min_on_active:
                should_turn_off = True
                _LOGGER.warning(
                    "[%s] Failsafe OFF (temp %.2f°C beyond target %.2f°C)",
                    self._entry_id,
                    effective_temp,
                    self._target_temperature,
                )

            if should_turn_off:
                _LOGGER.info(
                    "[%s] Turning heat OFF (temp=%.2f°C, target=%.2f°C)",
                    self._entry_id,
                    effective_temp,
                    self._target_temperature,
                )
                await self._async_turn_heater_off()
            return

        if self._window_heat_reenable_at is not None and now_ts < self._window_heat_reenable_at:
            if effective_temp <= lower_band:
                _LOGGER.debug(
                    "[%s] Heating suppressed for %ss post-window recovery",
                    self._entry_id,
                    round(self._window_heat_reenable_at - now_ts, 1),
                )
            return

        if min_off_active:
            return

        if effective_temp > lower_band:
            return

        tau_on = self._thermal_controller.propose_on_time(effective_temp, self._target_temperature)
        tau_on = max(tau_on, float(self._thermal_controller.min_on_s))
        tail_delay = self._thermal_controller.residual_peak_delay()
        if self._post_window_dampen_cycles > 0:
            tau_on = max(float(self._thermal_controller.min_on_s), tau_on * WINDOW_POST_RECOVERY_DAMPEN_SCALE)
        time_to_target = tau_on + tail_delay
        if tau_on <= 0:
            return

        if self._planned_off_unsub:
            self._planned_off_unsub()
            self._planned_off_unsub = None

        self._attr_extra_state_attributes["cycle_on_duration_s"] = tau_on
        self._attr_extra_state_attributes["cycle_tail_duration_s"] = tail_delay
        self._attr_extra_state_attributes["cycle_time_to_target_s"] = time_to_target
        _LOGGER.info(
            "[%s] Turning heat ON (temp=%.2f°C, target=%.2f°C, duration=%.0fs)",
            self._entry_id,
            effective_temp,
            self._target_temperature,
            tau_on,
        )
        await self._async_turn_heater_on()
        self._active_cycle = {
            "start_ts": now_ts,
            "start_temp": float(effective_temp),
            "target": float(self._target_temperature),
            "planned_duration": float(tau_on),
            "peak_temp": float(effective_temp),
            "predicted_tail": float(tail_delay),
            "predicted_time_to_target": float(time_to_target),
        }
        if self._post_window_dampen_cycles > 0:
            self._post_window_dampen_cycles = max(0, self._post_window_dampen_cycles - 1)
            self._attr_extra_state_attributes["post_window_dampen_cycles"] = self._post_window_dampen_cycles
        self._planned_heater_off_ts = now_ts + tau_on
        self._planned_on_duration = tau_on
        self._attr_extra_state_attributes["planned_zone_on_duration"] = tau_on
        self._attr_extra_state_attributes["planned_zone_off_time"] = self._iso_or_none(self._planned_heater_off_ts)
        self._planned_off_unsub = async_call_later(self.hass, tau_on, self._async_handle_planned_turn_off)
        self.async_write_ha_state()

    async def _async_handle_auto_onoff(
        self,
        outdoor_temp: Optional[float],
        backup_outdoor_temp: Optional[float],
    ) -> None:
        """Automatically toggle HVAC mode based on outdoor temperature."""
        temp = outdoor_temp if outdoor_temp is not None else backup_outdoor_temp
        if temp is None:
            return

        if self._last_outdoor_temp is not None and abs(temp - self._last_outdoor_temp) < 0.5:
            return

        self._last_outdoor_temp = temp
        self._current_outdoor_temp = temp

        if temp < self._auto_on_temp and self._hvac_mode == HVACMode.OFF:
            _LOGGER.info(
                "[%s] Auto turning ON (outdoor %.2f°C < %.2f°C)",
                self._entry_id,
                temp,
                self._auto_on_temp,
            )
            self._hvac_mode = HVACMode.HEAT
            await self._async_control_heating(dt_util.utcnow().timestamp())
            self._mark_state_dirty()
        elif temp > self._auto_off_temp and self._hvac_mode == HVACMode.HEAT:
            _LOGGER.info(
                "[%s] Auto turning OFF (outdoor %.2f°C > %.2f°C)",
                self._entry_id,
                temp,
                self._auto_off_temp,
            )
            await self._async_turn_heater_off()
            self._hvac_mode = HVACMode.OFF
            self._mark_state_dirty()

    async def _async_turn_heater_on(self) -> None:
        """Turn on valves and coordinate central heater."""
        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        for heater_id in self._heater_entity_ids:
            await self._async_turn_on_entity(heater_id, "zone heater/valve")

        if self._central_heater_entity_id:
            await self._async_coordinate_central_heater_on()

        now_ts = dt_util.utcnow().timestamp()
        self._zone_heater_on = True
        self._last_command_timestamp = now_ts
        self._attr_extra_state_attributes["zone_heater_on"] = True
        self._mark_state_dirty()

    async def _async_turn_heater_off(self) -> None:
        """Turn off valves and coordinate central heater."""
        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        if self._planned_off_unsub:
            self._planned_off_unsub()
            self._planned_off_unsub = None
        self._planned_heater_off_ts = None
        self._planned_on_duration = None
        self._attr_extra_state_attributes["planned_zone_off_time"] = None
        self._attr_extra_state_attributes["planned_zone_on_duration"] = None

        other_zones_need_heat = False
        if self._central_heater_entity_id:
            other_zones_need_heat = await self._async_check_other_zones_need_heat()

        if other_zones_need_heat:
            await self._async_close_zone_valves()
        else:
            delay = max(0.0, self._central_heater_turn_off_delay)
            if delay > 0:
                self._delayed_valve_off_task = asyncio.create_task(self._async_delayed_close_zone_valves(delay))
            else:
                await self._async_close_zone_valves()

        if self._central_heater_entity_id:
            await self._async_coordinate_central_heater_off(other_zones_need_heat)

        now_ts = dt_util.utcnow().timestamp()
        off_temp = (
            self._filtered_temperature
            if self._filtered_temperature is not None
            else self._current_temperature
        )

        if self._pending_cycle_eval:
            self._finalize_cycle_evaluation(now_ts, force_peak=off_temp if isinstance(off_temp, (int, float)) else None)

        if self._active_cycle:
            cycle = dict(self._active_cycle)
            cycle["off_ts"] = now_ts
            start_ts = cycle.get("start_ts")
            if isinstance(start_ts, (int, float)):
                cycle["actual_on_duration"] = max(0.0, now_ts - float(start_ts))
            if isinstance(off_temp, (int, float)):
                cycle["last_temp"] = float(off_temp)
                prev_peak = cycle.get("peak_temp", off_temp)
                cycle["peak_temp"] = max(float(off_temp), float(prev_peak))
                cycle["off_temp"] = float(off_temp)
            self._pending_cycle_eval = cycle
            self._active_cycle = None

        self._zone_heater_on = False
        self._last_command_timestamp = now_ts
        self._attr_extra_state_attributes["zone_heater_on"] = False
        self._mark_state_dirty()

    async def _async_close_zone_valves(self) -> None:
        """Close all zone valves immediately."""
        for heater_id in self._heater_entity_ids:
            await self._async_turn_off_entity(heater_id, "zone heater/valve")

    async def _async_delayed_close_zone_valves(self, delay: float) -> None:
        """Close zone valves after a delay."""
        try:
            await asyncio.sleep(delay)
            await self._async_close_zone_valves()
        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Delayed valve close task cancelled", self._entry_id)
        finally:
            self._delayed_valve_off_task = None

    async def _async_coordinate_central_heater_on(self) -> None:
        """Turn on central heater after valves."""
        if not self._central_heater_entity_id:
            return

        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None

        self._central_heater_task = asyncio.create_task(self._async_delayed_central_heater_on())

    async def _async_coordinate_central_heater_off(self, other_zones_need_heat: bool) -> None:
        """Turn off central heater if no zones require heat."""
        if not self._central_heater_entity_id:
            return

        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None

        if other_zones_need_heat:
            _LOGGER.debug("[%s] Keeping central heater ON (other zone requires heat)", self._entry_id)
            return

        await self._async_turn_off_entity(self._central_heater_entity_id, "central heater")

    async def _async_delayed_central_heater_on(self) -> None:
        """Turn on central heater with optional delay."""
        try:
            if self._central_heater_turn_on_delay > 0:
                await asyncio.sleep(self._central_heater_turn_on_delay)
            if self._zone_heater_on:
                await self._async_turn_on_entity(self._central_heater_entity_id, "central heater")
        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Central heater task cancelled", self._entry_id)
        finally:
            self._central_heater_task = None

    async def _async_check_other_zones_need_heat(self) -> bool:
        """Check if another thermostat using the same central heater still requests heat."""
        domain_data = self.hass.data.get(DOMAIN, {})
        entities = domain_data.get("entities", {})
        for entity_id, entity in entities.items():
            if (
                entity is not self
                and getattr(entity, "_central_heater_entity_id", None) == self._central_heater_entity_id
                and getattr(entity, "_zone_heater_on", False)
                and getattr(entity, "_hvac_mode", HVACMode.OFF) == HVACMode.HEAT
            ):
                return True
        return False

    async def _async_turn_on_entity(self, entity_id: Optional[str], label: str) -> None:
        """Helper to turn on a Home Assistant entity."""
        if not entity_id:
            return
        domain, service = self._resolve_domain_and_service(entity_id, True)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("[%s] Failed to turn ON %s %s: %s", self._entry_id, label, entity_id, err)

    async def _async_turn_off_entity(self, entity_id: Optional[str], label: str) -> None:
        """Helper to turn off a Home Assistant entity."""
        if not entity_id:
            return
        domain, service = self._resolve_domain_and_service(entity_id, False)
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("[%s] Failed to turn OFF %s %s: %s", self._entry_id, label, entity_id, err)

    def _resolve_domain_and_service(self, entity_id: str, turn_on: bool) -> Tuple[str, str]:
        """Resolve appropriate service for an entity."""
        if "." not in entity_id:
            return "switch", "turn_on" if turn_on else "turn_off"
        domain = entity_id.split(".", 1)[0]
        if domain == "valve":
            return "valve", "open_valve" if turn_on else "close_valve"
        if domain in {"switch", "input_boolean", "climate"}:
            return domain, "turn_on" if turn_on else "turn_off"
        return domain, "turn_on" if turn_on else "turn_off"

    def _gather_heater_states(self) -> List[Dict[str, Any]]:
        """Return current state of each heater entity."""
        states: List[Dict[str, Any]] = []
        for heater_id in self._heater_entity_ids:
            state = self.hass.states.get(heater_id)
            if state:
                states.append(
                    {
                        "entity_id": heater_id,
                        "state": state.state,
                        "friendly_name": state.attributes.get("friendly_name", heater_id),
                    }
                )
        return states

    def _gather_central_state(self) -> Optional[Dict[str, Any]]:
        """Return central heater state."""
        if not self._central_heater_entity_id:
            return None
        state = self.hass.states.get(self._central_heater_entity_id)
        if not state:
            return None
        return {
            "entity_id": self._central_heater_entity_id,
            "state": state.state,
            "friendly_name": state.attributes.get("friendly_name", self._central_heater_entity_id),
        }

    def _serialize_runtime_state(self) -> Dict[str, Any]:
        """Serialize runtime state for persistence."""
        hvac_mode_value = self._hvac_mode.value if isinstance(self._hvac_mode, HVACMode) else HVACMode.OFF.value
        return {
            "hvac_mode": hvac_mode_value,
            "target_temperature": self._target_temperature,
            "current_preset": self._current_preset,
            "manual_override": self._manual_override,
            "zone_heater_on": self._zone_heater_on,
            "thermal_state": self._thermal_controller.get_runtime_state(),
        }

    def _mark_state_dirty(self) -> None:
        """Schedule persistence of runtime state."""
        if not self._state_store:
            return
        self._state_dirty = True
        if self._state_save_unsub is None:
            self._state_save_unsub = async_call_later(
                self.hass, STATE_SAVE_DELAY_SECONDS, self._async_persist_runtime_state
            )

    async def _async_persist_runtime_state(self, _now: Optional[datetime] = None) -> None:
        """Persist runtime state."""
        self._state_save_unsub = None
        if not self._state_store or not self._state_dirty:
            return

        cache = self._state_cache_ref
        if cache is None:
            cache = {}
            self._state_cache_ref = cache

        cache[self._entry_id] = self._serialize_runtime_state()
        await self._state_store.async_save(cache)
        self._state_dirty = False

    async def _async_load_runtime_state(self) -> None:
        """Load runtime state after restart."""
        if not self._state_store:
            return

        cache = self._state_cache_ref
        if cache is None or not cache:
            stored = await self._state_store.async_load()
            cache = stored or {}
            self._state_cache_ref = cache

        data = cache.get(self._entry_id)
        if not data:
            return

        hvac_mode_value = data.get("hvac_mode", HVACMode.OFF.value)
        try:
            self._hvac_mode = HVACMode(hvac_mode_value)
        except ValueError:
            self._hvac_mode = HVACMode.OFF

        target = data.get("target_temperature")
        if isinstance(target, (int, float)):
            self._target_temperature = _clamp(float(target), MIN_TARGET_TEMP, MAX_TARGET_TEMP)
            self._thermal_controller.target = self._target_temperature

        thermal_state = data.get("thermal_state")
        if isinstance(thermal_state, dict):
            self._thermal_controller.restore_runtime_state(thermal_state)
            self._thermal_controller.target = self._target_temperature

        preset = data.get("current_preset")
        if preset in self._presets:
            self._current_preset = preset
        else:
            self._current_preset = None

        manual_override = bool(data.get("manual_override", False))
        self._manual_override = manual_override
        self._attr_extra_state_attributes["manual_override"] = manual_override

        self._zone_heater_on = False  # never resume with valve open on startup
        self._attr_extra_state_attributes["zone_heater_on"] = False
        self._attr_extra_state_attributes["thermal_params"] = self._current_thermal_param_payload()

        _LOGGER.info(
            "[%s] Restored state hvac_mode=%s target=%.2f preset=%s manual_override=%s",
            self._entry_id,
            self._hvac_mode,
            self._target_temperature,
            self._current_preset,
            manual_override,
        )

    async def _async_control_tick(self, _now: datetime) -> None:
        """Periodic control tick to ensure regular evaluation."""
        self.async_schedule_update_ha_state(True)

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Handle tracked entity state changes."""
        self.async_schedule_update_ha_state(True)

    def reset_manual_override(self) -> None:
        """Clear manual override so auto on/off can resume control."""
        self._manual_override = False
        self._attr_extra_state_attributes["manual_override"] = False
        self._mark_state_dirty()
        self.async_write_ha_state()
