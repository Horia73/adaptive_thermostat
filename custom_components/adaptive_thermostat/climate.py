"""Climate platform for the Adaptive Thermostat integration."""

import asyncio
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from homeassistant.components.climate import ( # type: ignore
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import ( # type: ignore
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_OFF,
    CONF_NAME,
    UnitOfTemperature,
)
from homeassistant.config_entries import ConfigEntry # type: ignore
from homeassistant.core import HomeAssistant, callback # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback # type: ignore
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_call_later,
    Event,
) # type: ignore
from homeassistant.helpers.storage import Store # type: ignore
from homeassistant.helpers.dispatcher import async_dispatcher_send # type: ignore
from homeassistant.helpers.device_registry import DeviceInfo # type: ignore
from homeassistant.util import dt as dt_util # type: ignore

from . import DOMAIN

# Import all configuration constants used
from .const import (
    CONF_HEATER,
    CONF_CENTRAL_HEATER,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_DOOR_WINDOW_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_BACKUP_OUTDOOR_SENSOR,
    CONF_SLEEP_PRESET,
    CONF_HOME_PRESET,
    CONF_AWAY_PRESET,
    CONF_TARGET_TOLERANCE,
    CONF_CONTROL_WINDOW,
    CONF_MIN_ON_TIME,
    CONF_MIN_OFF_TIME,
    CONF_WINDOW_DETECTION_ENABLED,
    CONF_WINDOW_SLOPE_THRESHOLD,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_AUTO_ON_TEMP,
    CONF_AUTO_OFF_TEMP,
    DEFAULT_NAME,
    DEFAULT_HOME_PRESET,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_AWAY_PRESET,
    CENTRAL_HEATER_TURN_ON_DELAY,
    CENTRAL_HEATER_TURN_OFF_DELAY,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_AUTO_OFF_TEMP,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_CONTROL_WINDOW,
    DEFAULT_MIN_ON_TIME,
    DEFAULT_MIN_OFF_TIME,
    DEFAULT_WINDOW_DETECTION_ENABLED,
    DEFAULT_WINDOW_SLOPE_THRESHOLD,
    MIN_TARGET_TEMP,
    MAX_TARGET_TEMP,
    STORAGE_KEY,
    STORAGE_VERSION,
    SIGNAL_THERMOSTAT_READY,
)

_LOGGER = logging.getLogger(__name__)
MIN_SLOPE_THRESHOLD = 0.0002  # °C per second (~0.012°C per minute)
SLOPE_CHANGE_EPSILON = 0.001  # °C change required to treat as a new slope sample
DEFAULT_DELAY_SECONDS = 90.0
CONTROL_KP = 0.6
CONTROL_KI = 0.0005
CYCLE_ENTRY_DELTA = 0.1  # °C below target where duty cycling kicks in
CYCLE_EXIT_DELTA = 0.2  # °C below target required to leave cycling mode
MAX_PROFILE_HISTORY = 1000
COMFORT_EPSILON = 0.01  # Minimum meaningful delta when comparing to target
LEARNING_RETENTION = timedelta(days=10)
OVERSHOOT_MIN_TRACK_SECONDS = 300.0  # Keep tracking at least 5 minutes
OVERSHOOT_MAX_TRACK_SECONDS = 1800.0  # Safety cap ~30 minutes
RUN_LOG_LENGTH = 20
PROFILE_HISTORY_ATTR_LIMIT = 5


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp value within bounds."""
    return max(low, min(high, value))


@dataclass
class AdaptiveProfile:
    """Persisted adaptive model for a heating zone."""

    heater_gain: float = 0.0
    loss_coefficient: float = 0.0
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    heating_rate: float = 0.0
    cooling_rate: float = 0.0
    overshoot: float = 0.0
    updated_at: float = 0.0
    heating_samples: int = 0
    cooling_samples: int = 0
    delay_samples: int = 0
    overshoot_samples: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_learned(self) -> bool:
        return self.heater_gain > 0 and self.loss_coefficient > 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdaptiveProfile":
        if not data:
            return cls()
        base = cls()
        cleaned: Dict[str, Any] = {}
        for key in cls.__annotations__.keys():
            default_value = getattr(base, key)
            value = data.get(key, default_value)
            if key == "history":
                if isinstance(value, list):
                    value = [dict(item) for item in value if isinstance(item, dict)]
                else:
                    value = list(default_value)
            cleaned[key] = value

        profile = cls(**cleaned)
        profile.heating_samples = int(profile.heating_samples or 0)
        profile.cooling_samples = int(profile.cooling_samples or 0)
        profile.delay_samples = int(profile.delay_samples or 0)
        profile.overshoot_samples = int(profile.overshoot_samples or 0)
        profile.history = list(profile.history or [])[-MAX_PROFILE_HISTORY:]
        return profile

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Adaptive Thermostat climate platform."""
    _LOGGER.info("Setting up climate entity for entry %s with data: %s", entry.entry_id, entry.data)
    try:
        thermostat = AdaptiveThermostat(hass, entry)
        _LOGGER.info("Created thermostat entity: %s (unique_id: %s)", thermostat.name, thermostat.unique_id)
        async_add_entities([thermostat], True)  # Force immediate update
        _LOGGER.info("Successfully added thermostat entity to Home Assistant")
    except Exception as e:
        _LOGGER.error("Failed to set up climate entity: %s", e, exc_info=True)
        raise


