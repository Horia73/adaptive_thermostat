"""The Adaptive Thermostat integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import DOMAIN, PLATFORMS
from .storage import get_state_store

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id})
SERVICES = {
    "reset_manual_override": "reset_manual_override",
    "dismiss_window_alert": "dismiss_window_alert",
    "reset_adaptive_profile": "reset_adaptive_profile",
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for the whole integration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault("entities", {})
    domain_data.setdefault("entry_to_entity_id", {})
    domain_data.setdefault("coordinators", {})

    def _make_handler(method: str) -> Any:
        @callback
        def _handle(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            entity = hass.data.get(DOMAIN, {}).get("entities", {}).get(entity_id)
            if entity is None or not hasattr(entity, method):
                _LOGGER.warning("Could not find adaptive thermostat entity: %s", entity_id)
                return
            getattr(entity, method)()
            _LOGGER.info("%s for %s", method, entity_id)

        return _handle

    for service, method in SERVICES.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, _make_handler(method), schema=SERVICE_SCHEMA)

    async def _on_stop(_event: Event) -> None:
        """Best effort: do not leave a boiler firing while Home Assistant is down."""
        from .actuators import async_command

        for coordinator in list(hass.data.get(DOMAIN, {}).get("coordinators", {}).values()):
            if coordinator.actual_on:
                _LOGGER.info("Home Assistant stopping: switching off %s", coordinator.entity_id)
                await async_command(hass, coordinator.entity_id, False)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one zone."""
    from .runtime import ZoneRuntime

    runtime = ZoneRuntime(hass, entry, get_state_store(hass))
    entry.runtime_data = runtime
    # Control starts first; entities attach to the running runtime as views.
    await runtime.async_start()
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await runtime.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one zone: stop control first, then the entities."""
    runtime = entry.runtime_data
    await runtime.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the stored state of a deleted zone."""
    store = get_state_store(hass)
    await store.async_load()
    store.remove(entry.entry_id)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
