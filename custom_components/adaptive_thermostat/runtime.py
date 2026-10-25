"""Per-zone runtime: intent, a single serialized control step, reconciler.

Architecture
------------
* **Intent** - service handlers (``set_hvac_mode``, ``set_temperature``,
  ``set_preset_mode``) only change the desired state (mode, target, preset),
  write the entity state immediately and request a control step.  They never
  wait for actuators.
* **One control step per zone** - :meth:`ZoneRuntime._run_step` is a plain
  synchronous function scheduled with ``call_soon`` and coalesced: it can
  never run concurrently with itself, so no update is lost or duplicated.
  Sensor callbacks feed the engine immediately (in event order) and request
  a step.  A single timer per zone wakes it for the next deadline (planned
  end of a burst, command confirmation, tick).
* **Reconciler** - each step compares the *desired* actuator state (burst
  requested, or pump-overrun hold granted by the boiler coordinator) with the
  *actual* entity states and issues open/close commands as tracked
  background tasks.  Travel is confirmed through state changes (which request
  a step) with a deadline instead of polling.  It is idempotent: re-running
  it with nothing changed does nothing.
* **Drift** - a valve that is open while it should be closed (outside the
  intended overrun hold) is closed and flagged; an unavailable actuator is a
  fault after a grace period and the desired state is re-applied as soon as
  it reports again.  On start-up the real actuator state is reconciled the
  same way (anything open that should not be is closed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util

from .actuators import (
    CLOSED,
    CLOSING,
    OPEN,
    OPENING,
    UNAVAILABLE,
    CommandTracker,
    async_command,
    classify,
    is_physically_open,
)
from .const import (
    ACTUATOR_MAX_ATTEMPTS,
    ACTUATOR_RETRY_BACKOFF,
    ACTUATOR_UNAVAILABLE_GRACE,
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
    CONTROL_TICK,
    DEFAULT_AUTO_OFF_TEMP,
    DEFAULT_AUTO_ON_TEMP,
    DEFAULT_AWAY_PRESET,
    DEFAULT_CENTRAL_HEATER_MIN_OFF,
    DEFAULT_CENTRAL_HEATER_MIN_ON,
    DEFAULT_HOME_PRESET,
    DEFAULT_MAX_BURST,
    DEFAULT_MIN_BURST,
    DEFAULT_MODEL_CONTROL,
    DEFAULT_NAME,
    DEFAULT_SENSOR_STALE_TIMEOUT,
    DEFAULT_SLEEP_PRESET,
    DEFAULT_TARGET_TOLERANCE,
    DEFAULT_WINDOW_DETECTION_ENABLED,
    DEFAULT_WINDOW_SLOPE_THRESHOLD,
    DOMAIN,
    DRIFT_NOTICE_SECONDS,
    FAULT_CENTRAL_UNAVAILABLE,
    FAULT_VALVE_MISMATCH,
    FAULT_VALVE_UNAVAILABLE,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    MODEL_SAVE_INTERVAL,
    SIGNAL_ZONE_UPDATE,
    VALVE_TRAVEL_TIMEOUT,
)
from .coordinator import CentralHeaterCoordinator, get_coordinator
from .engine.boiler import ZoneReport
from .engine.controller import ControllerConfig
from .engine.safety import SafetyConfig
from .engine.window import WindowConfig
from .engine.zone import ZoneConfig, ZoneCore, ZoneStatus
from .storage import StateStore

if TYPE_CHECKING:
    from .climate import AdaptiveThermostat

_LOGGER = logging.getLogger(__name__)

EVENT_LOGBOOK_ENTRY = "logbook_entry"
PRESETS = ("sleep", "home", "away")
DIAGNOSTIC_WRITE_INTERVAL = 900.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _float_state(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class ZoneRuntime:
    """Everything that runs for one config entry (one heating zone)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: StateStore) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.store = store
        config = {**entry.data, **entry.options}
        self.config = config

        def entity(key: str) -> str | None:
            value = config.get(key)
            return value if isinstance(value, str) and value.strip() else None

        self.name: str = config.get(CONF_NAME, DEFAULT_NAME)
        heaters = config.get(CONF_HEATER)
        if isinstance(heaters, str):
            heaters = [heaters]
        self.heater_ids: list[str] = [h for h in (heaters or []) if isinstance(h, str) and h]
        self.temp_sensor_id = entity(CONF_TEMP_SENSOR)
        self.humidity_sensor_id = entity(CONF_HUMIDITY_SENSOR)
        self.outdoor_sensor_id = entity(CONF_OUTDOOR_SENSOR)
        self.backup_outdoor_sensor_id = entity(CONF_BACKUP_OUTDOOR_SENSOR)
        self.door_window_sensor_id = entity(CONF_DOOR_WINDOW_SENSOR)
        self.motion_sensor_id = entity(CONF_MOTION_SENSOR)
        self.central_heater_id = entity(CONF_CENTRAL_HEATER)

        self.turn_on_delay = float(
            config.get(CONF_CENTRAL_HEATER_TURN_ON_DELAY, CENTRAL_HEATER_TURN_ON_DELAY)
        )
        self.turn_off_delay = float(
            config.get(CONF_CENTRAL_HEATER_TURN_OFF_DELAY, CENTRAL_HEATER_TURN_OFF_DELAY)
        )
        self.boiler_min_on = float(config.get(CONF_CENTRAL_HEATER_MIN_ON, DEFAULT_CENTRAL_HEATER_MIN_ON))
        self.boiler_min_off = float(config.get(CONF_CENTRAL_HEATER_MIN_OFF, DEFAULT_CENTRAL_HEATER_MIN_OFF))
        self.deadband = float(config.get(CONF_TARGET_TOLERANCE, DEFAULT_TARGET_TOLERANCE))
        self.window_detection = bool(
            config.get(CONF_WINDOW_DETECTION_ENABLED, DEFAULT_WINDOW_DETECTION_ENABLED)
        )
        self.window_slope_threshold = max(
            0.5, float(config.get(CONF_WINDOW_SLOPE_THRESHOLD, DEFAULT_WINDOW_SLOPE_THRESHOLD))
        )
        self.auto_on_off = bool(config.get(CONF_AUTO_ON_OFF_ENABLED, False))
        self.auto_on_temp = float(config.get(CONF_AUTO_ON_TEMP, DEFAULT_AUTO_ON_TEMP))
        self.auto_off_temp = float(config.get(CONF_AUTO_OFF_TEMP, DEFAULT_AUTO_OFF_TEMP))
        self.stale_timeout_s = 60.0 * float(
            config.get(CONF_SENSOR_STALE_TIMEOUT, DEFAULT_SENSOR_STALE_TIMEOUT)
        )
        self.min_burst_s = float(config.get(CONF_MIN_BURST, DEFAULT_MIN_BURST))
        self.max_burst_s = 60.0 * float(config.get(CONF_MAX_BURST, DEFAULT_MAX_BURST))
        self.model_control = bool(config.get(CONF_MODEL_CONTROL, DEFAULT_MODEL_CONTROL))

        self.presets: dict[str, float] = {
            "sleep": _clamp(
                float(config.get(CONF_SLEEP_PRESET, DEFAULT_SLEEP_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP
            ),
            "home": _clamp(
                float(config.get(CONF_HOME_PRESET, DEFAULT_HOME_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP
            ),
            "away": _clamp(
                float(config.get(CONF_AWAY_PRESET, DEFAULT_AWAY_PRESET)), MIN_TARGET_TEMP, MAX_TARGET_TEMP
            ),
        }

        # Intent (persisted).
        self.hvac_mode: HVACMode = HVACMode.OFF
        self.target: float = self.presets["home"]
        self.preset: str | None = "home"
        self.manual_override = False

        # Observed values.
        self.current_temperature: float | None = None
        self.current_humidity: float | None = None
        self.current_outdoor: float | None = None
        self.door_window_open: bool | None = None

        # Engine and HA plumbing.
        self.core: ZoneCore | None = None
        self.status: ZoneStatus | None = None
        self.coordinator: CentralHeaterCoordinator | None = None
        self.trackers: dict[str, CommandTracker] = {h: CommandTracker(h) for h in self.heater_ids}
        self.entity: AdaptiveThermostat | None = None
        self.zone_heater_on = False
        self.valve_error: str | None = None
        self.valve_error_at: float | None = None
        self._drift_notice: tuple[str, float] | None = None
        self._last_boiler_on = self.central_heater_id is None
        self._valve_open_memory: dict[str, bool] = {h: False for h in self.heater_ids}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None
        self._timer_at: float | None = None
        self._step_task: asyncio.Task[None] | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._stopping = False
        self._published: tuple[Any, ...] | None = None
        self._last_write_t = 0.0
        self._last_model_save = 0.0
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ helpers
    def _now(self) -> float:
        return dt_util.utcnow().timestamp()

    @property
    def entity_id(self) -> str | None:
        return self.entity.entity_id if self.entity is not None else None

    def _zone_config(self) -> ZoneConfig:
        return ZoneConfig(
            deadband=self.deadband,
            window_detection=self.window_detection,
            use_model=self.model_control,
            turn_off_delay_s=self.turn_off_delay if self.central_heater_id else 0.0,
            controller=ControllerConfig(
                min_burst_s=self.min_burst_s,
                max_burst_s=max(self.min_burst_s, self.max_burst_s),
            ),
            window=WindowConfig(slope_threshold=self.window_slope_threshold),
            safety=SafetyConfig(stale_timeout_s=self.stale_timeout_s),
        )

    # ---------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Load state, seed the engine, subscribe and run the first step."""
        if self._started:
            return
        self._started = True
        self._stopping = False
        now = self._now()
        await self.store.async_load()
        stored = self.store.get(self.entry_id) or {}
        self._restore_intent(stored)
        core_data = stored.get("core") if isinstance(stored.get("core"), dict) else None
        self.core = ZoneCore(self._zone_config(), stored=core_data, now=now)
        self.core.set_hvac(now, self.hvac_mode == HVACMode.HEAT)
        self.core.set_target(self.target)
        self._seed_from_states(now)

        if self.central_heater_id:
            self.coordinator = get_coordinator(self.hass, self.central_heater_id)
            self.coordinator.register(
                self.entry_id,
                self.wake,
                min_on_s=self.boiler_min_on,
                min_off_s=self.boiler_min_off,
            )
            if self.coordinator.actual_on and any(
                is_physically_open(classify(h, self.hass.states.get(h))) for h in self.heater_ids
            ):
                self.coordinator.seed_running(self.entry_id)

        tracked = [
            e
            for e in (
                self.humidity_sensor_id,
                self.outdoor_sensor_id,
                self.backup_outdoor_sensor_id,
                self.door_window_sensor_id,
                self.central_heater_id,
                *self.heater_ids,
            )
            if e
        ]
        if self.temp_sensor_id:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.temp_sensor_id], self._temp_changed)
            )
            self._unsubs.append(
                async_track_state_report_event(self.hass, [self.temp_sensor_id], self._temp_reported)
            )
        if tracked:
            self._unsubs.append(async_track_state_change_event(self.hass, tracked, self._other_changed))
        self._persist_intent()
        self.wake()

    async def async_stop(self) -> None:
        """Stop listeners, timers and tasks; leave actuators safe."""
        if not self._started:
            return
        self._stopping = True
        self._started = False
        if self._step_task is not None:
            self._step_task.cancel()
            self._step_task = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer is not None:
            self._timer()
            self._timer = None
        for task in list(self._command_tasks):
            task.cancel()
        for task in list(self._command_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._command_tasks.clear()
        now = self._now()
        held = False
        if self.coordinator is not None:
            held = self.coordinator.hold_until(self.entry_id, now) is not None
            await self.coordinator.async_unregister(self.entry_id)
            held = held or self.coordinator.hold_until(self.entry_id, now) is not None
        # Close valves that are open for a burst; an overrun hold (boiler just
        # stopped) is left open and reconciled at the next start.
        if not held and self.status is not None and self.status.want_heat:
            for heater in self.heater_ids:
                if is_physically_open(classify(heater, self.hass.states.get(heater))):
                    await async_command(self.hass, heater, False)
        self._persist(now, force=True)
        await self.store.async_flush()

    # ------------------------------------------------------------- intent
    def _restore_intent(self, stored: dict[str, Any]) -> None:
        mode = stored.get("hvac_mode")
        try:
            self.hvac_mode = HVACMode(mode) if mode in (HVACMode.HEAT, HVACMode.OFF) else HVACMode.OFF
        except ValueError:
            self.hvac_mode = HVACMode.OFF
        presets = stored.get("preset_targets")
        if isinstance(presets, dict):
            for key, value in presets.items():
                if key in self.presets and isinstance(value, (int, float)):
                    self.presets[key] = _clamp(float(value), MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        target = stored.get("target_temperature")
        if isinstance(target, (int, float)):
            self.target = _clamp(float(target), MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        preset = stored.get("current_preset")
        self.preset = preset if preset in self.presets else None
        if "current_preset" not in stored:
            self.preset = "home"
        self.manual_override = bool(stored.get("manual_override", False))

    @callback
    def set_hvac_mode(self, mode: HVACMode) -> None:
        now = self._now()
        if mode == HVACMode.OFF:
            self.hvac_mode = HVACMode.OFF
            self.manual_override = self.auto_on_off
        elif mode == HVACMode.HEAT:
            self.hvac_mode = HVACMode.HEAT
            self.manual_override = False
        else:
            return
        if self.core is not None:
            self.core.set_hvac(now, self.hvac_mode == HVACMode.HEAT)
        self._intent_changed()

    @callback
    def set_target(self, temperature: float) -> None:
        target = _clamp(float(temperature), MIN_TARGET_TEMP, MAX_TARGET_TEMP)
        if self.preset in self.presets:
            self.presets[self.preset] = target
        self.target = target
        if self.core is not None:
            self.core.set_target(target)
        self._intent_changed()

    @callback
    def set_preset(self, preset: str) -> None:
        if preset not in self.presets:
            return
        self.preset = preset
        self.target = self.presets[preset]
        if self.core is not None:
            self.core.set_target(self.target)
        self._intent_changed()

    @callback
    def reset_manual_override(self) -> None:
        self.manual_override = False
        self._intent_changed()

    @callback
    def dismiss_window_alert(self) -> None:
        if self.core is not None:
            self.core.dismiss_window(self._now())
        self._intent_changed()

    @callback
    def reset_model(self) -> None:
        if self.core is not None:
            self.core.reset_model()
        self._last_model_save = 0.0
        self._intent_changed()

    @callback
    def _intent_changed(self) -> None:
        """Intent changed: show it now, persist it, let the step act on it."""
        self._update_zone_heater(self._compute_zone_heater())
        self._write_state(force=True)
        self._persist_intent()
        self.wake()

    # ------------------------------------------------------------ inputs
    def _seed_from_states(self, now: float) -> None:
        assert self.core is not None
        core = self.core
        out = self._outdoor_value()
        if out is not None:
            core.on_outdoor(now, out)
            self.current_outdoor = out
        if self.temp_sensor_id:
            state = self.hass.states.get(self.temp_sensor_id)
            value = _float_state(state)
            changed = state.last_changed.timestamp() if state is not None else now
            reported = state.last_reported.timestamp() if state is not None else now
            if value is not None:
                core.on_temperature(min(now, changed), value, reported_t=reported)
                self.current_temperature = value
            else:
                core.on_temperature(now, None)
        self.current_humidity = _float_state(
            self.hass.states.get(self.humidity_sensor_id) if self.humidity_sensor_id else None
        )
        self._update_window_contact(now)

    def _outdoor_value(self) -> float | None:
        value = _float_state(self.hass.states.get(self.outdoor_sensor_id)) if self.outdoor_sensor_id else None
        if value is None and self.backup_outdoor_sensor_id:
            value = _float_state(self.hass.states.get(self.backup_outdoor_sensor_id))
        return value

    def _update_window_contact(self, now: float) -> None:
        if not self.door_window_sensor_id or self.core is None:
            return
        state = self.hass.states.get(self.door_window_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            is_open = None
        else:
            is_open = state.state == STATE_ON
        if is_open != self.door_window_open:
            self.door_window_open = is_open
            self.core.set_window_contact(now, is_open)

    @callback
    def _temp_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.core is None or self._stopping:
            return
        state = event.data["new_state"]
        value = _float_state(state)
        now = self._now()
        reported = state.last_reported.timestamp() if state is not None else now
        self.core.on_temperature(now, value, reported_t=reported)
        if value is not None:
            self.current_temperature = value
        self.wake()

    @callback
    def _temp_reported(self, event: Event[EventStateReportedData]) -> None:
        """Same value reported again (heartbeat): the sensor is alive."""
        if self.core is None or self._stopping:
            return
        state = event.data["new_state"]
        value = _float_state(state)
        if value is None:
            return
        now = self._now()
        self.core.on_temperature(now, value, reported_t=now)
        self.wake()

    @callback
    def _other_changed(self, event: Event[EventStateChangedData]) -> None:
        if self.core is None or self._stopping:
            return
        entity_id = event.data["entity_id"]
        now = self._now()
        if entity_id in (self.outdoor_sensor_id, self.backup_outdoor_sensor_id):
            out = self._outdoor_value()
            if out is not None:
                self.core.on_outdoor(now, out)
                self.current_outdoor = out
        elif entity_id == self.humidity_sensor_id:
            self.current_humidity = _float_state(event.data["new_state"])
        elif entity_id == self.door_window_sensor_id:
            self._update_window_contact(now)
        self.wake()

    # ----------------------------------------------------------- scheduler
    @callback
    def wake(self) -> None:
        """Request a control step (coalesced, never concurrent)."""
        if self._step_task is not None or self._stopping or not self._started:
            return
        # A tracked, non-eager task: runs after the current callback, at most
        # one pending at a time, awaited by async_block_till_done in tests and
        # cancelled on unload.  The step itself never awaits.
        self._step_task = self.hass.async_create_task(
            self._async_step(), f"{DOMAIN} {self.name} step", eager_start=False
        )

    async def _async_step(self) -> None:
        self._step_task = None
        self._run_step()

    @callback
    def _schedule(self, when: float) -> None:
        if self._stopping:
            return
        if self._timer is not None and self._timer_at is not None and self._timer_at <= when:
            return
        if self._timer is not None:
            self._timer()
        self._timer_at = when
        self._timer = async_call_later(self.hass, max(0.0, when - self._now()), self._timer_fired)

    @callback
    def _timer_fired(self, _now: Any) -> None:
        self._timer = None
        self._timer_at = None
        self.wake()

    @callback
    def _run_step(self) -> None:
        if self._stopping or self.core is None:
            return
        try:
            self._step()
        except Exception:
            _LOGGER.exception("[%s] Control step failed", self.name)
            self._schedule(self._now() + CONTROL_TICK)

    # ---------------------------------------------------------------- step
    def _valve_summary(self) -> tuple[float, bool, bool]:
        """(open fraction, all open, all closed) from measured states."""
        if not self.heater_ids:
            return 0.0, False, True
        opened = 0
        all_open = True
        all_closed = True
        for heater in self.heater_ids:
            kind = classify(heater, self.hass.states.get(heater))
            if kind == UNAVAILABLE:
                # Physically the valve keeps its last position.
                is_open = self._valve_open_memory.get(heater, False)
                all_open = False
                all_closed = all_closed and not is_open
            else:
                is_open = is_physically_open(kind)
                self._valve_open_memory[heater] = is_open
                all_open = all_open and kind == OPEN
                all_closed = all_closed and kind == CLOSED
            opened += 1 if is_open else 0
        return opened / len(self.heater_ids), all_open, all_closed

    def _boiler_on(self) -> bool:
        if self.coordinator is None:
            return True
        actual = self.coordinator.actual_on
        if actual is not None:
            self._last_boiler_on = actual
        return self._last_boiler_on

    def _step(self) -> None:
        core = self.core
        assert core is not None
        now = self._now()
        fraction, all_open, all_closed = self._valve_summary()
        core.on_actuators(now, fraction, self._boiler_on())
        self._observe_actuators(now)
        self._auto_on_off(now)
        core.set_actuator_fault(self._actuator_fault(now))
        if self.coordinator is not None:
            core.set_start_conditions(
                self.coordinator.start_delay(now, self.turn_on_delay), self.coordinator.shared_duty()
            )
        else:
            core.set_start_conditions(25.0, 0.0)
        status = core.update(now)
        self.status = status
        if status.window_event is not None:
            self._log_window(status)

        hold: float | None = None
        if self.coordinator is not None:
            self.coordinator.report(
                self.entry_id,
                ZoneReport(
                    demand=status.want_heat,
                    forced_stop=status.forced_stop,
                    valves_open=all_open,
                    valves_closed=all_closed,
                    turn_on_delay_s=self.turn_on_delay,
                    turn_off_delay_s=self.turn_off_delay,
                ),
            )
            hold = self.coordinator.hold_until(self.entry_id, now)
        desired_open = status.want_heat or hold is not None
        deadline = self._reconcile(now, desired_open)
        self._update_zone_heater(status.want_heat and all_open)
        self._write_state()
        async_dispatcher_send(self.hass, f"{SIGNAL_ZONE_UPDATE}_{self.entry_id}")
        self._persist(now)
        wake_at = min(status.next_wake, now + CONTROL_TICK)
        if deadline is not None:
            wake_at = min(wake_at, deadline)
        if hold is not None:
            wake_at = min(wake_at, hold + 0.5)
        self._schedule(wake_at)

    def _compute_zone_heater(self) -> bool:
        if self.hvac_mode != HVACMode.HEAT or self.status is None:
            return False
        return self.zone_heater_on and self.status.want_heat

    # ---------------------------------------------------------- reconcile
    def _observe_actuators(self, now: float) -> None:
        """Track availability and clear a mismatch once the actuator proves itself."""
        for heater, tracker in self.trackers.items():
            kind = classify(heater, self.hass.states.get(heater))
            if kind == UNAVAILABLE:
                if tracker.unavailable_since is None:
                    tracker.unavailable_since = now
                    _LOGGER.info("[%s] %s unavailable", self.name, heater)
                continue
            if tracker.unavailable_since is not None:
                _LOGGER.info("[%s] %s available again (%s)", self.name, heater, kind)
                tracker.unavailable_since = None
            if tracker.mismatch:
                reached = (kind == OPEN) if tracker.mismatch_target else (kind == CLOSED)
                if reached or now >= tracker.next_allowed:
                    # It works again, or the back-off is over: allow a new attempt.
                    tracker.mismatch = False
                    tracker.mismatch_target = None
                    tracker.attempts = 0

    def _reconcile(self, now: float, desired_open: bool) -> float | None:
        """Drive every heater entity towards ``desired_open``; returns next deadline."""
        deadlines: list[float] = []
        for heater, tracker in self.trackers.items():
            kind = classify(heater, self.hass.states.get(heater))
            if kind == UNAVAILABLE:
                since = tracker.unavailable_since if tracker.unavailable_since is not None else now
                deadlines.append(since + ACTUATOR_UNAVAILABLE_GRACE)
                continue
            at_target = (kind == OPEN) if desired_open else (kind == CLOSED)
            if at_target:
                tracker.target_open = desired_open
                tracker.confirmed = True
                tracker.attempts = 0
                continue
            if tracker.mismatch and tracker.mismatch_target == desired_open:
                # Known to fail towards this position: wait for the back-off.
                deadlines.append(tracker.next_allowed)
                continue
            if tracker.in_flight:
                continue
            if tracker.target_open == desired_open and tracker.confirmed:
                # It reached the commanded position earlier and moved away on
                # its own (e.g. re-opened after a gateway blip).
                self._record_drift(now, heater, kind, desired_open)
                tracker.attempts = 0
            elif tracker.target_open == desired_open:
                moving = kind == (OPENING if desired_open else CLOSING)
                if now < tracker.deadline:
                    deadlines.append(tracker.deadline)
                    continue
                if now < tracker.next_allowed:
                    deadlines.append(tracker.next_allowed)
                    continue
                tracker.attempts += 1
                if tracker.attempts >= ACTUATOR_MAX_ATTEMPTS:
                    tracker.mismatch = True
                    tracker.mismatch_target = desired_open
                    tracker.next_allowed = now + ACTUATOR_RETRY_BACKOFF
                    tracker.attempts = 0
                    _LOGGER.warning(
                        "[%s] %s does not %s (state %s); retrying in %.0f s",
                        self.name,
                        heater,
                        "open" if desired_open else "close",
                        kind,
                        ACTUATOR_RETRY_BACKOFF,
                    )
                    deadlines.append(tracker.next_allowed)
                    continue
                if moving:
                    tracker.deadline = now + VALVE_TRAVEL_TIMEOUT
                    deadlines.append(tracker.deadline)
                    continue
            else:
                if not desired_open and tracker.target_open is False and kind in (OPEN, OPENING):
                    self._record_drift(now, heater, kind, desired_open)
                tracker.attempts = 0
            self._send(heater, tracker, desired_open, now)
            deadlines.append(tracker.deadline)
        return min(deadlines) if deadlines else None

    def _send(self, heater: str, tracker: CommandTracker, turn_on: bool, now: float) -> None:
        tracker.target_open = turn_on
        tracker.confirmed = False
        tracker.issued_at = now
        tracker.deadline = now + VALVE_TRAVEL_TIMEOUT
        tracker.in_flight = True
        _LOGGER.debug("[%s] %s %s", self.name, "Opening" if turn_on else "Closing", heater)

        async def _run() -> None:
            ok = False
            try:
                ok = await async_command(self.hass, heater, turn_on)
            finally:
                tracker.in_flight = False
                if not ok:
                    tracker.deadline = self._now()
                self.wake()

        task = self.entry.async_create_task(self.hass, _run(), f"{DOMAIN} {self.name} {heater}")
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    def _record_drift(self, now: float, heater: str, kind: str, desired_open: bool) -> None:
        tracker = self.trackers[heater]
        tracker.drift_events += 1
        tracker.last_drift_at = now
        message = (
            f"{heater} opened without a command; closing it"
            if not desired_open
            else f"{heater} closed without a command; reopening it"
        )
        _LOGGER.warning("[%s] %s (state %s)", self.name, message, kind)
        self._drift_notice = (message, now)
        self._fire_logbook(message)

    def _actuator_fault(self, now: float) -> tuple[str, str] | None:
        for heater, tracker in self.trackers.items():
            if (
                tracker.unavailable_since is not None
                and now - tracker.unavailable_since >= ACTUATOR_UNAVAILABLE_GRACE
            ):
                return FAULT_VALVE_UNAVAILABLE, f"{self._friendly(heater)} unavailable"
        for heater, tracker in self.trackers.items():
            if tracker.mismatch:
                verb = "open" if tracker.mismatch_target else "close"
                return FAULT_VALVE_MISMATCH, f"{self._friendly(heater)} does not {verb}"
        if self.coordinator is not None and self.coordinator.fault is not None:
            if self.hvac_mode == HVACMode.HEAT:
                return FAULT_CENTRAL_UNAVAILABLE, self.coordinator.fault[1]
        return None

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return entity_id

    # ------------------------------------------------------------ auto mode
    def _auto_on_off(self, now: float) -> None:
        if not self.auto_on_off or self.manual_override or self.core is None:
            return
        outdoor = self.core.observer.outdoor.value
        if outdoor is None:
            return
        if self.hvac_mode == HVACMode.OFF and outdoor < self.auto_on_temp:
            _LOGGER.info("[%s] Auto ON (outdoor %.1f°C < %.1f°C)", self.name, outdoor, self.auto_on_temp)
            self.hvac_mode = HVACMode.HEAT
        elif self.hvac_mode == HVACMode.HEAT and outdoor > self.auto_off_temp:
            _LOGGER.info("[%s] Auto OFF (outdoor %.1f°C > %.1f°C)", self.name, outdoor, self.auto_off_temp)
            self.hvac_mode = HVACMode.OFF
        else:
            return
        self.core.set_hvac(now, self.hvac_mode == HVACMode.HEAT)
        self._persist_intent()

    # ------------------------------------------------------------ outputs
    @property
    def hvac_action(self) -> HVACAction:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if self.zone_heater_on else HVACAction.IDLE

    @property
    def window_open(self) -> bool:
        return bool(self.status and self.status.window_open)

    @property
    def window_alert(self) -> str | None:
        return self.status.window_alert if self.status else None

    @property
    def fault(self) -> str | None:
        return self.status.fault if self.status else None

    @property
    def fault_message(self) -> str | None:
        return self.status.fault_message if self.status else None

    def current_valve_error(self, now: float) -> str | None:
        """Actuator problem text for the card (``valve_error``), or None."""
        message: str | None = None
        fault = self.fault
        if fault in (FAULT_VALVE_MISMATCH, FAULT_VALVE_UNAVAILABLE, FAULT_CENTRAL_UNAVAILABLE):
            message = self.fault_message
        elif self._drift_notice is not None:
            text, at = self._drift_notice
            if now - at < DRIFT_NOTICE_SECONDS:
                message = text
            else:
                self._drift_notice = None
        if message != self.valve_error:
            self.valve_error = message
            self.valve_error_at = now if message else None
        return message

    def _update_zone_heater(self, on: bool) -> None:
        if self.hvac_mode != HVACMode.HEAT:
            on = False
        if on == self.zone_heater_on:
            return
        self.zone_heater_on = on
        self._fire_logbook("started heating" if on else "stopped heating")

    def _fire_logbook(self, message: str) -> None:
        entity_id = self.entity_id
        if not entity_id:
            return
        self.hass.bus.async_fire(
            EVENT_LOGBOOK_ENTRY,
            {"name": self.name, "message": message, "entity_id": entity_id, "domain": "climate"},
        )

    def _log_window(self, status: ZoneStatus) -> None:
        event = status.window_event
        if event is None:
            return
        if event.opened:
            _LOGGER.warning("[%s] %s - heating paused", self.name, status.window_alert)
            self._fire_logbook(status.window_alert or "window open detected")
        else:
            _LOGGER.info("[%s] Window event cleared (%s)", self.name, event.reason)

    def signature(self) -> tuple[Any, ...]:
        """Card-visible state; the entity is written only when this changes."""
        self.current_valve_error(self._now())
        return (
            self.hvac_mode,
            self.hvac_action,
            self.target,
            self.preset,
            self.current_temperature,
            self.current_humidity,
            self.window_open,
            self.window_alert,
            self.valve_error,
            self.fault,
            self.fault_message,
            self.manual_override,
            self.zone_heater_on,
        )

    @callback
    def _write_state(self, force: bool = False) -> None:
        entity = self.entity
        if entity is None or entity.hass is None or not entity.entity_id:
            return
        signature = self.signature()
        now = self._now()
        if force or signature != self._published or now - self._last_write_t >= DIAGNOSTIC_WRITE_INTERVAL:
            self._published = signature
            self._last_write_t = now
            entity.async_write_ha_state()

    def diagnostics(self) -> dict[str, Any]:
        """Values for the diagnostic sensors and unrecorded attributes."""
        status = self.status
        core = self.core
        out: dict[str, Any] = {}
        if core is None:
            return out
        est = core.estimator
        prm = est.params
        out["control_mode"] = status.control_mode if status else None
        out["model_confidence"] = round(core.confidence(), 2)
        out["predicted_peak"] = round(status.predicted_peak, 2) if status and status.predicted_peak else None
        out["cycle_on_duration_s"] = (
            round(status.planned_burst_s) if status and status.planned_burst_s else None
        )
        out["cycle_tail_duration_s"] = round(status.tail_s) if status and status.tail_s else None
        out["cycle_time_to_target_s"] = (
            round(status.time_to_peak_s) if status and status.time_to_peak_s else None
        )
        out["heat_elapsed_s"] = round(status.heat_elapsed_s) if status else 0
        out["heat_flowing"] = bool(status and status.heat_flowing)
        out["learning"] = bool(status and status.learning)
        out["control_reason"] = status.reason if status else None
        out["temperature_slope_instant_per_hour"] = round(status.slope_instant, 3) if status else 0.0
        out["temperature_slope_per_hour"] = round(status.slope_hourly, 3) if status else 0.0
        out["current_outdoor_temp"] = self.current_outdoor
        out["last_fault"] = core.last_fault
        out["last_fault_at"] = (
            dt_util.utc_from_timestamp(core.last_fault_at).isoformat() if core.last_fault_at else None
        )
        out["thermal_params"] = {
            "q_s": round(prm.q_s, 3),
            "tau_f_min": round(prm.tau_f * 60, 2),
            "tau_e_min": round(prm.tau_e * 60, 1),
            "tau_r_h": round(prm.tau_r, 1),
            "tau_am_h": round(prm.tau_am, 2),
            "rho": round(prm.rho, 2),
            "base_gain": round(est.base_gain, 3),
            "shared_gain": round(est.shared_gain, 3),
            "bursts_learned": est.stats.bursts_learned,
        }
        return out

    # ---------------------------------------------------------- persistence
    def _payload(self) -> dict[str, Any]:
        return {
            "hvac_mode": self.hvac_mode.value,
            "target_temperature": self.target,
            "current_preset": self.preset,
            "manual_override": self.manual_override,
            "preset_targets": dict(self.presets),
            "core": self.core.to_dict() if self.core is not None else None,
        }

    def _persist_intent(self) -> None:
        self.store.set(self.entry_id, self._payload())

    def _persist(self, now: float, *, force: bool = False) -> None:
        if force or now - self._last_model_save >= MODEL_SAVE_INTERVAL:
            self._last_model_save = now
            self.store.set(self.entry_id, self._payload())
