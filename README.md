# Adaptive Thermostat for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

The Adaptive Thermostat is a custom integration for Home Assistant that provides intelligent climate control. It learns your preferences and adapts to your environment to maintain comfort efficiently.

**This repository contains the core integration. For the Lovelace UI card, please see the [Adaptive Thermostat Card repository](https://github.com/Horia73/adaptive-thermostat-card).**

## Features

*   Adaptive learning of temperature preferences.
*   (Add more features specific to your thermostat)
*   Configurable via Home Assistant UI.

## Prerequisites

*   Home Assistant version 2023.1.0 or newer.
*   HACS (Home Assistant Community Store) installed.
*   **Adaptive Thermostat Card installed from HACS.** (Link to your card's GitHub repo or HACS installation instructions for it).

## Installation

This integration consists of two parts that need to be installed via HACS:
1.  The **Adaptive Thermostat Integration** (this repository).
2.  The **Adaptive Thermostat Card** (from its separate repository).

### Step 1: Install Adaptive Thermostat Integration (this repository)

1.  **Ensure HACS is Installed:** If you don't have HACS, follow the [official HACS installation guide](https://hacs.xyz/docs/setup/download).
2.  **Add Custom Repository for the Integration:**
    *   Open HACS in your Home Assistant.
    *   Go to "Integrations".
    *   Click the three dots in the top right corner and select "Custom repositories".
    *   In the "Repository" field, enter: `https://github.com/Horia73/adaptive_thermostat`
    *   In the "Category" field, select "Integration".
    *   Click "Add".
3.  **Install Integration:**
    *   Search for "Adaptive Thermostat" in HACS (it should now appear).
    *   Click "Install".
4.  **Restart Home Assistant:** After installation, restart Home Assistant to load the integration.

### Step 2: Install Adaptive Thermostat Card

1.  **Add Custom Repository for the Card:**
    *   In HACS, go to "Frontend".
    *   Click the three dots in the top right corner and select "Custom repositories".
    *   In the "Repository" field, enter the URL of your **new card repository** (e.g., `https://github.com/Horia73/adaptive-thermostat-card`).
    *   In the "Category" field, select "Lovelace" (or "Plugin").
    *   Click "Add".
2.  **Install Card:**
    *   Search for "Adaptive Thermostat Card" (or the name you gave it in its `hacs.json`) in HACS under "Frontend".
    *   Click "Install".
3.  **Verify Card Resource (Important):**
    *   The card *should* be automatically added to your Lovelace resources by HACS.
    *   To verify, or if it's not working, go to **Settings > Dashboards** in Home Assistant.
    *   Click the three dots menu in the top right and select "Resources".
    *   Check if a resource exists with a URL similar to `/hacsfiles/adaptive-thermostat-card/adaptive-thermostat-card.js`.
    *   If not, click "+ ADD RESOURCE":
        *   URL: `/hacsfiles/adaptive-thermostat-card/adaptive-thermostat-card.js` (replace `adaptive-thermostat-card` with the exact name HACS uses for your card plugin if different).
        *   Resource type: `JavaScript Module`.
        *   Click "Create".
    *   You may need to refresh your browser or clear its cache.

## Configuration (After installing both Integration and Card)

1.  Go to **Settings > Devices & Services** in Home Assistant.
2.  Click the **+ ADD INTEGRATION** button in the bottom right.
3.  Search for "Adaptive Thermostat" and select it.
4.  Follow the on-screen instructions to configure your thermostat.

## Lovelace Card Usage

Once both the integration and card are installed and configured:

1.  Open the dashboard where you want to add the card and click the three dots in the top right to "Edit Dashboard".
2.  Click the "+ ADD CARD" button.
3.  Search for "Custom: Adaptive Thermostat Card".
4.  Select the card and configure its options:
    ```yaml
    type: custom:adaptive-thermostat-card
    entity: climate.your_adaptive_thermostat_entity_id # Replace with your entity
    # Add any other card-specific options here
    ```
5.  Click "Save".

## Troubleshooting

*   **Integration not found:** Ensure you've restarted Home Assistant after installing the integration via HACS. Check logs.
*   **Card not found or "Custom element doesn't exist":**
    *   Ensure you've installed the **Adaptive Thermostat Card** from HACS (Frontend section).
    *   Verify the Lovelace resource URL as described in "Step 2: Install Adaptive Thermostat Card".
    *   Clear browser cache and refresh.
    *   Check Home Assistant developer tools console for errors.
*   For issues with the **integration**, please [open an issue here](https://github.com/Horia73/adaptive_thermostat/issues).
*   For issues with the **card**, please open an issue on the [Adaptive Thermostat Card repository](https://github.com/Horia73/adaptive-thermostat-card/issues).

## Contributing

Contributions to the integration are welcome here! For card contributions, please see the card repository.

## License

This project is licensed under the [MIT License](LICENSE).
