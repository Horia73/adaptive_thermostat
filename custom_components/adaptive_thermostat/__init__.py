"""The Adaptive Thermostat integration."""

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

# Import constants from const.py
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Adaptive Thermostat integration and register Lovelace card."""
    hass.data.setdefault(DOMAIN, {})

    # Register the Lovelace card
    # This ensures the card is available, complementing HACS's registration via manifest.json.
    # The URL must match the one HACS uses, derived from your manifest.json.
    card_url = f"/hacsfiles/{DOMAIN}/adaptive-thermostat-card.js"
    resource_type = "module"

    if hasattr(hass.components, 'lovelace') and hasattr(hass.components.lovelace, 'async_register_resource'):
        _LOGGER.debug("Attempting to register Lovelace card: %s as type %s", card_url, resource_type)
        try:
            await hass.components.lovelace.async_register_resource(hass, card_url, resource_type)
            _LOGGER.info("Successfully ensured Lovelace card %s (type: %s) is registered.", card_url, resource_type)
        except Exception as e:
            _LOGGER.error(
                "Error registering Lovelace card %s: %s. "
                "HACS registration via manifest.json or manual addition may still work.",
                card_url, e
            )
    else:
        _LOGGER.warning(
            "Lovelace component or async_register_resource not available. "
            "Cannot programmatically register card. Relying on HACS manifest entry or manual addition."
        )

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Adaptive Thermostat from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("Setting up Adaptive Thermostat entry %s", entry.entry_id)

    # Forward the setup to the climate platform.
    await hass.config_entries.async_setup_platforms(entry, PLATFORMS)

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