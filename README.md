# Adaptive Thermostat

This repository contains the **Adaptive Thermostat** custom integration for Home Assistant.

The integration provides a thermostat entity that adapts its behaviour based on environmental sensors and presets. A companion Lovelace card is included under `custom_components/adaptive_thermostat/www/`.

## Installation via HACS

1. In Home Assistant, open **HACS** and go to **Integrations**.
2. Choose **"Explore & download repositories"** and add this repository as a
   **custom repository** of type *Integration*.
3. Locate **Adaptive Thermostat** in the list and click **Download**.
4. Restart Home Assistant to load the integration files.
5. After restart, go to **Settings → Devices & Services** and click
   **"Add Integration"**. Search for **Adaptive Thermostat** and follow the
   prompts.

### Lovelace card

This integration bundles a Lovelace card located in `www/adaptive-thermostat-card.js`.
From Home Assistant 2021.11 and later the card will be automatically registered
as a resource thanks to the entry in `manifest.json`. After installing the
integration you can simply add a manual card with type `adaptive-thermostat-card`.
If you run an older Home Assistant version you may need to add the resource
manually under **Settings → Dashboards → Resources** using the URL:

```
/adaptive_thermostat/adaptive-thermostat-card.js
```
