"""The Adaptive Thermostat integration."""

import logging
from pathlib import Path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

# Import constants from const.py
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# Define the URL path for the card served by this integration
LOCAL_CARD_PATH_URL = f"/{DOMAIN}_local_assets/adaptive-thermostat-card.js"
# Define the actual file system path to the card
LOCAL_CARD_FILE_PATH = Path(__file__).parent / "www" / "adaptive-thermostat-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Adaptive Thermostat integration, register Lovelace card, and serve card asset."""
    hass.data.setdefault(DOMAIN, {})

    # Serve the Lovelace card JS file locally
    # This makes the card available at LOCAL_CARD_PATH_URL
    _LOGGER.debug("Attempting to serve Lovelace card from %s at URL %s", LOCAL_CARD_FILE_PATH, LOCAL_CARD_PATH_URL)
    try:
        hass.http.async_register_static_path(
            LOCAL_CARD_PATH_URL,
            str(LOCAL_CARD_FILE_PATH), # Ensure it's a string
            cache_headers=False # Can be true for production, false for dev
        )
        _LOGGER.info("Lovelace card JS is being served at %s", LOCAL_CARD_PATH_URL)
    except Exception as e:
        _LOGGER.error("Failed to register static path for Lovelace card at %s: %s", LOCAL_CARD_PATH_URL, e)
        # If serving fails, we might not want to proceed or rely on other methods.
        # For now, we'll log and continue, assuming HACS or manual setup might still work.

    # Register the Lovelace card resource using the new local URL
    card_url = LOCAL_CARD_PATH_URL
    resource_type = "module"

    if hasattr(hass.components, 'lovelace') and hasattr(hass.components.lovelace, 'async_register_resource'):
        _LOGGER.debug("Attempting to register Lovelace card resource: %s as type %s", card_url, resource_type)
        try:
            await hass.components.lovelace.async_register_resource(hass, card_url, resource_type)
            _LOGGER.info("Successfully ensured Lovelace card resource %s (type: %s) is registered.", card_url, resource_type)
        except Exception as e:
            _LOGGER.error(
                "Error registering Lovelace card resource %s: %s. "
                "Manual registration might be required via Lovelace UI.",
                card_url, e
            )
    else:
        _LOGGER.warning(
            "Lovelace component or async_register_resource not available. "
            "Cannot programmatically register card resource. Manual registration will be required."
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