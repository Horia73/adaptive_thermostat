# Adaptive Thermostat for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

The Adaptive Thermostat is a custom integration for Home Assistant that provides intelligent climate control. It learns your preferences and adapts to your environment to maintain comfort efficiently. This repository also includes a companion Lovelace card for easy UI integration.

## Features

*   Adaptive learning of temperature preferences.
*   (Add more features specific to your thermostat)
*   Configurable via Home Assistant UI.
*   Custom Lovelace card for a beautiful and functional interface.

## Prerequisites

*   Home Assistant version 2023.1.0 or newer.
*   HACS (Home Assistant Community Store) installed (for the recommended installation method).

## Installation

There are two ways to install the Adaptive Thermostat integration:

### Method 1: HACS (Recommended)

1.  **Ensure HACS is Installed:** If you don't have HACS, follow the [official HACS installation guide](https://hacs.xyz/docs/setup/download).
2.  **Add Custom Repository:**
    *   Open HACS in your Home Assistant.
    *   Go to "Integrations".
    *   Click the three dots in the top right corner and select "Custom repositories".
    *   In the "Repository" field, enter: `https://github.com/Horia73/adaptive_thermostat`
    *   In the "Category" field, select "Integration".
    *   Click "Add".
3.  **Install Integration:**
    *   Search for "Adaptive Thermostat" in HACS.
    *   Click "Install". HACS will install both the integration and the Lovelace card (`adaptive-thermostat-card.js`).
4.  **Restart Home Assistant:** After installation, restart Home Assistant to load the integration.

### Method 2: Manual Installation

1.  **Download Files:**
    *   Go to the [Releases page](https://github.com/Horia73/adaptive_thermostat/releases) of this repository.
    *   Download the latest `adaptive_thermostat.zip` file (or clone/download the source).
2.  **Copy Files:**
    *   Extract the downloaded files.
    *   Copy the `custom_components/adaptive_thermostat` directory (which contains the integration files and the `www` subdirectory with the card) into your Home Assistant `config` directory. The final path should be `config/custom_components/adaptive_thermostat/`.
3.  **Restart Home Assistant:** Restart Home Assistant to load the integration.
    *   The Lovelace card should be automatically registered by the integration. If not, you might need to add it manually to your Lovelace resources (see "Lovelace Card Setup" below).

## Configuration

Once the integration is installed and Home Assistant has restarted:

1.  Go to **Settings > Devices & Services** in Home Assistant.
2.  Click the **+ ADD INTEGRATION** button in the bottom right.
3.  Search for "Adaptive Thermostat" and select it.
4.  Follow the on-screen instructions to configure your thermostat. You will be able to set up your climate entity and any other relevant options.

## Lovelace Card Setup

The `adaptive-thermostat-card.js` should be automatically installed and registered if you used HACS or if the manual installation picked up the resource registration from `manifest.json` or `__init__.py`.

To add the card to your Lovelace dashboard:

1.  Open the dashboard where you want to add the card and click the three dots in the top right to "Edit Dashboard".
2.  Click the "+ ADD CARD" button.
3.  Search for "Custom: Adaptive Thermostat Card" (the name might vary slightly based on how it's registered).
4.  Select the card and configure its options, such as the climate entity provided by this integration.
    ```yaml
    type: custom:adaptive-thermostat-card
    entity: climate.your_adaptive_thermostat_entity_id
    # Add any other card-specific options here
    ```
5.  Click "Save".

If the card is not found, and you installed manually, you may need to add it as a resource:
1.  Go to **Settings > Dashboards**.
2.  Click the three dots in the top right and select "Resources".
3.  Click "+ ADD RESOURCE".
4.  URL: `/hacsfiles/adaptive_thermostat/adaptive-thermostat-card.js` (if HACS moved it) or `/local/custom_components/adaptive_thermostat/www/adaptive-thermostat-card.js` (if you placed it manually and it wasn't auto-registered under `hacsfiles`). *Note: The path `/hacsfiles/...` is preferred and should work if the integration is loaded correctly.*
5.  Resource type: `JavaScript Module`.
6.  Click "Create".

## Troubleshooting

*   **Integration not found after installation:** Ensure you have restarted Home Assistant. Check the Home Assistant logs for any errors related to `adaptive_thermostat`.
*   **Card not found:**
    *   Clear your browser cache.
    *   Verify the resource URL in Lovelace resources if added manually.
    *   Check Home Assistant logs for errors related to `adaptive-thermostat-card.js` or Lovelace resource registration.
*   For issues, please [open an issue](https://github.com/Horia73/adaptive_thermostat/issues) on GitHub.

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues.

## License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details (You might want to add a LICENSE file if you don't have one).
