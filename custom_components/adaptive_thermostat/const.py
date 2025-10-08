"""Constants for the Adaptive Thermostat integration."""

DOMAIN = "adaptive_thermostat"

# Platforms to set up
PLATFORMS = ["climate", "sensor"]

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
CONF_TARGET_TOLERANCE = "target_tolerance"
CONF_CONTROL_WINDOW = "control_window"
CONF_MIN_ON_TIME = "min_on_time"
CONF_MIN_OFF_TIME = "min_off_time"
CONF_FILTER_ALPHA = "filter_alpha"
CONF_WINDOW_DETECTION_ENABLED = "window_detection_enabled"
CONF_WINDOW_SLOPE_THRESHOLD = "window_slope_threshold"

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
DEFAULT_TARGET_TOLERANCE = 0.1
DEFAULT_CONTROL_WINDOW = 180  # seconds
DEFAULT_MIN_ON_TIME = 45      # seconds
DEFAULT_MIN_OFF_TIME = 45     # seconds
DEFAULT_FILTER_ALPHA = 0.2
DEFAULT_WINDOW_DETECTION_ENABLED = False
DEFAULT_WINDOW_SLOPE_THRESHOLD = 0.3  # °C per minute

# Temperature limits applied to presets and manual setpoints
MIN_TARGET_TEMP = 5.0
MAX_TARGET_TEMP = 30.0

# Timing constants for central heater coordination
CENTRAL_HEATER_TURN_ON_DELAY = 10  # seconds - delay before turning on central heater after valve
CENTRAL_HEATER_TURN_OFF_DELAY = 120  # seconds - delay before turning off valve after central heater

# Auto on/off defaults
DEFAULT_AUTO_ON_TEMP = 10.0  # °C - turn on when outdoor temp is below this
DEFAULT_AUTO_OFF_TEMP = 18.0  # °C - turn off when outdoor temp is above this

# Persistent storage
STORAGE_KEY = "adaptive_thermostat_models"
STORAGE_VERSION = 1

# Dispatcher signals
SIGNAL_THERMOSTAT_READY = "adaptive_thermostat_thermostat_ready"
