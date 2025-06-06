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
CONF_BACKUP_OUTDOOR_SENSOR = "backup_outdoor_sensor"
CONF_SLEEP_PRESET = "sleep_preset"
CONF_HOME_PRESET = "home_preset"
CONF_AWAY_PRESET = "away_preset"

# Advanced configuration keys
CONF_CENTRAL_HEATER_TURN_ON_DELAY = "central_heater_turn_on_delay"
CONF_CENTRAL_HEATER_TURN_OFF_DELAY = "central_heater_turn_off_delay"
CONF_AUTO_ON_OFF_ENABLED = "auto_on_off_enabled"
CONF_AUTO_ON_TEMP = "auto_on_temp"
CONF_AUTO_OFF_TEMP = "auto_off_temp"

# Default values
DEFAULT_NAME = "Adaptive Thermostat"
DEFAULT_HOME_PRESET = 23.0
DEFAULT_SLEEP_PRESET = 21.0
DEFAULT_AWAY_PRESET = 18.0

# Timing constants for central heater coordination
CENTRAL_HEATER_TURN_ON_DELAY = 10  # seconds - delay before turning on central heater after valve
CENTRAL_HEATER_TURN_OFF_DELAY = 120  # seconds - delay before turning off valve after central heater

# Auto on/off defaults
DEFAULT_AUTO_ON_TEMP = 10.0  # °C - turn on when outdoor temp is below this
DEFAULT_AUTO_OFF_TEMP = 18.0  # °C - turn off when outdoor temp is above this