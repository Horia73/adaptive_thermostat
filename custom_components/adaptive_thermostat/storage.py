"""Persistent runtime state (one shared store for all zones).

Layout (``.storage/adaptive_thermostat_runtime_state``)::

    {entry_id: {"hvac_mode", "target_temperature", "current_preset",
                "manual_override", "preset_targets", "core": {...}}}

Version history:

* 1.1 (integration <= 1.1.6): per entry also ``thermal_state`` (the old
  peak-ratio parameters) and ``zone_heater_on``.
* 1.2 (2.0.0): ``thermal_state`` dropped (those parameters came from a wrong
  model and are not reusable), ``core`` holds the new estimator state.
  User intent (mode, target, presets, manual override) is kept.

Only the *minor* version changed, so Home Assistant hands the 1.2 data to
1.1.x code unchanged on a rollback; that code ignores ``core``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    STORAGE_MINOR_VERSION,
    STORAGE_SAVE_DELAY,
    STORAGE_STATE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

KEEP_KEYS = ("hvac_mode", "target_temperature", "current_preset", "manual_override", "preset_targets")


def migrate_entry_v1(entry_data: Any) -> dict[str, Any] | None:
    """Convert one 1.1 entry to the 1.2 format (pure, for tests too)."""
    if not isinstance(entry_data, dict):
        return None
    out = {key: entry_data[key] for key in KEEP_KEYS if key in entry_data}
    out["core"] = None
    return out


class RuntimeStore(Store[dict[str, Any]]):
    """Store with the 1.1 -> 1.2 migration."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version == 1 and old_minor_version < 2:
            migrated: dict[str, Any] = {}
            for entry_id, entry_data in (old_data or {}).items():
                new = migrate_entry_v1(entry_data)
                if new is not None:
                    migrated[entry_id] = new
            _LOGGER.info(
                "Migrated adaptive thermostat state for %d zone(s); learned model restarts",
                len(migrated),
            )
            return migrated
        raise NotImplementedError


class StateStore:
    """Shared, lazily loaded cache in front of :class:`RuntimeStore`."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = RuntimeStore(
            hass, STORAGE_VERSION, STORAGE_STATE_KEY, minor_version=STORAGE_MINOR_VERSION
        )
        self._data: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                loaded = await self._store.async_load()
            except Exception:
                _LOGGER.exception("Could not load stored state; starting fresh")
                loaded = None
            self._data = loaded if isinstance(loaded, dict) else {}
        return self._data

    def get(self, entry_id: str) -> dict[str, Any] | None:
        if self._data is None:
            return None
        data = self._data.get(entry_id)
        return data if isinstance(data, dict) else None

    def set(self, entry_id: str, value: dict[str, Any]) -> None:
        if self._data is None:
            self._data = {}
        self._data[entry_id] = value
        self._store.async_delay_save(lambda: dict(self._data or {}), STORAGE_SAVE_DELAY)

    def remove(self, entry_id: str) -> None:
        if self._data and entry_id in self._data:
            del self._data[entry_id]
            self._store.async_delay_save(lambda: dict(self._data or {}), STORAGE_SAVE_DELAY)

    async def async_flush(self) -> None:
        if self._data is not None:
            await self._store.async_save(dict(self._data))


def get_state_store(hass: HomeAssistant) -> StateStore:
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get("state_store")
    if not isinstance(store, StateStore):
        store = StateStore(hass)
        domain_data["state_store"] = store
    return store
