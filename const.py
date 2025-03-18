"""Constants for the Adaptive Thermostat integration."""
from homeassistant.const import Platform

DOMAIN = "adaptive_thermostat"
PLATFORMS = [Platform.CLIMATE]

# Configuration options
CONF_HEATER = "heater"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_DOOR_WINDOW_SENSOR = "door_window_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_WEATHER_SENSOR = "weather_sensor"
CONF_OUTSIDE_TEMP_OFF = "outside_temp_off"
CONF_SLEEP_PRESET = "sleep_preset"
CONF_HOME_PRESET = "home_preset"
CONF_AWAY_PRESET = "away_preset" 