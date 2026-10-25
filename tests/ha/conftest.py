"""Fixtures for the Home Assistant level tests.

``FakePlant`` stands in for the Zigbee valves and the boiler relays: it
implements ``valve.open_valve/close_valve`` and ``switch.turn_on/turn_off``
with a realistic 18 s valve travel (``opening``/``closing`` states), records
every call, and can inject faults (unavailable, stuck, self-opening).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.adaptive_thermostat.const import (
    CONF_AUTO_ON_OFF_ENABLED,
    CONF_CENTRAL_HEATER,
    CONF_CENTRAL_HEATER_TURN_OFF_DELAY,
    CONF_CENTRAL_HEATER_TURN_ON_DELAY,
    CONF_HEATER,
    CONF_HOME_PRESET,
    CONF_OUTDOOR_SENSOR,
    CONF_TARGET_TOLERANCE,
    CONF_TEMP_SENSOR,
    CONF_WINDOW_DETECTION_ENABLED,
    CONF_WINDOW_SLOPE_THRESHOLD,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Load custom_components/ from this repository."""
    return


@dataclass
class FakePlant:
    """Simulated actuators."""

    hass: HomeAssistant
    travel_s: float = 18.0
    calls: list[tuple[float, str, str]] = field(default_factory=list)
    stuck: set[str] = field(default_factory=set)
    _timers: list[Callable[[], None]] = field(default_factory=list)

    def install(self) -> None:
        for service in ("open_valve", "close_valve"):
            self.hass.services.async_register("valve", service, self._valve)
        for service in ("turn_on", "turn_off"):
            self.hass.services.async_register("switch", service, self._switch)

    def now(self) -> float:
        return dt_util.utcnow().timestamp()

    @callback
    def _valve(self, call: ServiceCall) -> None:
        opening = call.service == "open_valve"
        for entity_id in _ids(call):
            self.calls.append((self.now(), entity_id, call.service))
            if entity_id in self.stuck:
                continue
            state = self.hass.states.get(entity_id)
            current = state.state if state else "closed"
            if opening and current in ("open", "opening"):
                continue
            if not opening and current in ("closed", "closing"):
                continue
            self.hass.states.async_set(entity_id, "opening" if opening else "closing")
            final = "open" if opening else "closed"

            @callback
            def _done(
                _now: Any,
                eid: str = entity_id,
                end: str = final,
                moving: str = "opening" if opening else "closing",
            ) -> None:
                st = self.hass.states.get(eid)
                if st is not None and st.state == moving:
                    self.hass.states.async_set(eid, end)

            self._timers.append(async_call_later(self.hass, self.travel_s, _done))

    @callback
    def _switch(self, call: ServiceCall) -> None:
        for entity_id in _ids(call):
            self.calls.append((self.now(), entity_id, call.service))
            if entity_id in self.stuck:
                continue
            self.hass.states.async_set(entity_id, "on" if call.service == "turn_on" else "off")

    def commands(self, entity_id: str) -> list[str]:
        return [svc for _t, eid, svc in self.calls if eid == entity_id]

    def cancel(self) -> None:
        for unsub in self._timers:
            unsub()
        self._timers.clear()


def _ids(call: ServiceCall) -> list[str]:
    ids = call.data.get("entity_id", [])
    return [ids] if isinstance(ids, str) else list(ids)


@pytest.fixture
def plant(hass: HomeAssistant):
    fake = FakePlant(hass)
    fake.install()
    yield fake
    fake.cancel()


def zone_data(
    name: str,
    *,
    valves: list[str],
    sensor: str,
    boiler: str | None = "switch.centrala",
    outdoor: str = "sensor.outdoor",
    window: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        CONF_HEATER: valves,
        CONF_TEMP_SENSOR: sensor,
        CONF_OUTDOOR_SENSOR: outdoor,
        CONF_TARGET_TOLERANCE: 0.1,
        CONF_WINDOW_DETECTION_ENABLED: window,
        CONF_WINDOW_SLOPE_THRESHOLD: 2.5,
        CONF_AUTO_ON_OFF_ENABLED: False,
        CONF_HOME_PRESET: 22.0,
    }
    if boiler:
        data[CONF_CENTRAL_HEATER] = boiler
        data[CONF_CENTRAL_HEATER_TURN_ON_DELAY] = 10
        data[CONF_CENTRAL_HEATER_TURN_OFF_DELAY] = 120
    data.update(extra)
    return data


async def setup_zone(
    hass: HomeAssistant, data: dict[str, Any], entry_id: str | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        title=data["name"],
        unique_id=data["name"],
        entry_id=entry_id or f"entry_{data['name'].lower()}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float, step: float = 5.0
) -> None:
    """Move time forward in small steps, firing timers on the way."""
    # Let work triggered by the last action run at the current instant first.
    await hass.async_block_till_done()
    remaining = seconds
    while remaining > 0:
        dt = min(step, remaining)
        freezer.tick(timedelta(seconds=dt))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= dt


def heat_flowing(hass: HomeAssistant, valve: str, boiler: str = "switch.centrala") -> bool:
    v = hass.states.get(valve)
    b = hass.states.get(boiler)
    return bool(v and v.state in ("open", "closing") and b and b.state == "on")
