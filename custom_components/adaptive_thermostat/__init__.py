"""The Adaptive Thermostat integration."""

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

# Import constants from const.py
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML (placeholder)."""
    hass.data.setdefault(DOMAIN, {})
    # Return True as YAML config is not primary for this integration
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Adaptive Thermostat from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("Setting up Adaptive Thermostat entry %s", entry.entry_id)

    # Forward the setup to platforms defined in PLATFORMS (likely just 'climate')
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Add the update listener to handle options changes and trigger reload
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    _LOGGER.info("Unloading Adaptive Thermostat entry %s", entry.entry_id)

    # Forward the unload to platforms defined in PLATFORMS
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Optional: Clean up hass.data if you stored anything specific to this entry
    # if unload_ok and entry.entry_id in hass.data.get(DOMAIN, {}):
    #     del hass.data[DOMAIN][entry.entry_id]
    #     if not hass.data[DOMAIN]:
    #           del hass.data[DOMAIN]

    return unload_ok

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    # This function is called by the listener when options are updated.
    _LOGGER.warning( # Use WARNING level initially for high visibility in logs
        "Adaptive Thermostat configuration options updated for %s. Reloading integration.",
        entry.title,
    )
    # Reload the config entry, triggering async_unload_entry and async_setup_entry

    await hass.config_entries.async_reload(entry.entry_id)

