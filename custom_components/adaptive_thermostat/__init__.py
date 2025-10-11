"""The Adaptive Thermostat integration."""

import logging
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import config_validation as cv

# Import constants from const.py
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Adaptive Thermostat integration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault("entities", {})
    domain_data.setdefault("entry_to_entity_id", {})
    # No Lovelace registration attempt here anymore.
    # Rely on manifest.json and HACS for card availability,
    # and manual user addition if necessary.
    _LOGGER.info("Adaptive Thermostat async_setup completed. Platforms will be set up via async_setup_entry.")
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Adaptive Thermostat from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("Setting up Adaptive Thermostat entry %s", entry.entry_id)

    # Forward the setup to the climate platform.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services if not already registered
    if not hass.services.has_service(DOMAIN, "reset_manual_override"):
        async def reset_manual_override_service(call: ServiceCall) -> None:
            """Service to reset manual override for a thermostat."""
            entity_id = call.data.get("entity_id")

            climate_entities = hass.data.get(DOMAIN, {}).get("entities", {})
            climate_entity = climate_entities.get(entity_id)
            if climate_entity and hasattr(climate_entity, "reset_manual_override"):
                climate_entity.reset_manual_override()
                _LOGGER.info("Reset manual override for %s", entity_id)
                return

            _LOGGER.warning("Could not find adaptive thermostat entity: %s", entity_id)

        hass.services.async_register(
            DOMAIN,
            "reset_manual_override",
            reset_manual_override_service,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
            }),
        )

    # Listen for options updates.
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    _LOGGER.info("Unloading Adaptive Thermostat entry %s", entry.entry_id)
    
    # Unload platforms associated with this entry.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # if unload_ok: # No need to pop from hass.data[DOMAIN][entry.entry_id] unless you specifically stored it there for other reasons
    #     hass.data[DOMAIN].pop(entry.entry_id, None)

    _LOGGER.info("Successfully unloaded Adaptive Thermostat entry %s", entry.entry_id)
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    _LOGGER.debug("Options updated for %s, reloading entry", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
    _LOGGER.warning( # Use WARNING level initially for high visibility in logs
        "Adaptive Thermostat configuration options updated for %s. Reloading integration.",
        entry.title,
    )
    # Reload the config entry, triggering async_unload_entry and async_setup_entry
