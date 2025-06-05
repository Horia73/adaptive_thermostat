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
    hass.data.setdefault(DOMAIN, {}) # DOMAIN is imported from .const

    # Register the Lovelace card
    # This ensures the card is available, complementing HACS's registration via manifest.json.
    # The URL must match the one HACS uses, derived from your manifest.json.
    card_url = f"/hacsfiles/{DOMAIN}/adaptive-thermostat-card.js"
    resource_type = "module"  # Assuming the card is an ES6 module. Change to "js" if it's a plain script.

    # Ensure Lovelace component and registration function are available
    if hasattr(hass.components, 'lovelace') and hasattr(hass.components.lovelace, 'async_register_resource'):
        _LOGGER.debug("Attempting to register Lovelace card: %s as type %s", card_url, resource_type)
        try:
            # async_register_resource is idempotent.
            # It will not add a duplicate if a resource with the same url and type already exists.
            await hass.components.lovelace.async_register_resource(hass, card_url, resource_type)
            _LOGGER.info("Successfully ensured Lovelace card %s (type: %s) is registered.", card_url, resource_type)
        except Exception as e:
            # Log an error if registration fails, but don't prevent setup.
            _LOGGER.error(
                "Error registering Lovelace card %s: %s. "
                "HACS registration via manifest.json should still work.",
                card_url, e
            )
    else:
        _LOGGER.warning(
            "Lovelace component or async_register_resource not available. "
            "Cannot programmatically register card. Relying on HACS manifest entry."
        )

    # Return True as YAML config is not primary for this integration.
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