class AdaptiveThermostat(ClimateEntity):
    """Representation of an Adaptive Thermostat zone."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the thermostat."""
        self._hass = hass
        self._attr_unique_id = entry.entry_id
        self._entry_id = entry.entry_id

        # --- Read configuration: prioritize options, fall back to data ---
        config = {**entry.data, **entry.options}
        _LOGGER.debug("[%s] Initializing with combined config: %s", self._entry_id, config)

        # --- Helper function to handle None or "" ---
        def get_entity_id(key):
            """Return entity_id or None if value is None or empty string."""
            val = config.get(key)
            return val if val else None
        # --- End Helper ---

        def clamp_temp(value: Any, fallback: float) -> float:
            """Clamp configuration temperatures to supported range."""
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = fallback
            return _clamp(numeric, MIN_TARGET_TEMP, MAX_TARGET_TEMP)

        def coerce_bool(value: Any, default: bool = False) -> bool:
            """Convert config values that may be strings/ints into booleans."""
            if isinstance(value, bool):
                return value
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        # Required configuration
        self._attr_name = config.get(CONF_NAME, DEFAULT_NAME)
        self._heater_entity_id = config.get(CONF_HEATER)
        self._temp_sensor_entity_id = config.get(CONF_TEMP_SENSOR)

        # CRITICAL: Log zone initialization for debugging
        _LOGGER.info("[%s] *** INITIALIZING ZONE '%s' ***", self._entry_id, self._attr_name)
        _LOGGER.info("[%s] *** UNIQUE ID: %s ***", self._entry_id, self._attr_unique_id)

        # Handle multiple heaters - convert single heater to list format
        heater_config = config.get(CONF_HEATER)
        if heater_config:
            if isinstance(heater_config, str):
                self._heater_entity_ids = [heater_config]
            elif isinstance(heater_config, list):
                self._heater_entity_ids = heater_config
            else:
                self._heater_entity_ids = [heater_config]
        else:
            self._heater_entity_ids = []
        
        # Keep backward compatibility for single heater reference
        self._heater_entity_id = self._heater_entity_ids[0] if self._heater_entity_ids else None

        # Central heater configuration (optional)
        self._central_heater_entity_id = get_entity_id(CONF_CENTRAL_HEATER)

        # Central heater timing configuration
        self._central_heater_turn_on_delay = config.get(
            CONF_CENTRAL_HEATER_TURN_ON_DELAY, CENTRAL_HEATER_TURN_ON_DELAY
        )
        self._central_heater_turn_off_delay = config.get(
            CONF_CENTRAL_HEATER_TURN_OFF_DELAY, CENTRAL_HEATER_TURN_OFF_DELAY
        )

        # Ensure the central heater is not also treated as a zone valve
        if self._central_heater_entity_id:
            self._heater_entity_ids = [
                heater_id
                for heater_id in self._heater_entity_ids
                if heater_id != self._central_heater_entity_id
            ]
        
        # Auto on/off configuration
        self._auto_on_off_enabled = config.get(CONF_AUTO_ON_OFF_ENABLED, False)
        self._auto_on_temp = config.get(CONF_AUTO_ON_TEMP, DEFAULT_AUTO_ON_TEMP)
        self._auto_off_temp = config.get(CONF_AUTO_OFF_TEMP, DEFAULT_AUTO_OFF_TEMP)

        # Adaptive control configuration
        self._target_tolerance = max(0.01, float(config.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE)))
        self._control_window = max(60.0, float(config.get(CONF_CONTROL_WINDOW, DEFAULT_CONTROL_WINDOW)))
        self._configured_control_window = self._control_window
        self._dynamic_control_window: float | None = None
        self._min_on_time = max(10.0, float(config.get(CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME)))
        self._min_off_time = max(10.0, float(config.get(CONF_MIN_OFF_TIME, DEFAULT_MIN_OFF_TIME)))
        self._window_detection_enabled = coerce_bool(
            config.get(CONF_WINDOW_DETECTION_ENABLED, DEFAULT_WINDOW_DETECTION_ENABLED),
            DEFAULT_WINDOW_DETECTION_ENABLED,
        )
        self._window_slope_threshold = max(
            0.5, float(config.get(CONF_WINDOW_SLOPE_THRESHOLD, DEFAULT_WINDOW_SLOPE_THRESHOLD))
        )

        # Manual override state - when user manually controls the thermostat
        self._manual_override = False
        self._last_outdoor_temp = None
        self._current_outdoor_temp: float | None = None

        # Check required fields and warn
        if not self._heater_entity_ids:
            _LOGGER.warning("[%s] No heater entities configured for zone '%s' - entity will not be functional", self._entry_id, self._attr_name)
        if not self._temp_sensor_entity_id:
            _LOGGER.warning("[%s] Temperature sensor missing for zone '%s' - using default values", self._entry_id, self._attr_name)
            # Set a reasonable default temperature to prevent entity from being unavailable
            self._current_temperature = 20.0

        # Optional configuration sensors
        self._humidity_sensor_entity_id = get_entity_id(CONF_HUMIDITY_SENSOR)
        self._door_window_sensor_entity_id = get_entity_id(CONF_DOOR_WINDOW_SENSOR)
        self._motion_sensor_entity_id = get_entity_id(CONF_MOTION_SENSOR)
        self._outdoor_sensor_entity_id = get_entity_id(CONF_OUTDOOR_SENSOR)
        self._backup_outdoor_sensor_entity_id = get_entity_id(CONF_BACKUP_OUTDOOR_SENSOR)

        # Preset temperatures
        self._presets = {
            "sleep": clamp_temp(config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET), DEFAULT_SLEEP_PRESET),
            "home": clamp_temp(config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET), DEFAULT_HOME_PRESET),
            "away": clamp_temp(config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET), DEFAULT_AWAY_PRESET),
        }

        # Internal state attributes
        self._current_temperature: float | None = None
        self._current_humidity: float | None = None
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._target_temperature: float = self._presets["home"]
        self._current_preset: str = "home"

        # Climate entity attributes
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_preset_modes = list(self._presets.keys())
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
        )
        self._attr_temperature_unit = self._hass.config.units.temperature_unit

        # Extra state attributes for UI card
        self._attr_extra_state_attributes = {
            "heater_entity_id": self._heater_entity_id,
            "heater_entity_ids": self._heater_entity_ids,
            "central_heater_entity_id": self._central_heater_entity_id,
            "temp_sensor_entity_id": self._temp_sensor_entity_id,
            "humidity_sensor": self._humidity_sensor_entity_id,
            "motion_sensor": self._motion_sensor_entity_id,
            "door_window_sensor": self._door_window_sensor_entity_id,
            "outdoor_sensor": self._outdoor_sensor_entity_id,
            "weather_sensor": self._backup_outdoor_sensor_entity_id,  # Renamed for card compatibility
            "manual_override": False,  # Track manual override state for card
            "window_detection_enabled": self._window_detection_enabled,
            "window_open_detected": False,
            "window_slope_threshold": self._window_slope_threshold,
            "window_alert": None,
            "cycle_mode_active": False,
            "cycle_entry_delta": CYCLE_ENTRY_DELTA,
            "cycle_exit_delta": CYCLE_EXIT_DELTA,
            "min_target_temp": MIN_TARGET_TEMP,
            "max_target_temp": MAX_TARGET_TEMP,
            "control_temperature": None,
            "last_run_duration_seconds": None,
            "last_overshoot_capture": None,
            "run_history": [],
            "total_run_count": 0,
            "cycle_entries": 0,
            "current_run": None,
        }
        _LOGGER.debug("[%s] Extra state attributes set: %s", self._entry_id, self._attr_extra_state_attributes)

        # Central heater coordination state
        self._zone_heater_on = False
        self._central_heater_task = None
        self._delayed_valve_off_task = None

        # Listener for state changes
        self._remove_listener = None

        # Adaptive control internal state
        self._filtered_temperature: float | None = None
        self._temperature_slope: float = 0.0
        self._raw_temperature_slope: float = 0.0
        self._last_measurement_ts: float | None = None
        self._last_measurement_temp: float | None = None
        self._display_temperature_slope: float = 0.0
        self._last_update_ts: float | None = None
        self._integrator: float = 0.0
        self._window_start_ts: float | None = None
        self._window_on_time: float = 0.0
        self._window_desired_on: float = 0.0
        self._adaptive_profile: AdaptiveProfile = AdaptiveProfile()
        self._profile_store: Store | None = None
        self._profile_cache_ref: Dict[str, Any] | None = None
        self._profile_dirty: bool = False
        self._profile_save_unsub = None
        self._control_tick_unsub = None
        self._awaiting_delay_timestamp: float | None = None
        self._awaiting_peak: bool = False
        self._peak_max_temp: float | None = None
        self._peak_target: float | None = None
        self._peak_run_duration: float | None = None
        self._peak_track_start_ts: float | None = None
        self._last_command_timestamp: float | None = None
        self._last_heater_command: Optional[bool] = None
        self._cycle_mode_active: bool = False
        self._open_window_detected: bool = False
        self._last_window_event_ts: float | None = None
        self._last_run_duration: float | None = None
        self._run_history: list[Dict[str, Any]] = []
        self._current_run_record: Optional[Dict[str, Any]] = None
        self._pending_overshoot_record: Optional[Dict[str, Any]] = None
        self._run_sequence: int = 0
        self._total_run_count: int = 0
        self._cycle_entries: int = 0

        # Remove device creation - not needed for single climate entity

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        _LOGGER.info("[%s] *** ZONE '%s' STARTING UP - Entity: %s ***", self._entry_id, self._attr_name, self.entity_id)

        # Track entity instance so services can address it by entity_id
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        entry_map = domain_data.setdefault("entry_to_entity_id", {})

        if "profile_store" not in domain_data:
            domain_data["profile_store"] = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        if "profile_cache" not in domain_data:
            domain_data["profile_cache"] = {}

        self._profile_store = domain_data["profile_store"]
        self._profile_cache_ref = domain_data["profile_cache"]

        entities = domain_data.setdefault("entities", {})
        if self.entity_id:
            entities[self.entity_id] = self

        # Validate that configured entities exist
        await self._validate_entities()

        try:
            await self._async_load_profile()
            # CRITICAL: Load all sensor data immediately for card
            await self.async_update()
            
            # CRITICAL: Write state immediately so card gets data on reload
            self.async_write_ha_state()
            _LOGGER.info("[%s] *** ZONE '%s' ENTITY READY: %s ***", self._entry_id, self._attr_name, self.entity_id)
            if self.entity_id:
                entry_map[self._entry_id] = self.entity_id
                async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self._entry_id}", self.entity_id)
        except Exception as e:
            _LOGGER.error("[%s] Error during entity initialization: %s", self._entry_id, e, exc_info=True)

        # Register state change listeners
        entities_to_track = []
        if self._temp_sensor_entity_id:
            entities_to_track.append(self._temp_sensor_entity_id)
        if self._humidity_sensor_entity_id:
            entities_to_track.append(self._humidity_sensor_entity_id)
        
        # Add all heater entities to tracking
        for heater_id in self._heater_entity_ids:
            if heater_id and heater_id not in entities_to_track:
                entities_to_track.append(heater_id)

        # Add all optional sensors to tracking
        if self._outdoor_sensor_entity_id:
            entities_to_track.append(self._outdoor_sensor_entity_id)
        if self._backup_outdoor_sensor_entity_id:
            entities_to_track.append(self._backup_outdoor_sensor_entity_id)
        if self._motion_sensor_entity_id:
            entities_to_track.append(self._motion_sensor_entity_id)
        if self._door_window_sensor_entity_id:
            entities_to_track.append(self._door_window_sensor_entity_id)

        if entities_to_track:
            self._remove_listener = async_track_state_change_event(
                self.hass, entities_to_track, self._async_state_changed
            )
            _LOGGER.info("[%s] Tracking %d entities for zone '%s': %s", 
                        self._entry_id, len(entities_to_track), self._attr_name, entities_to_track)
        else:
            _LOGGER.warning("[%s] No valid sensors/heater configured for state tracking.", self._entry_id)

        # Periodic control tick to keep adaptive loop active even without sensor state changes
        control_interval = max(15.0, min(60.0, self._control_window / 4.0))
        self._control_tick_unsub = async_track_time_interval(
            self.hass, self._async_control_tick, timedelta(seconds=control_interval)
        )

        # Schedule periodic updates to ensure card stays current
        self.async_schedule_update_ha_state(True)

    async def _validate_entities(self) -> None:
        """Validate that configured entities exist in Home Assistant."""
        _LOGGER.info("[%s] Validating configured entities for zone '%s'", self._entry_id, self._attr_name)
        
        # Check heater entities
        missing_heaters = []
        for heater_id in self._heater_entity_ids:
            if heater_id and not self.hass.states.get(heater_id):
                missing_heaters.append(heater_id)
        
        if missing_heaters:
            _LOGGER.error("[%s] Missing heater entities: %s", self._entry_id, missing_heaters)
        else:
            _LOGGER.info("[%s] ✅ All heater entities found", self._entry_id)
        
        # Check temperature sensor
        if self._temp_sensor_entity_id:
            temp_state = self.hass.states.get(self._temp_sensor_entity_id)
            if not temp_state:
                _LOGGER.error("[%s] ❌ Temperature sensor not found: %s", self._entry_id, self._temp_sensor_entity_id)
            else:
                _LOGGER.info("[%s] ✅ Temperature sensor found: %s (current: %s)", 
                           self._entry_id, self._temp_sensor_entity_id, temp_state.state)
        
        # Check outdoor sensor
        if self._outdoor_sensor_entity_id:
            outdoor_state = self.hass.states.get(self._outdoor_sensor_entity_id)
            if not outdoor_state:
                _LOGGER.error("[%s] ❌ Outdoor sensor not found: %s", self._entry_id, self._outdoor_sensor_entity_id)
            else:
                _LOGGER.info("[%s] ✅ Outdoor sensor found: %s (current: %s)", 
                           self._entry_id, self._outdoor_sensor_entity_id, outdoor_state.state)
        
        # Check optional sensors
        optional_sensors = [
            (self._humidity_sensor_entity_id, "Humidity"),
            (self._motion_sensor_entity_id, "Motion"),
            (self._door_window_sensor_entity_id, "Door/Window"),
            (self._backup_outdoor_sensor_entity_id, "Backup Outdoor"),
            (self._central_heater_entity_id, "Central Heater")
        ]
        
        for entity_id, sensor_type in optional_sensors:
            if entity_id:
                state = self.hass.states.get(entity_id)
                if not state:
                    _LOGGER.warning("[%s] ⚠️ Optional %s sensor not found: %s", self._entry_id, sensor_type, entity_id)
                else:
                    _LOGGER.info("[%s] ✅ %s sensor found: %s", self._entry_id, sensor_type, entity_id)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _LOGGER.debug("[%s] Removing adaptive thermostat from Home Assistant", self._entry_id)
        domain_data = self.hass.data.get(DOMAIN, {})

        # Cancel any pending central heater tasks
        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None

        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
            _LOGGER.debug("[%s] Unsubscribed state listener.", self._entry_id)

        if self._control_tick_unsub:
            self._control_tick_unsub()
            self._control_tick_unsub = None

        if self._profile_save_unsub:
            self._profile_save_unsub()
            self._profile_save_unsub = None

        if self._profile_dirty:
            await self._async_persist_profile()

        # Remove entity from tracked instances
        entities = domain_data.get("entities")
        if entities and self.entity_id in entities:
            entities.pop(self.entity_id, None)

        entry_map = domain_data.get("entry_to_entity_id")
        if entry_map and self._entry_id in entry_map:
            entry_map.pop(self._entry_id, None)
            async_dispatcher_send(self.hass, f"{SIGNAL_THERMOSTAT_READY}_{self._entry_id}", None)

        await super().async_will_remove_from_hass()

    # --- Properties ---

    @property
    def name(self) -> str:
        """Return the display name of the thermostat."""
        return self._attr_name

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the entity."""
        return self._attr_unique_id

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity."""
        return self._current_humidity

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation ie. heat, cool mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation if supported."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        elif self._zone_heater_on:
            return HVACAction.HEATING
        else:
            return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp."""
        return self._current_preset

    @property
    def preset_modes(self) -> list[str] | None:
        """Return a list of available preset modes."""
        return list(self._presets.keys())

    @property
    def device_info(self) -> DeviceInfo:
        """Return device metadata for registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._attr_name,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the optional state attributes."""
        return self._attr_extra_state_attributes

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return MIN_TARGET_TEMP

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return MAX_TARGET_TEMP

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        return 0.1

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        # Entity is always available if it has a name - functionality depends on configuration
        return bool(self._attr_name)

    # --- Service calls ---

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.info("[%s] 👤 USER ACTION: Setting HVAC mode from %s to %s", 
                    self._entry_id, self._hvac_mode, hvac_mode)
        
        # Mark as manual override when user manually changes HVAC mode
        self._manual_override = True
        self._attr_extra_state_attributes["manual_override"] = True
        
        if hvac_mode == HVACMode.OFF:
            # First set HVAC mode to OFF to prevent control loop from running
            self._hvac_mode = HVACMode.OFF
            
            # Turn off heaters and update state
            if self._zone_heater_on:
                _LOGGER.info("[%s] 🛑 User turned OFF - shutting down zone heaters", self._entry_id)
                await self._async_turn_heater_off()
            self._zone_heater_on = False
            
            _LOGGER.info("[%s] ✅ Zone is now OFF (manual override active)", self._entry_id)
            
        elif hvac_mode == HVACMode.HEAT:
            self._hvac_mode = HVACMode.HEAT
            _LOGGER.info("[%s] 🔥 User enabled HEAT mode - starting adaptive control", self._entry_id)
            await self._async_control_heating(dt_util.utcnow().timestamp())
        else:
            _LOGGER.warning("[%s] Unsupported HVAC mode: %s", self._entry_id, hvac_mode)
            return

        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode not in self._presets:
            _LOGGER.warning("[%s] Invalid preset mode: %s", self._entry_id, preset_mode)
            return

        _LOGGER.debug("[%s] Setting preset mode to: %s", self._entry_id, preset_mode)
        
        # Mark as manual override when user manually changes preset
        self._manual_override = True
        self._attr_extra_state_attributes["manual_override"] = True
        
        self._current_preset = preset_mode
        target = self._presets[preset_mode]
        clamped_target = _clamp(target, MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        if clamped_target != target:
            _LOGGER.warning(
                "[%s] Preset '%s' temperature %.2f°C is outside supported range; clamped to %.2f°C",
                self._entry_id,
                preset_mode,
                target,
                clamped_target,
            )
        self._target_temperature = clamped_target
        
        # Trigger heating control with new target temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating(dt_util.utcnow().timestamp())
        
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        _LOGGER.debug("[%s] Setting target temperature to: %s°C", self._entry_id, temperature)
        
        # Mark as manual override when user manually sets temperature
        self._manual_override = True
        self._attr_extra_state_attributes["manual_override"] = True
        
        requested = float(temperature)
        clamped = _clamp(requested, MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        if clamped != requested:
            _LOGGER.warning(
                "[%s] Requested target %.2f°C is outside supported range; clamped to %.2f°C",
                self._entry_id,
                requested,
                clamped,
            )

        self._target_temperature = clamped
        
        # Clear preset mode when manually setting temperature
        self._current_preset = None
        
        # Trigger heating control with new target temperature
        if self._hvac_mode == HVACMode.HEAT:
            await self._async_control_heating(dt_util.utcnow().timestamp())
        
        self.async_write_ha_state()

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Handle state changes of tracked entities."""
        _LOGGER.debug("[%s] State changed event: %s", self._entry_id, event.data)
        # Schedule an update which will call async_update
        self.async_schedule_update_ha_state(True)

    async def async_update(self) -> None:
        """Update the entity state."""
        try:
            now = dt_util.utcnow()
            now_ts = dt_util.as_timestamp(now)
            dt = 0.0 if self._last_update_ts is None else max(0.0, now_ts - self._last_update_ts)

            raw_temp, temp_ts = self._read_temperature(now_ts)
            humidity = self._read_humidity()
            outdoor_temp, backup_outdoor_temp = self._read_outdoor_temperatures()
            motion_active = self._read_binary_sensor(self._motion_sensor_entity_id)
            door_window_open = self._read_binary_sensor(self._door_window_sensor_entity_id)
            heater_states = self._gather_heater_states()
            central_state = self._gather_central_state()

            if raw_temp is not None:
                self._current_temperature = raw_temp
                self._update_temperature_metrics(raw_temp, temp_ts, outdoor_temp, now_ts)
            if humidity is not None:
                self._current_humidity = humidity

            if outdoor_temp is not None:
                self._current_outdoor_temp = outdoor_temp
            elif backup_outdoor_temp is not None and self._current_outdoor_temp is None:
                self._current_outdoor_temp = backup_outdoor_temp

            filtered_for_control = self._filtered_temperature or self._current_temperature
            if (
                self._hvac_mode == HVACMode.HEAT
                and filtered_for_control is not None
                and self._target_temperature is not None
                and dt > 0
            ):
                error = self._target_temperature - filtered_for_control
                tolerance = self._target_tolerance if self._target_tolerance is not None else DEFAULT_TARGET_TOLERANCE
                tolerance = max(tolerance, COMFORT_EPSILON)
                if error <= -tolerance:
                    self._integrator = min(self._integrator, 0.0)
                elif error >= tolerance:
                    self._integrator = max(self._integrator, 0.0)
                self._integrator = _clamp(self._integrator + error * dt, -3600.0, 3600.0)

            self._advance_control_window(now_ts, dt)
            self._rolling_window_reset(now_ts)
            await self._async_update_window_detection(now_ts)

            self._update_sensor_attributes(
                outdoor_temp,
                backup_outdoor_temp,
                motion_active,
                door_window_open,
                heater_states,
                central_state,
                now,
            )

            # Run auto_on_off only when manual override is not active
            if self._auto_on_off_enabled:
                if self._manual_override:
                    _LOGGER.debug("[%s] Auto on/off skipped - manual override is active", self._entry_id)
                else:
                    await self._async_handle_auto_onoff(outdoor_temp, backup_outdoor_temp)

            if self._hvac_mode == HVACMode.HEAT:
                await self._async_control_heating(now_ts)

            self._last_update_ts = now_ts
        except Exception as err:
            _LOGGER.error("[%s] Error during update: %s", self._entry_id, err, exc_info=True)

    def _read_temperature(self, fallback_ts: float) -> tuple[Optional[float], Optional[float]]:
        """Read the main temperature sensor."""
        if not self._temp_sensor_entity_id:
            return self._current_temperature, fallback_ts

        state = self.hass.states.get(self._temp_sensor_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None, None

        try:
            value = float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("[%s] Invalid temperature from sensor %s: %s", self._entry_id, self._temp_sensor_entity_id, state.state)
            return None, None

        state_dt = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
        measurement_ts = dt_util.as_timestamp(state_dt) if state_dt else fallback_ts
        return value, measurement_ts

    def _read_humidity(self) -> Optional[float]:
        """Read humidity sensor value."""
        if not self._humidity_sensor_entity_id:
            return None

        state = self.hass.states.get(self._humidity_sensor_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None

        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "[%s] Invalid humidity from sensor %s: %s",
                self._entry_id,
                self._humidity_sensor_entity_id,
                state.state,
            )
            return None

    def _safe_state_float(self, entity_id: Optional[str]) -> Optional[float]:
        """Return sensor float value or None."""
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

    def _read_outdoor_temperatures(self) -> tuple[Optional[float], Optional[float]]:
        """Read outdoor and backup outdoor temperatures."""
        primary = self._safe_state_float(self._outdoor_sensor_entity_id)
        backup = self._safe_state_float(self._backup_outdoor_sensor_entity_id)
        return primary, backup

    def _read_binary_sensor(self, entity_id: Optional[str]) -> Optional[bool]:
        """Read binary sensor state as boolean."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        return state.state == STATE_ON

    def _gather_heater_states(self) -> List[Dict[str, Any]]:
        """Gather heater entity snapshots for diagnostics."""
        states: List[Dict[str, Any]] = []
        for heater_id in self._heater_entity_ids:
            heater_state = self.hass.states.get(heater_id)
            if heater_state:
                states.append(
                    {
                        "entity_id": heater_id,
                        "state": heater_state.state,
                        "friendly_name": heater_state.attributes.get("friendly_name", heater_id),
                    }
                )
        return states

    def _gather_central_state(self) -> Optional[Dict[str, Any]]:
        """Return central heater state snapshot."""
        if not self._central_heater_entity_id:
            return None
        central_state = self.hass.states.get(self._central_heater_entity_id)
        if not central_state:
            return None
        return {
            "entity_id": self._central_heater_entity_id,
            "state": central_state.state,
            "friendly_name": central_state.attributes.get("friendly_name", self._central_heater_entity_id),
        }

    def _summarize_adaptive_profile(self) -> Dict[str, Any]:
        """Return compact adaptive profile details for state attributes."""
        profile = self._adaptive_profile
        history_tail = profile.history[-PROFILE_HISTORY_ATTR_LIMIT:] if profile.history else []
        return {
            "heater_gain": round(profile.heater_gain, 6),
            "loss_coefficient": round(profile.loss_coefficient, 6),
            "delay_seconds": round(profile.delay_seconds, 2),
            "heating_rate": round(profile.heating_rate, 6),
            "cooling_rate": round(profile.cooling_rate, 6),
            "overshoot": round(profile.overshoot, 6),
            "updated_at": profile.updated_at,
            "heating_samples": profile.heating_samples,
            "cooling_samples": profile.cooling_samples,
            "delay_samples": profile.delay_samples,
            "overshoot_samples": profile.overshoot_samples,
            "history_length": len(profile.history),
            "history": history_tail,
        }

    def _update_sensor_attributes(
        self,
        outdoor_temp: Optional[float],
        backup_outdoor_temp: Optional[float],
        motion_active: Optional[bool],
        door_window_open: Optional[bool],
        heater_states: List[Dict[str, Any]],
        central_heater_state: Optional[Dict[str, Any]],
        now: datetime,
    ) -> None:
        """Update extra state attributes with current sensor readings."""
        sensor_time_state = self.hass.states.get("sensor.time")
        last_updated = (
            sensor_time_state.last_updated.isoformat()
            if sensor_time_state
            else now.isoformat()
        )

        last_window_event = (
            dt_util.utc_from_timestamp(self._last_window_event_ts).isoformat()
            if self._last_window_event_ts
            else None
        )

        slope_per_sec = self._display_temperature_slope
        slope_per_hour = slope_per_sec * 3600.0
        raw_slope_per_sec = self._raw_temperature_slope or 0.0
        raw_slope_per_hour = raw_slope_per_sec * 3600.0
        window_alert = self._attr_extra_state_attributes.get("window_alert")

        if self._filtered_temperature is None:
            control_temperature = self._current_temperature
        elif self._current_temperature is None:
            control_temperature = self._filtered_temperature
        else:
            control_temperature = min(self._filtered_temperature, self._current_temperature)

        adaptive_profile_state = self._summarize_adaptive_profile()
        adaptive_history = list(adaptive_profile_state.get("history", []))

        self._attr_extra_state_attributes.update({
            "zone_name": self._attr_name,
            "zone_id": self._entry_id,
            "zone_unique_id": self._attr_unique_id,
            "heater_entity_id": self._heater_entity_id,
            "heater_entity_ids": self._heater_entity_ids,
            "central_heater_entity_id": self._central_heater_entity_id,
            "temp_sensor_entity_id": self._temp_sensor_entity_id,
            "humidity_sensor": self._humidity_sensor_entity_id,
            "motion_sensor": self._motion_sensor_entity_id,
            "door_window_sensor": self._door_window_sensor_entity_id,
            "outdoor_sensor": self._outdoor_sensor_entity_id,
            "weather_sensor": self._backup_outdoor_sensor_entity_id,
            "preset_modes": list(self._presets.keys()),
            "current_preset_mode": self._current_preset,
            "preset_temperatures": self._presets,
            "current_outdoor_temp": outdoor_temp,
            "current_backup_outdoor_temp": backup_outdoor_temp,
            "current_motion_active": motion_active,
            "current_door_window_open": door_window_open,
            "hvac_modes": [mode.value for mode in self._attr_hvac_modes],
            "current_hvac_mode": self._hvac_mode.value,
            "hvac_action": self.hvac_action.value if self.hvac_action else "unknown",
            "current_temperature": self._current_temperature,
            "filtered_temperature": self._filtered_temperature,
            "control_temperature": control_temperature,
            "temperature_slope_per_hour": slope_per_hour,
            "target_temperature": self._target_temperature,
            "target_tolerance": self._target_tolerance,
            "current_humidity": self._current_humidity,
            "heater_states": heater_states,
            "central_heater_state": central_heater_state,
            "zone_heater_on": self._zone_heater_on,
            "cycle_mode_active": self._cycle_mode_active,
            "last_run_duration_seconds": round(self._last_run_duration, 1)
            if self._last_run_duration is not None
            else None,
            "current_run": (
                {
                    **self._current_run_record,
                    "on_time_seconds": round(self._current_run_record.get("on_time_seconds", 0.0), 1),
                }
                if self._current_run_record
                else None
            ),
            "run_history": list(self._run_history[-5:]),
            "total_run_count": self._total_run_count,
            "cycle_entries": self._cycle_entries,
            "auto_on_off_enabled": self._auto_on_off_enabled,
            "auto_on_temp": self._auto_on_temp,
            "auto_off_temp": self._auto_off_temp,
            "manual_override": self._manual_override,
            "central_heater_turn_on_delay": self._central_heater_turn_on_delay,
            "central_heater_turn_off_delay": self._central_heater_turn_off_delay,
            "control_window_seconds": round(self._control_window, 1),
            "control_window_configured_seconds": round(self._configured_control_window, 1),
            "control_window_adaptive_seconds": round(self._dynamic_control_window, 1)
            if self._dynamic_control_window is not None
            else None,
            "desired_on_time_seconds": round(self._window_desired_on, 1),
            "actual_on_time_seconds": round(self._window_on_time, 1),
            "integrator_state": round(self._integrator, 3),
            "adaptive_profile": adaptive_profile_state,
            "adaptive_history": adaptive_history,
            "adaptive_learning_samples": {
                "heating": self._adaptive_profile.heating_samples,
                "cooling": self._adaptive_profile.cooling_samples,
                "delay": self._adaptive_profile.delay_samples,
                "overshoot": self._adaptive_profile.overshoot_samples,
            },
            "window_detection_enabled": self._window_detection_enabled,
            "window_open_detected": self._open_window_detected,
            "window_slope_threshold": self._window_slope_threshold,
            "window_alert": window_alert,
            "last_window_event": last_window_event,
            "entity_available": True,
            "last_updated": last_updated,
        })

        _LOGGER.debug(
            "[%s] Sensor update: T=%.2f°C (filtered=%.2f°C, slope=%.3f°C/h, instant=%.3f°C/h), target=%.2f°C",
            self._entry_id,
            self._current_temperature if self._current_temperature is not None else float("nan"),
            self._filtered_temperature if self._filtered_temperature is not None else float("nan"),
            slope_per_hour,
            raw_slope_per_hour,
            self._target_temperature if self._target_temperature is not None else float("nan"),
        )

    def _advance_control_window(self, now_ts: float, dt: float) -> None:
        """Advance the duty window accounting for elapsed time."""
        if self._window_start_ts is None:
            self._window_start_ts = now_ts
            self._window_on_time = 0.0
            self._window_desired_on = self._compute_desired_on_duration()
            return

        if dt > 0 and self._zone_heater_on:
            self._window_on_time += dt
            if self._current_run_record is not None:
                on_time = self._current_run_record.get("on_time_seconds", 0.0) + dt
                self._current_run_record["on_time_seconds"] = on_time
                self._attr_extra_state_attributes["current_run"] = {
                    **self._current_run_record,
                    "on_time_seconds": round(on_time, 1),
                }

    def _rolling_window_reset(self, now_ts: float) -> None:
        """Reset duty window when elapsed time exceeds window length."""
        if self._window_start_ts is None:
            return
        elapsed = now_ts - self._window_start_ts
        if elapsed >= self._control_window:
            cycles = max(1, int(elapsed // self._control_window))
            self._window_start_ts += cycles * self._control_window
            self._window_on_time = 0.0
            self._window_desired_on = self._compute_desired_on_duration()

    async def _async_update_window_detection(self, now_ts: float) -> None:
        """Detect rapid cooling indicative of an open window."""
        if not self._window_detection_enabled:
            if self._open_window_detected:
                self._open_window_detected = False
                self._last_window_event_ts = now_ts
            self._attr_extra_state_attributes["window_alert"] = None
            return

        if self._filtered_temperature is None and self._current_temperature is None:
            return

        slope_base = self._raw_temperature_slope if self._raw_temperature_slope is not None else self._temperature_slope
        slope_per_hour = (slope_base or 0.0) * 3600.0
        threshold = self._window_slope_threshold
        release_threshold = threshold * 0.4

        if not self._open_window_detected:
            if slope_per_hour <= -threshold:
                self._open_window_detected = True
                self._last_window_event_ts = now_ts
                self._integrator = 0.0
                message = f"Open window detected (drop {abs(slope_per_hour):.2f}°C/h)"
                self._attr_extra_state_attributes["window_alert"] = message
                _LOGGER.warning(
                    "[%s] Rapid cooling detected (%.2f°C/h). Assuming open window and disabling heating.",
                    self._entry_id,
                    slope_per_hour,
                )
                if self._zone_heater_on:
                    await self._async_turn_heater_off()
                    self._zone_heater_on = False
        else:
            if slope_per_hour >= -release_threshold:
                self._open_window_detected = False
                self._last_window_event_ts = now_ts
                self._attr_extra_state_attributes["window_alert"] = None
                _LOGGER.info(
                    "[%s] Temperature drop resolved (%.2f°C/h). Resuming adaptive control.",
                    self._entry_id,
                    slope_per_hour,
                )
            else:
                self._attr_extra_state_attributes["window_alert"] = (
                    f"Open window detected (drop {abs(slope_per_hour):.2f}°C/h)"
                )

    def _compute_desired_on_duration(self) -> float:
        """Compute target heat time for the current duty window."""
        if self._target_temperature is None:
            return 0.0

        target = self._target_temperature
        filtered = self._filtered_temperature or self._current_temperature
        outdoor = self._current_outdoor_temp

        duty = 0.0
        if self._adaptive_profile.is_learned and outdoor is not None:
            delta = target - outdoor
            if delta > 0:
                duty = _clamp(
                    (self._adaptive_profile.loss_coefficient * delta)
                    / max(self._adaptive_profile.heater_gain, 1e-6),
                    0.0,
                    1.0,
                )

        if filtered is not None:
            error = target - filtered
            duty += CONTROL_KP * error
            duty += CONTROL_KI * self._integrator

        tolerance = self._target_tolerance if self._target_tolerance is not None else DEFAULT_TARGET_TOLERANCE
        tolerance = max(tolerance, COMFORT_EPSILON)
        overshoot = max(self._adaptive_profile.overshoot, 0.0)
        if overshoot > tolerance:
            damp_factor = 1.0 / (1.0 + overshoot / tolerance)
            duty *= damp_factor

        return _clamp(duty, 0.0, 1.0) * self._control_window

    def _update_temperature_metrics(
        self,
        raw_temp: float,
        measurement_ts: Optional[float],
        outdoor_temp: Optional[float],
        now_ts: float,
    ) -> None:
        """Update filtered temperature, slope, and adaptive learning."""
        sample_ts = measurement_ts if measurement_ts is not None else now_ts

        prev_ts = self._last_measurement_ts or sample_ts
        dt = max(0.0, sample_ts - prev_ts)
        if dt <= 0.0:
            dt = max(0.0, now_ts - prev_ts)
        if dt <= 0.0:
            dt = 1e-6
        self._last_measurement_ts = sample_ts

        prev_temp = self._last_measurement_temp
        temp_delta = 0.0
        meaningful_change = False
        if prev_temp is None:
            slope = 0.0
        else:
            temp_delta = raw_temp - prev_temp
            meaningful_change = abs(temp_delta) >= SLOPE_CHANGE_EPSILON
            slope = temp_delta / dt if dt > 0 else 0.0

        self._last_measurement_temp = raw_temp
        self._filtered_temperature = raw_temp
        self._raw_temperature_slope = slope
        self._temperature_slope = slope

        if prev_temp is None:
            self._display_temperature_slope = slope
        elif meaningful_change and dt > 0:
            self._display_temperature_slope = slope

        if meaningful_change:
            self._update_adaptive_learning(sample_ts, slope, outdoor_temp, self._zone_heater_on, dt)

    def _update_adaptive_learning(
        self,
        now_ts: float,
        slope: float,
        outdoor_temp: Optional[float],
        heater_on: bool,
        dt: float,
    ) -> None:
        """Continuously update adaptive model parameters."""
        if dt <= 0:
            return

        if heater_on and slope > MIN_SLOPE_THRESHOLD:
            alpha = self._compute_learning_alpha(self._adaptive_profile.heating_samples)
            heating_rate = self._adaptive_profile.heating_rate or slope
            new_heating = (1 - alpha) * heating_rate + alpha * slope
            new_heating = max(new_heating, MIN_SLOPE_THRESHOLD)
            changed_heating = self._set_profile_field("heating_rate", new_heating)
            self._record_learning_event(
                "heating_rate",
                self._adaptive_profile.heating_rate,
                {
                    "sample_slope": round(slope, 5),
                    "alpha": round(alpha, 4),
                    "sample_dt": round(dt, 2),
                    "changed": changed_heating,
                },
                sample_field="heating_samples",
            )

            if (
                outdoor_temp is not None
                and self._filtered_temperature is not None
                and self._adaptive_profile.loss_coefficient > 0
            ):
                temp_delta = self._filtered_temperature - outdoor_temp
                a_est = slope + self._adaptive_profile.loss_coefficient * temp_delta
                if a_est > 0:
                    new_gain = (1 - alpha) * (self._adaptive_profile.heater_gain or a_est) + alpha * a_est
                    new_gain = max(new_gain, MIN_SLOPE_THRESHOLD)
                    changed_gain = self._set_profile_field("heater_gain", new_gain)
                    self._record_learning_event(
                        "heater_gain",
                        self._adaptive_profile.heater_gain,
                        {
                            "sample_gain": round(a_est, 5),
                            "alpha": round(alpha, 4),
                            "temp_delta": round(temp_delta, 3),
                            "changed": changed_gain,
                        },
                    )

        elif not heater_on and slope < -MIN_SLOPE_THRESHOLD and not self._open_window_detected:
            alpha = self._compute_learning_alpha(self._adaptive_profile.cooling_samples)
            cooling_sample = -slope
            new_cooling = (1 - alpha) * (self._adaptive_profile.cooling_rate or cooling_sample) + alpha * cooling_sample
            new_cooling = max(new_cooling, MIN_SLOPE_THRESHOLD)
            changed_cooling = self._set_profile_field("cooling_rate", new_cooling)
            self._record_learning_event(
                "cooling_rate",
                self._adaptive_profile.cooling_rate,
                {
                    "sample_slope": round(slope, 5),
                    "alpha": round(alpha, 4),
                    "sample_dt": round(dt, 2),
                    "changed": changed_cooling,
                },
                sample_field="cooling_samples",
            )

            if (
                outdoor_temp is not None
                and self._filtered_temperature is not None
                and abs(self._filtered_temperature - outdoor_temp) > 0.05
            ):
                denominator = self._filtered_temperature - outdoor_temp
                if denominator != 0:
                    b_est = -slope / denominator
                    if 0 < b_est < 0.01:
                        new_loss = (1 - alpha) * (self._adaptive_profile.loss_coefficient or b_est) + alpha * b_est
                        new_loss = max(new_loss, MIN_SLOPE_THRESHOLD / 10.0)
                        changed_loss = self._set_profile_field("loss_coefficient", new_loss)
                        self._record_learning_event(
                            "loss_coefficient",
                            self._adaptive_profile.loss_coefficient,
                            {
                                "sample_loss": round(b_est, 6),
                                "alpha": round(alpha, 4),
                                "temp_delta": round(denominator, 3),
                                "changed": changed_loss,
                            },
                        )

        # Delay estimation
        if heater_on and self._awaiting_delay_timestamp is not None and slope > MIN_SLOPE_THRESHOLD:
            delay = max(0.0, now_ts - self._awaiting_delay_timestamp)
            if delay > 0:
                alpha_delay = self._compute_learning_alpha(self._adaptive_profile.delay_samples)
                current_delay = self._adaptive_profile.delay_seconds or DEFAULT_DELAY_SECONDS
                smoothed = (1 - alpha_delay) * current_delay + alpha_delay * delay
                new_delay = _clamp(smoothed, 20.0, 900.0)
                changed_delay = self._set_profile_field("delay_seconds", new_delay)
                self._record_learning_event(
                    "delay_seconds",
                    self._adaptive_profile.delay_seconds,
                    {
                        "sample_delay": round(delay, 2),
                        "alpha": round(alpha_delay, 4),
                        "changed": changed_delay,
                    },
                    sample_field="delay_samples",
                )
            self._awaiting_delay_timestamp = None

        self._maybe_update_control_window()

        # Overshoot tracking after heater turned off
        if self._awaiting_peak:
            peak_candidate = None
            for temp in (self._current_temperature, self._filtered_temperature):
                if temp is None:
                    continue
                peak_candidate = temp if peak_candidate is None else max(peak_candidate, temp)
            if peak_candidate is not None:
                if self._peak_max_temp is None or peak_candidate > self._peak_max_temp:
                    self._peak_max_temp = peak_candidate
            track_start = self._peak_track_start_ts or self._last_command_timestamp or now_ts
            track_elapsed = max(0.0, now_ts - track_start)
            target = self._peak_target or self._target_temperature
            tolerance = self._target_tolerance if self._target_tolerance is not None else DEFAULT_TARGET_TOLERANCE
            tolerance = max(tolerance, COMFORT_EPSILON)
            current_temp = self._filtered_temperature if self._filtered_temperature is not None else self._current_temperature

            should_finalize = False
            if track_elapsed >= OVERSHOOT_MAX_TRACK_SECONDS:
                should_finalize = True
            elif track_elapsed >= OVERSHOOT_MIN_TRACK_SECONDS:
                if slope < -MIN_SLOPE_THRESHOLD:
                    should_finalize = True
                elif (
                    target is not None
                    and current_temp is not None
                    and current_temp <= target + tolerance
                ):
                    should_finalize = True

            if should_finalize:
                self._finalize_peak_tracking()

    def _finalize_peak_tracking(self) -> None:
        """Finalize overshoot estimation after heater off."""
        if not self._awaiting_peak:
            return

        peak_temp = self._peak_max_temp
        target = self._peak_target or self._target_temperature

        if peak_temp is not None and target is not None:
            overshoot = peak_temp - target
            beta = self._compute_learning_alpha(
                self._adaptive_profile.overshoot_samples,
                min_alpha=0.05,
                max_alpha=0.4,
            )
            target_sample = max(overshoot, 0.0)
            new_overshoot = (1 - beta) * self._adaptive_profile.overshoot + beta * target_sample
            new_overshoot = max(new_overshoot, 0.0)
            run_duration = self._peak_run_duration
            run_duration_rounded = round(run_duration, 1) if run_duration is not None else None
            changed_overshoot = self._set_profile_field("overshoot", new_overshoot)
            self._record_learning_event(
                "overshoot",
                self._adaptive_profile.overshoot,
                {
                    "sample_overshoot": round(overshoot, 4),
                    "alpha": round(beta, 4),
                    "run_duration": run_duration_rounded,
                    "changed": changed_overshoot,
                },
                sample_field="overshoot_samples",
            )
            self._attr_extra_state_attributes["last_overshoot_capture"] = {
                "peak_temp": round(peak_temp, 3),
                "target": round(target, 3),
                "overshoot": round(target_sample, 3),
                "run_duration": run_duration_rounded,
            }
            if self._pending_overshoot_record is not None:
                self._pending_overshoot_record["overshoot"] = round(target_sample, 3)
                self._pending_overshoot_record["peak_temperature"] = round(peak_temp, 3)
                self._pending_overshoot_record["overshoot_captured_at"] = dt_util.utcnow().isoformat()
                self._attr_extra_state_attributes["run_history"] = list(self._run_history[-5:])
                self._pending_overshoot_record = None
        else:
            self._attr_extra_state_attributes["last_overshoot_capture"] = None
            self._pending_overshoot_record = None

        self._awaiting_peak = False
        self._peak_target = None
        self._peak_max_temp = None
        self._peak_run_duration = None
        self._peak_track_start_ts = None

    def _apply_learning_retention(self) -> None:
        """Prune old adaptive learning events and sync sample counters."""
        history = self._adaptive_profile.history
        if not history:
            return

        cutoff_dt = dt_util.utcnow() - LEARNING_RETENTION
        cutoff_ts = cutoff_dt.timestamp()
        pruned_history: list[Dict[str, Any]] = []
        for event in history:
            ts_str = event.get("timestamp")
            event_ts = None
            if isinstance(ts_str, str):
                parsed = dt_util.parse_datetime(ts_str)
                if parsed is not None:
                    event_ts = parsed.timestamp()
            if event_ts is None:
                # Keep malformed entries but don't rely on them for counts
                pruned_history.append(event)
                continue
            if event_ts >= cutoff_ts:
                pruned_history.append(event)

        if len(pruned_history) > MAX_PROFILE_HISTORY:
            pruned_history = pruned_history[-MAX_PROFILE_HISTORY:]

        if pruned_history is not history:
            history[:] = pruned_history

        heating = sum(1 for e in history if e.get("kind") == "heating_rate")
        cooling = sum(1 for e in history if e.get("kind") == "cooling_rate")
        delay = sum(1 for e in history if e.get("kind") == "delay_seconds")
        overshoot = sum(1 for e in history if e.get("kind") == "overshoot")

        self._adaptive_profile.heating_samples = heating
        self._adaptive_profile.cooling_samples = cooling
        self._adaptive_profile.delay_samples = delay
        self._adaptive_profile.overshoot_samples = overshoot

    def _record_run_start(self, start_ts: float) -> None:
        """Track the beginning of a heating run for diagnostics."""
        if self._current_run_record is not None:
            return
        self._run_sequence += 1
        self._total_run_count += 1
        record: Dict[str, Any] = {
            "run_id": self._run_sequence,
            "started_at": dt_util.utc_from_timestamp(start_ts).isoformat(),
            "target": round(self._target_temperature, 3) if self._target_temperature is not None else None,
            "start_temperature": round(self._current_temperature, 3)
            if self._current_temperature is not None
            else None,
            "cycle_mode_used": bool(self._cycle_mode_active),
            "on_time_seconds": 0.0,
            "duration_seconds": None,
            "overshoot": None,
        }
        self._current_run_record = record
        self._attr_extra_state_attributes["total_run_count"] = self._total_run_count
        self._attr_extra_state_attributes["current_run"] = {
            **record,
            "on_time_seconds": round(record.get("on_time_seconds", 0.0), 1),
        }

    def _finalize_current_run(self, run_duration: Optional[float], end_ts: float) -> None:
        """Finalize run metrics once the heater turns off."""
        if self._current_run_record is None:
            return
        record = dict(self._current_run_record)
        record["ended_at"] = dt_util.utc_from_timestamp(end_ts).isoformat()
        record["duration_seconds"] = round(run_duration, 1) if run_duration is not None else None
        record["on_time_seconds"] = round(record.get("on_time_seconds", 0.0), 1)
        record["end_temperature"] = round(self._current_temperature, 3) if self._current_temperature is not None else None
        record["integrator_end"] = round(self._integrator, 3)
        self._run_history.append(record)
        if len(self._run_history) > RUN_LOG_LENGTH:
            del self._run_history[: len(self._run_history) - RUN_LOG_LENGTH]
        self._attr_extra_state_attributes["run_history"] = list(self._run_history[-5:])
        self._pending_overshoot_record = record
        self._current_run_record = None
        self._attr_extra_state_attributes["current_run"] = None

    def _set_profile_field(self, field: str, value: float) -> bool:
        """Set adaptive profile field and schedule persistence if changed."""
        if math.isnan(value) or math.isinf(value):
            return False
        current = getattr(self._adaptive_profile, field)
        if abs(value - current) < 1e-6:
            return False
        setattr(self._adaptive_profile, field, value)
        self._adaptive_profile.updated_at = dt_util.utcnow().timestamp()
        self._mark_profile_dirty()
        return True

    def _compute_learning_alpha(self, samples: int, *, min_alpha: float = 0.05, max_alpha: float = 0.3) -> float:
        """Return smoothing coefficient based on how many samples we have."""
        samples = max(0, samples)
        dynamic = 1.0 / (samples + 1)
        return max(min_alpha, min(max_alpha, dynamic))

    def _record_learning_event(
        self,
        kind: str,
        value: float,
        extra: Optional[Dict[str, Any]] = None,
        sample_field: Optional[str] = None,
    ) -> None:
        """Append a learning event to history and persist counters."""
        if sample_field:
            current_samples = getattr(self._adaptive_profile, sample_field, 0) or 0
            setattr(self._adaptive_profile, sample_field, current_samples + 1)

        history = self._adaptive_profile.history
        if not isinstance(history, list):
            history = []
            self._adaptive_profile.history = history

        event: Dict[str, Any] = {
            "timestamp": dt_util.utcnow().isoformat(),
            "kind": kind,
            "value": round(float(value), 5),
        }
        if extra:
            event.update(extra)

        history.append(event)
        if len(history) > MAX_PROFILE_HISTORY:
            del history[: len(history) - MAX_PROFILE_HISTORY]

        self._apply_learning_retention()
        self._mark_profile_dirty()

    def _maybe_update_control_window(self) -> None:
        """Adapt control window length based on learned dynamics."""
        if self._adaptive_profile.heating_samples < 3 or self._adaptive_profile.cooling_samples < 3:
            return

        heating_rate = max(self._adaptive_profile.heating_rate, MIN_SLOPE_THRESHOLD)
        cooling_rate = max(self._adaptive_profile.cooling_rate, MIN_SLOPE_THRESHOLD)
        tolerance_setting = self._target_tolerance if self._target_tolerance is not None else DEFAULT_TARGET_TOLERANCE
        tolerance = max(tolerance_setting, COMFORT_EPSILON)
        delay = max(self._adaptive_profile.delay_seconds, 20.0)

        heating_time = tolerance / heating_rate
        cooling_time = tolerance / cooling_rate

        target_window = delay + heating_time + cooling_time
        overshoot = max(self._adaptive_profile.overshoot, 0.0)
        if overshoot > tolerance:
            target_window /= 1.0 + overshoot / tolerance
        min_window = max(60.0, self._min_on_time + self._min_off_time, self._configured_control_window * 0.5)
        max_window = min(900.0, self._configured_control_window * 1.5)
        target_window = _clamp(target_window, min_window, max_window)

        blend = 0.2
        new_window = (1 - blend) * self._control_window + blend * target_window
        if abs(new_window - self._control_window) > 0.5:
            self._control_window = new_window
            self._dynamic_control_window = new_window
            self._window_desired_on = self._compute_desired_on_duration()

    def _mark_profile_dirty(self) -> None:
        """Schedule persistence of the adaptive profile."""
        if not self._profile_store:
            return
        self._profile_dirty = True
        if self._profile_save_unsub is None:
            self._profile_save_unsub = async_call_later(self.hass, 5.0, self._async_persist_profile)

    async def _async_persist_profile(self, _now: Optional[datetime] = None) -> None:
        """Persist adaptive profile to storage."""
        self._profile_save_unsub = None
        if not self._profile_store or not self._profile_dirty:
            return

        cache = self._profile_cache_ref
        if cache is None:
            cache = {}
            self._profile_cache_ref = cache

        cache[self._entry_id] = self._adaptive_profile.to_dict()
        await self._profile_store.async_save(cache)
        self._profile_dirty = False

    async def _async_load_profile(self) -> None:
        """Load adaptive profile from storage."""
        if not self._profile_store:
            self._adaptive_profile = AdaptiveProfile()
            return

        cache = self._profile_cache_ref
        if cache is None or not cache:
            stored = await self._profile_store.async_load()
            if stored is None:
                stored = {}
            cache = stored
            self._profile_cache_ref = cache

        data = cache.get(self._entry_id)
        self._adaptive_profile = AdaptiveProfile.from_dict(data or {})
        self._adaptive_profile.delay_seconds = _clamp(
            self._adaptive_profile.delay_seconds or DEFAULT_DELAY_SECONDS,
            20.0,
            900.0,
        )

    async def _async_control_tick(self, _now: datetime) -> None:
        """Periodic control tick to keep regulation active."""
        self._rolling_window_reset(dt_util.as_timestamp(_now))
        self.async_schedule_update_ha_state(True)

    def _evaluate_should_heat(self, now_ts: float) -> bool:
        """Decide whether heating should be active."""
        if self._open_window_detected:
            return False

        if self._target_temperature is None:
            return False

        raw_temp = self._current_temperature
        filtered_temp = self._filtered_temperature

        if filtered_temp is None and raw_temp is None:
            return False

        if filtered_temp is None:
            filtered_temp = raw_temp

        if raw_temp is None:
            comparison_temp = filtered_temp
        else:
            comparison_temp = min(filtered_temp, raw_temp)

        if comparison_temp is None:
            return False

        target = self._target_temperature
        tolerance = self._target_tolerance

        # Basic hysteresis guard outside comfort band to guarantee heat starts early enough
        if comparison_temp <= target - tolerance:
            if (
                not self._zone_heater_on
                and self._last_command_timestamp is not None
                and now_ts - self._last_command_timestamp < self._min_off_time
            ):
                return False
            if self._cycle_mode_active:
                self._cycle_mode_active = False
            self._attr_extra_state_attributes["cycle_mode_active"] = False
            return True

        if comparison_temp >= target + tolerance:
            if (
                self._zone_heater_on
                and self._last_command_timestamp is not None
                and now_ts - self._last_command_timestamp < self._min_on_time
            ):
                return True
            if self._cycle_mode_active:
                self._cycle_mode_active = False
            self._attr_extra_state_attributes["cycle_mode_active"] = False
            return False

        delta_below_target = target - comparison_temp
        cycle_active = self._cycle_mode_active

        if cycle_active:
            if delta_below_target < 0 or delta_below_target >= CYCLE_EXIT_DELTA:
                cycle_active = False
        else:
            if 0 <= delta_below_target <= CYCLE_ENTRY_DELTA:
                cycle_active = True

        if cycle_active != self._cycle_mode_active:
            if cycle_active:
                _LOGGER.debug(
                    "[%s] Entering duty cycling (effective temp %.2f°C below target)",
                    self._entry_id,
                    delta_below_target,
                )
                self._cycle_entries += 1
            else:
                _LOGGER.debug(
                    "[%s] Exiting duty cycling (effective temp %.2f°C below target)",
                    self._entry_id,
                    delta_below_target,
                )
            self._cycle_mode_active = cycle_active

        self._attr_extra_state_attributes["cycle_mode_active"] = self._cycle_mode_active
        self._attr_extra_state_attributes["cycle_entries"] = self._cycle_entries
        if self._current_run_record is not None and self._cycle_mode_active:
            self._current_run_record["cycle_mode_used"] = True

        delay = _clamp(self._adaptive_profile.delay_seconds, 20.0, 900.0)
        overshoot = max(self._adaptive_profile.overshoot, 0.0)
        heating_rate = max(self._adaptive_profile.heating_rate, MIN_SLOPE_THRESHOLD)
        cooling_rate = max(self._adaptive_profile.cooling_rate, MIN_SLOPE_THRESHOLD)

        in_min_on = (
            self._zone_heater_on
            and self._last_command_timestamp is not None
            and now_ts - self._last_command_timestamp < self._min_on_time
        )
        in_min_off = (
            not self._zone_heater_on
            and self._last_command_timestamp is not None
            and now_ts - self._last_command_timestamp < self._min_off_time
        )

        effective_filtered = filtered_temp if filtered_temp is not None else comparison_temp

        if self._zone_heater_on:
            predicted_peak = effective_filtered + heating_rate * delay + overshoot
            if predicted_peak >= target + tolerance and not in_min_on:
                return False
            if (
                self._window_desired_on > 0
                and self._window_on_time >= self._window_desired_on
                and (
                    (not self._cycle_mode_active and effective_filtered >= target)
                    or (self._cycle_mode_active and comparison_temp >= target - CYCLE_ENTRY_DELTA)
                )
                and not in_min_on
            ):
                return False
            return True

        if in_min_off:
            return False

        predicted_floor = comparison_temp - cooling_rate * delay
        floor_threshold = target - (CYCLE_ENTRY_DELTA if self._cycle_mode_active else tolerance)
        if predicted_floor <= floor_threshold:
            return True

        if (
            self._window_desired_on > 0
            and self._window_on_time < self._window_desired_on
            and comparison_temp <= target - COMFORT_EPSILON
        ):
            return True

        return False

    async def _async_handle_auto_onoff(
        self,
        outdoor_temp: Optional[float],
        backup_outdoor_temp: Optional[float],
    ) -> None:
        """Handle automatic on/off based on outdoor temperature."""
        temp = outdoor_temp if outdoor_temp is not None else backup_outdoor_temp
        if temp is None:
            return

        # Only process if temperature changed significantly (0.5°C hysteresis)
        if self._last_outdoor_temp is not None and abs(temp - self._last_outdoor_temp) < 0.5:
            return

        self._last_outdoor_temp = temp
        self._current_outdoor_temp = temp

        _LOGGER.debug(
            "[%s] Auto on/off check - Outdoor temp: %.2f°C, Current mode: %s",
            self._entry_id,
            temp,
            self._hvac_mode,
        )

        # Auto turn on when outdoor temp drops below threshold
        if temp < self._auto_on_temp and self._hvac_mode == HVACMode.OFF:
            _LOGGER.info(
                "[%s] Auto turning ON - Outdoor temp %.2f°C < %.2f°C",
                self._entry_id,
                temp,
                self._auto_on_temp,
            )
            self._hvac_mode = HVACMode.HEAT
            await self._async_control_heating(dt_util.utcnow().timestamp())

        # Auto turn off when outdoor temp rises above threshold
        elif temp > self._auto_off_temp and self._hvac_mode == HVACMode.HEAT:
            _LOGGER.info(
                "[%s] Auto turning OFF - Outdoor temp %.2f°C > %.2f°C",
                self._entry_id,
                temp,
                self._auto_off_temp,
            )
            await self._async_turn_heater_off()
            self._hvac_mode = HVACMode.OFF

    async def _async_control_heating(self, now_ts: float) -> None:
        """Control heating based on adaptive model."""
        # Don't control if HVAC mode is OFF
        if self._hvac_mode != HVACMode.HEAT:
            _LOGGER.debug("[%s] Control heating skipped - HVAC mode is %s", self._entry_id, self._hvac_mode)
            return

        should_heat = self._evaluate_should_heat(now_ts)

        if should_heat and not self._zone_heater_on:
            if self._target_temperature is not None:
                tolerance = self._target_tolerance if self._target_tolerance is not None else DEFAULT_TARGET_TOLERANCE
                tolerance = max(tolerance, COMFORT_EPSILON)
                candidate_temps = [
                    temp for temp in (self._current_temperature, self._filtered_temperature) if temp is not None
                ]
                if candidate_temps:
                    max_temp = max(candidate_temps)
                    if max_temp >= self._target_temperature + tolerance:
                        _LOGGER.debug(
                            "[%s] Heat request suppressed - measured %.2f°C exceeds target+tolerance %.2f°C",
                            self._entry_id,
                            max_temp,
                            self._target_temperature + tolerance,
                        )
                        return
            _LOGGER.info(
                "[%s] 🔥 Control: Turning heater ON (current=%.2f°C, target=%.2f°C)",
                self._entry_id,
                self._current_temperature if self._current_temperature is not None else float("nan"),
                self._target_temperature if self._target_temperature is not None else float("nan"),
            )
            await self._async_turn_heater_on()
            self._zone_heater_on = True

        elif not should_heat and self._zone_heater_on:
            _LOGGER.info(
                "[%s] ❄️  Control: Turning heater OFF (current=%.2f°C, target=%.2f°C)",
                self._entry_id,
                self._current_temperature if self._current_temperature is not None else float("nan"),
                self._target_temperature if self._target_temperature is not None else float("nan"),
            )
            await self._async_turn_heater_off()
            self._zone_heater_on = False

    def _resolve_domain_and_service(self, entity_id: str, turn_on: bool) -> tuple[str, str]:
        """Return the target domain and service for an entity action."""
        if not entity_id or "." not in entity_id:
            return "switch", "turn_on" if turn_on else "turn_off"

        domain = entity_id.split(".", 1)[0]

        if domain == "valve":
            service = "open_valve" if turn_on else "close_valve"
        elif domain in {"switch", "input_boolean", "climate"}:
            service = "turn_on" if turn_on else "turn_off"
        else:
            # Default to generic turn_on/turn_off
            service = "turn_on" if turn_on else "turn_off"

        return domain, service

    async def _async_turn_on_entity(self, entity_id: str, entity_name: str) -> None:
        """Turn on an entity (switch, climate, or valve)."""
        if not entity_id:
            return

        domain, service = self._resolve_domain_and_service(entity_id, True)
        _LOGGER.info(
            "[%s] 🟢 Turning ON %s: %s (domain: %s, service: %s)",
            self._entry_id,
            entity_name,
            entity_id,
            domain,
            service,
        )

        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
            _LOGGER.info("[%s] ✅ Successfully turned ON %s: %s", self._entry_id, entity_name, entity_id)
        except Exception as e:
            _LOGGER.error("[%s] ❌ Failed to turn ON %s: %s - Error: %s", self._entry_id, entity_name, entity_id, e)

    async def _async_turn_off_entity(self, entity_id: str, entity_name: str) -> None:
        """Turn off an entity (switch, climate, or valve)."""
        if not entity_id:
            return
            
        domain, service = self._resolve_domain_and_service(entity_id, False)
        _LOGGER.info(
            "[%s] 🔴 Turning OFF %s: %s (domain: %s, service: %s)",
            self._entry_id,
            entity_name,
            entity_id,
            domain,
            service,
        )

        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
            _LOGGER.info("[%s] ✅ Successfully turned OFF %s: %s", self._entry_id, entity_name, entity_id)
        except Exception as e:
            _LOGGER.error("[%s] ❌ Failed to turn OFF %s: %s - Error: %s", self._entry_id, entity_name, entity_id, e)

    async def _async_check_other_zones_need_heat(self) -> bool:
        """Check if other zones using the same central heater need heat."""
        if not self._central_heater_entity_id:
            return False
            
        # Get all adaptive thermostat entities from domain data
        domain_data = self.hass.data.get(DOMAIN, {})
        all_entities = domain_data.get("entities", {})
        
        # Check entities with same central heater
        for entity_id, entity in all_entities.items():
            if (entity != self and 
                hasattr(entity, '_central_heater_entity_id') and
                entity._central_heater_entity_id == self._central_heater_entity_id and
                hasattr(entity, '_zone_heater_on') and
                entity._zone_heater_on and
                hasattr(entity, '_hvac_mode') and
                entity._hvac_mode == HVACMode.HEAT):
                _LOGGER.debug("[%s] Other zone %s still needs heat (heater_on=%s, hvac_mode=%s)", 
                            self._entry_id, entity_id, entity._zone_heater_on, entity._hvac_mode)
                return True
                
        _LOGGER.info("[%s] No other zones need heat from central heater - all zones OFF", self._entry_id)
        return False

    async def _async_turn_heater_on(self) -> None:
        """Turn on zone heaters and coordinate with central heater."""
        _LOGGER.info("[%s] 📍 Starting heater turn-on sequence for %d valves",
                    self._entry_id, len(self._heater_entity_ids))

        # Cancel any pending delayed valve close tasks
        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        # Turn on all zone heaters (valves)
        for heater_id in self._heater_entity_ids:
            await self._async_turn_on_entity(heater_id, "zone heater/valve")

        # Coordinate with central heater if configured
        if self._central_heater_entity_id:
            await self._async_coordinate_central_heater_on()
        else:
            _LOGGER.debug("[%s] No central heater configured - only zone valves controlled", self._entry_id)

        now_ts = dt_util.utcnow().timestamp()
        self._awaiting_delay_timestamp = now_ts
        self._awaiting_peak = False
        self._peak_max_temp = None
        self._peak_target = None
        self._peak_run_duration = None
        self._last_run_duration = None
        self._last_command_timestamp = now_ts
        self._last_heater_command = True
        self._record_run_start(now_ts)

    async def _async_close_zone_valves(self) -> None:
        """Helper to close all configured zone valves."""
        for heater_id in self._heater_entity_ids:
            await self._async_turn_off_entity(heater_id, "zone heater/valve")

    async def _async_delayed_close_zone_valves(self, delay: float) -> None:
        """Close valves after a delay, unless cancelled by a new heat request."""
        try:
            if delay > 0:
                _LOGGER.info(
                    "[%s] Waiting %s seconds before closing zone valves to protect the pump",
                    self._entry_id,
                    delay,
                )
                await asyncio.sleep(delay)

            if not self._zone_heater_on:
                await self._async_close_zone_valves()
        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Delayed valve close task cancelled", self._entry_id)
        finally:
            self._delayed_valve_off_task = None

    async def _async_turn_heater_off(self) -> None:
        """Turn off zone heaters and coordinate with central heater."""
        _LOGGER.info("[%s] 📍 Starting heater turn-off sequence for %d valves",
                    self._entry_id, len(self._heater_entity_ids))

        # Cancel any existing delayed valve close task
        if self._delayed_valve_off_task:
            self._delayed_valve_off_task.cancel()
            self._delayed_valve_off_task = None

        # Determine if any other zones sharing the central heater still need heat
        other_zones_need_heat = False
        if self._central_heater_entity_id:
            other_zones_need_heat = await self._async_check_other_zones_need_heat()

        if other_zones_need_heat:
            _LOGGER.info(
                "[%s] Other zones still need heat - closing this zone's valves immediately",
                self._entry_id,
            )
            await self._async_close_zone_valves()
        else:
            delay = max(0, float(self._central_heater_turn_off_delay or 0))
            if delay > 0:
                self._delayed_valve_off_task = asyncio.create_task(
                    self._async_delayed_close_zone_valves(delay)
                )
            else:
                await self._async_close_zone_valves()

        # Coordinate with central heater if configured
        if self._central_heater_entity_id:
            await self._async_coordinate_central_heater_off(other_zones_need_heat)
        else:
            _LOGGER.debug("[%s] No central heater configured - only zone valves controlled", self._entry_id)

        now_ts = dt_util.utcnow().timestamp()
        if self._last_heater_command and self._last_command_timestamp is not None:
            run_duration = max(0.0, now_ts - self._last_command_timestamp)
        else:
            run_duration = None
        self._last_run_duration = run_duration
        self._awaiting_delay_timestamp = None
        self._awaiting_peak = True
        self._peak_target = self._target_temperature
        peak_candidate = [temp for temp in (self._current_temperature, self._filtered_temperature) if temp is not None]
        self._peak_max_temp = max(peak_candidate) if peak_candidate else None
        self._peak_run_duration = run_duration
        self._last_command_timestamp = now_ts
        self._last_heater_command = False
        self._integrator = min(self._integrator, 0.0)
        self._finalize_current_run(run_duration, now_ts)
        self._peak_track_start_ts = now_ts

    async def _async_coordinate_central_heater_on(self) -> None:
        """Coordinate turning on central heater after zone valves."""
        if not self._central_heater_entity_id:
            return
            
        # Cancel any existing task
        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None
        
        # Create task to turn on central heater after delay
        self._central_heater_task = asyncio.create_task(
            self._async_delayed_central_heater_on()
        )

    async def _async_coordinate_central_heater_off(self, other_zones_need_heat: bool) -> None:
        """Coordinate turning off central heater considering other zones."""
        if not self._central_heater_entity_id:
            return

        # Cancel any pending on/off coordination task
        if self._central_heater_task:
            self._central_heater_task.cancel()
            self._central_heater_task = None

        if other_zones_need_heat:
            _LOGGER.info(
                "[%s] Keeping central heater ON because another zone still requires heat",
                self._entry_id,
            )
            return

        await self._async_turn_off_entity(self._central_heater_entity_id, "central heater")
        _LOGGER.info(
            "[%s] Central heater turned OFF immediately - no other zones require heat",
            self._entry_id,
        )

    async def _async_delayed_central_heater_on(self) -> None:
        """Turn on central heater after configured delay."""
        try:
            _LOGGER.debug("[%s] Waiting %s seconds before turning on central heater", 
                         self._entry_id, self._central_heater_turn_on_delay)
            await asyncio.sleep(self._central_heater_turn_on_delay)
            
            # Check if we still need to turn on (in case zone turned off during delay)
            if self._zone_heater_on:
                await self._async_turn_on_entity(self._central_heater_entity_id, "central heater")
                _LOGGER.info("[%s] Central heater turned on after %s second delay", 
                            self._entry_id, self._central_heater_turn_on_delay)
        except asyncio.CancelledError:
            _LOGGER.debug("[%s] Central heater turn-on task cancelled", self._entry_id)
        finally:
            self._central_heater_task = None

    def reset_manual_override(self) -> None:
        """Reset manual override to allow auto on/off to resume.
        
        This service allows you to clear the manual override flag that is set
        when you manually control the thermostat via the UI. Once cleared,
        the auto_on_off feature will resume automatic control based on outdoor temperature.
        """
        _LOGGER.info("[%s] Manual override reset by service call - auto on/off will resume", self._entry_id)
        self._manual_override = False
        self._attr_extra_state_attributes["manual_override"] = False
        self.async_write_ha_state()

    async def async_reset_adaptive_profile(self) -> None:
        """Reset the learned adaptive profile and persist the cleared state."""
        _LOGGER.warning("[%s] Adaptive profile reset requested - learning will restart", self._entry_id)
        self._adaptive_profile = AdaptiveProfile()
        self._adaptive_profile.updated_at = dt_util.utcnow().timestamp()
        self._integrator = 0.0
        self._window_desired_on = 0.0
        self._window_on_time = 0.0
        self._cycle_mode_active = False
        self._attr_extra_state_attributes["cycle_mode_active"] = False
        adaptive_profile_state = self._summarize_adaptive_profile()
        self._attr_extra_state_attributes["adaptive_profile"] = adaptive_profile_state
        self._attr_extra_state_attributes["adaptive_history"] = list(adaptive_profile_state.get("history", []))
        self._attr_extra_state_attributes["adaptive_learning_samples"] = {
            "heating": 0,
            "cooling": 0,
            "delay": 0,
            "overshoot": 0,
        }
        if self._profile_save_unsub:
            self._profile_save_unsub()
            self._profile_save_unsub = None
        self._mark_profile_dirty()
        await self._async_persist_profile()
        self.async_write_ha_state()
