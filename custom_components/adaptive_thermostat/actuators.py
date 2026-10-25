"""Actuator state classification and command tracking.

Supports the heater domains offered by the config flow (``valve``,
``switch``, ``input_boolean``, ``climate``) and the boiler domains
(``switch``, ``input_boolean``, ``climate``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from .const import VALVE_COMMAND_TIMEOUT

_LOGGER = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"
OPENING = "opening"
CLOSING = "closing"
UNAVAILABLE = "unavailable"


def classify(entity_id: str, state: State | None) -> str:
    """Map an entity state to open/closed/opening/closing/unavailable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return UNAVAILABLE
    domain = entity_id.split(".", 1)[0]
    value = state.state
    if domain == "valve":
        if value in (OPEN, CLOSED, OPENING, CLOSING):
            return value
        return UNAVAILABLE
    if domain == "climate":
        return CLOSED if value == "off" else OPEN
    return OPEN if value == STATE_ON else CLOSED


def is_physically_open(kind: str) -> bool:
    """Heat can flow (valve open or closing, switch on)."""
    return kind in (OPEN, CLOSING)


def service_for(entity_id: str, turn_on: bool) -> tuple[str, str]:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "switch"
    if domain == "valve":
        return domain, "open_valve" if turn_on else "close_valve"
    return domain, "turn_on" if turn_on else "turn_off"


async def async_command(hass: HomeAssistant, entity_id: str, turn_on: bool) -> bool:
    """Send an on/off (open/close) command; returns False on failure."""
    domain, service = service_for(entity_id, turn_on)
    try:
        async with asyncio.timeout(VALVE_COMMAND_TIMEOUT):
            await hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)
    except TimeoutError:
        _LOGGER.warning("Timed out sending %s.%s to %s", domain, service, entity_id)
        return False
    except Exception as err:  # noqa: BLE001 - any integration error is a failed command
        _LOGGER.warning("Failed to send %s.%s to %s: %s", domain, service, entity_id, err)
        return False
    return True


@dataclass
class CommandTracker:
    """Tracks the last command sent to one actuator."""

    entity_id: str
    target_open: bool | None = None
    issued_at: float = 0.0
    deadline: float = 0.0
    attempts: int = 0
    in_flight: bool = False
    confirmed: bool = False  # the entity reached ``target_open``
    mismatch: bool = False  # repeated commands did not reach the target
    mismatch_target: bool | None = None  # the position it failed to reach
    next_allowed: float = 0.0
    unavailable_since: float | None = None
    drift_events: int = 0
    last_drift_at: float | None = None

    def reached(self, kind: str) -> bool:
        if self.target_open is None:
            return True
        return (kind == OPEN) if self.target_open else (kind == CLOSED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_open": self.target_open,
            "attempts": self.attempts,
            "in_flight": self.in_flight,
            "mismatch": self.mismatch,
            "drift_events": self.drift_events,
        }
