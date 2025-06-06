"""Constants for the Adaptive Thermostat integration."""

DOMAIN = "adaptive_thermostat"

# Platforms to set up
PLATFORMS = ["climate"]

# Configuration Keys
CONF_HEATER = "heater"
CONF_CENTRAL_HEATER = "central_heater"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_DOOR_WINDOW_SENSOR = "door_window_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_SLEEP_PRESET = "sleep_preset"
CONF_HOME_PRESET = "home_preset"
CONF_AWAY_PRESET = "away_preset"
CONF_BACKUP_OUTDOOR_SENSOR = "backup_outdoor_sensor"

# Default values
DEFAULT_NAME = "Adaptive Thermostat"
DEFAULT_HOME_PRESET = 23.0
DEFAULT_SLEEP_PRESET = 21.0
DEFAULT_AWAY_PRESET = 18.0

# Timing constants for central heater coordination
CENTRAL_HEATER_TURN_ON_DELAY = 10  # seconds - delay before turning on central heater after valve
CENTRAL_HEATER_TURN_OFF_DELAY = 120  # seconds - delay before turning off valve after central heater