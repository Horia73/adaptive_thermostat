"""Constants for the Adaptive Thermostat integration."""

DOMAIN = "adaptive_thermostat"

# Platforms to set up
PLATFORMS = ["climate"]

# Configuration Keys
CONF_HEATER = "heater"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_DOOR_WINDOW_SENSOR = "door_window_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_WEATHER_SENSOR = "weather_sensor"
CONF_SLEEP_PRESET = "sleep_preset"
CONF_HOME_PRESET = "home_preset"
CONF_AWAY_PRESET = "away_preset"

# Default values (optional, can also be handled in config flow)
DEFAULT_NAME = "Adaptive Thermostat"
DEFAULT_HOME_PRESET = 23.0
DEFAULT_SLEEP_PRESET = 21.0
DEFAULT_AWAY_PRESET = 18.0