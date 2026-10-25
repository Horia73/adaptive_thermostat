"""Constants for the Adaptive Thermostat integration."""

DOMAIN = "adaptive_thermostat"

# Platforms to set up
PLATFORMS = ["climate", "sensor", "binary_sensor"]

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
CONF_WINDOW_DETECTION_ENABLED = "window_detection_enabled"
CONF_WINDOW_SLOPE_THRESHOLD = "window_slope_threshold"

# Advanced configuration keys
CONF_CENTRAL_HEATER_TURN_ON_DELAY = "central_heater_turn_on_delay"
CONF_CENTRAL_HEATER_TURN_OFF_DELAY = "central_heater_turn_off_delay"
CONF_AUTO_ON_OFF_ENABLED = "auto_on_off_enabled"
CONF_AUTO_ON_TEMP = "auto_on_temp"
CONF_AUTO_OFF_TEMP = "auto_off_temp"

# Added in 2.0 (all optional, with safe defaults for existing entries)
CONF_CENTRAL_HEATER_MIN_ON = "central_heater_min_on"
CONF_CENTRAL_HEATER_MIN_OFF = "central_heater_min_off"
CONF_SENSOR_STALE_TIMEOUT = "sensor_stale_timeout"
CONF_MIN_BURST = "min_burst_seconds"
CONF_MAX_BURST = "max_burst_minutes"
CONF_MODEL_CONTROL = "model_control_enabled"

# Default values
DEFAULT_NAME = "Adaptive Thermostat"
DEFAULT_HOME_PRESET = 23.0
DEFAULT_SLEEP_PRESET = 21.0
DEFAULT_AWAY_PRESET = 18.0
DEFAULT_TARGET_TOLERANCE = 0.1
DEFAULT_WINDOW_DETECTION_ENABLED = False
DEFAULT_WINDOW_SLOPE_THRESHOLD = 2.5  # °C per hour

DEFAULT_CENTRAL_HEATER_MIN_ON = 120  # s, boiler anti short-cycling
DEFAULT_CENTRAL_HEATER_MIN_OFF = 180  # s
DEFAULT_SENSOR_STALE_TIMEOUT = 180  # min
DEFAULT_MIN_BURST = 120  # s
DEFAULT_MAX_BURST = 45  # min
DEFAULT_MODEL_CONTROL = True

# Temperature limits applied to presets and manual setpoints
MIN_TARGET_TEMP = 5.0
MAX_TARGET_TEMP = 30.0

# Timing constants for central heater coordination
CENTRAL_HEATER_TURN_ON_DELAY = 10  # seconds - delay before turning on central heater after valve
CENTRAL_HEATER_TURN_OFF_DELAY = 120  # seconds - delay before turning off valve after central heater

# Auto on/off defaults
DEFAULT_AUTO_ON_TEMP = 10.0  # °C - turn on when outdoor temp is below this
DEFAULT_AUTO_OFF_TEMP = 18.0  # °C - turn off when outdoor temp is above this

# Persistent storage.  2.0 bumped the *minor* version: Home Assistant loads a
# store with a newer minor version unchanged in older code, so a rollback to
# 1.1.x keeps working (it ignores the new "core" key).
STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 2
STORAGE_STATE_KEY = "adaptive_thermostat_runtime_state"
STORAGE_SAVE_DELAY = 10.0
MODEL_SAVE_INTERVAL = 900.0  # s between saves caused only by model learning

# Dispatcher signals
SIGNAL_THERMOSTAT_READY = "adaptive_thermostat_thermostat_ready"
SIGNAL_ZONE_UPDATE = "adaptive_thermostat_zone_update"

# Actuators
VALVE_TRAVEL_TIMEOUT = 60.0  # s to reach the commanded position
VALVE_COMMAND_TIMEOUT = 15.0  # s for the service call itself
ACTUATOR_UNAVAILABLE_GRACE = 90.0  # s before an unavailable actuator is a fault
ACTUATOR_RETRY_BACKOFF = 300.0  # s between retries after repeated failures
ACTUATOR_MAX_ATTEMPTS = 3
DRIFT_NOTICE_SECONDS = 900.0  # how long a corrected drift stays visible
CONTROL_TICK = 60.0

# Faults exposed to the card
FAULT_SENSOR_STALE = "sensor_stale"
FAULT_SENSOR_UNAVAILABLE = "sensor_unavailable"
FAULT_VALVE_MISMATCH = "valve_mismatch"
FAULT_VALVE_UNAVAILABLE = "valve_unavailable"
FAULT_CENTRAL_UNAVAILABLE = "central_unavailable"
