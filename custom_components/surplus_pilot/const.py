"""Constants for Surplus Pilot."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "surplus_pilot"
PLATFORMS = ["sensor", "switch", "number", "select", "time", "text", "date"]

UPDATE_INTERVAL_SECONDS = 60
FEED_CONFIRM_READINGS = 2     # consistent NEW battery readings before the battery-feeds-car state flips
FEED_CAR_STEADY_MIN = 6       # battery reading counts only when the car charged this long before it
FEED_EXPIRY_DAYS = 3          # without a new reading the configured value applies again
STORAGE_VERSION = 1

# ---------------------------------------------------------------- energy (entry.data)
CONF_PV_SENSOR = "pv_sensor"
CONF_LOAD_SENSOR = "load_sensor"
CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_POWER_SENSOR = "battery_power_sensor"
CONF_STALE_MINUTES = "stale_minutes"
CONF_PRICE_SOURCE = "price_source"
CONF_TIBBER_HOME = "tibber_home"
CONF_PRICE_SENSOR = "price_sensor"
CONF_PRICE_HISTORY_SENSOR = "price_history_sensor"
CONF_PV_END_BEFORE_SUNSET_H = "pv_end_before_sunset_h"

PRICE_NONE = "none"
PRICE_TIBBER = "tibber"
PRICE_SENSOR = "sensor"

DEFAULT_BATTERY_CAPACITY_KWH = 10.0
DEFAULT_BATTERY_MIN_SOC = 15.0
DEFAULT_STALE_MINUTES = 20
DEFAULT_PV_END_BEFORE_SUNSET_H = 1.5

# ---------------------------------------------------------------- car (entry.data["car"])
CONF_CAR = "car"
CONF_CAR_NAME = "name"
CONF_CAR_CHARGE_SWITCH = "charge_switch"
CONF_CAR_CURRENT_ENTITY = "current_entity"
CONF_CAR_SOC_SENSOR = "soc_sensor"
CONF_CAR_SOC_FALLBACK = "soc_fallback_sensor"
CONF_CAR_LIMIT_ENTITY = "limit_entity"
CONF_CAR_LIMIT_DEFAULT = "limit_default"
CONF_CAR_PLUGGED_SENSOR = "plugged_sensor"
CONF_CAR_PLUGGED_FALLBACK = "plugged_fallback_sensor"
CONF_CAR_CHARGING_SENSOR = "charging_sensor"
CONF_CAR_POWER_SENSOR = "power_sensor"
CONF_CAR_TRACKER = "tracker"
CONF_CAR_TRACKER_FALLBACK = "tracker_fallback"
CONF_CAR_CAPACITY_KWH = "capacity_kwh"
CONF_CAR_EFFICIENCY = "efficiency"
CONF_CAR_MIN_A = "min_amps"
CONF_CAR_MAX_A = "max_amps"
CONF_CAR_PHASES = "phases"
CONF_CAR_VOLTAGE = "voltage"
CONF_CAR_MAX_COMMANDS = "max_commands_per_day"
CONF_CAR_PAUSE_ENTITY = "pause_entity"
CONF_CAR_PAUSE_STATE = "pause_state"
CONF_CAR_ALLOW_GRID = "allow_grid"
CONF_CAR_BATTERY_FEEDS = "battery_feeds_car"

DEFAULT_CAR_LIMIT = 80.0
DEFAULT_CAR_CAPACITY_KWH = 60.0
DEFAULT_CAR_EFFICIENCY = 0.9
DEFAULT_CAR_MIN_A = 6
DEFAULT_CAR_MAX_A = 16
DEFAULT_CAR_PHASES = 3
DEFAULT_CAR_VOLTAGE = 230

# car decisions: every 15 min on a 30-min average (live-tuned in Smart Car Charger)
CAR_DECISION_MINUTES = 15
CAR_AVERAGE_MINUTES = 30
CAR_MIN_COVERAGE_MINUTES = 10
CAR_STOP_AFTER_LOW_WINDOWS = 2
CAR_AUTOSTART_GUARD_S = 300
CAR_RETRY_DELAYS_MIN = (1, 2, 5, 10)

# ---------------------------------------------------------------- devices (entry.data["devices"])
CONF_DEVICES = "devices"
CONF_DEV_ID = "id"
CONF_DEV_NAME = "name"
CONF_DEV_KIND = "kind"            # "switch" | "climate"
CONF_DEV_ENTITY = "entity"
CONF_DEV_CLIMATE_MODE = "climate_on_mode"
CONF_DEV_PRIORITY = "priority"
CONF_DEV_POWER_KW = "power_kw"
CONF_DEV_POWER_SENSOR = "power_sensor"
CONF_DEV_SCHEDULE = "schedule"
CONF_DEV_WINDOW_START = "window_start"
CONF_DEV_WINDOW_END = "window_end"
CONF_DEV_DEPENDS_ON = "depends_on"
CONF_DEV_MIN_RUNTIME_H = "min_runtime_h"
CONF_DEV_SOC_RESERVE = "soc_reserve"

KIND_SWITCH = "switch"
KIND_CLIMATE = "climate"
CLIMATE_MODES = ["heat", "cool", "auto", "heat_cool", "dry", "fan_only"]

DEVICE_ON_DELAY_S = 600          # surplus must hold 10 min before switching on
DEVICE_OFF_DELAY_S = 600         # and be gone 10 min before switching off
DEVICE_FORCED_ON_DELAY_S = 120   # min-runtime forcing is deterministic
CLIMATE_DELAY_FACTOR = 2.0       # thermostats/compressors switch half as often
DEVICE_SMOOTH_MINUTES = 5        # device decisions on a 5-min median

# ---------------------------------------------------------------- departures
NUM_SLOTS = 4
DEFAULT_SLOT_TIME = "07:00"
DEFAULT_SLOT_TARGET = 50.0
MISSED_DEPARTURE_GRACE_H = 1.0

# ---------------------------------------------------------------- modes
MODE_AUTO = "auto"
MODE_OBSERVE = "observe"
MODE_OFF = "off"
MODES = [MODE_AUTO, MODE_OBSERVE, MODE_OFF]

# ---------------------------------------------------------------- solar start
DEFAULT_SOLAR_OFFSETS = [3.5, 3.0, 2.5, 2.0, 2.0, 2.2, 2.2, 2.0, 2.5, 3.0, 3.5, 4.0]
CALIBRATION_INTERVAL = timedelta(hours=24)
CALIBRATION_RETRY_INTERVAL = timedelta(hours=1)
CALIBRATION_LOOKBACK_DAYS = 400
CALIBRATION_MIN_HOURLY_POINTS = 24
CALIBRATION_CLOUD_WINDOW_DAYS = 10
CALIBRATION_CLOUD_GOOD_RATIO = 0.70
CALIBRATION_THRESHOLD_RATIO = 0.15
CALIBRATION_MIN_GOOD_DAYS = 5
CALIBRATION_MAX_INTERP_MONTHS = 2

DEFAULT_NIGHT_BASE_KW = 0.5

# cheap grid top-up (threshold = percentile of the last 8 days' prices,
# never above the same percentile of the last 30 days)
PRICE_RECENT_DAYS = 8
PRICE_CAP_DAYS = 30
PRICE_ARCHIVE_DAYS = PRICE_CAP_DAYS
PRICE_MIN_SAMPLES_H = 48
DEFAULT_CHEAP_PERCENTILE = 10
DEFAULT_CHEAP_TARGET = 80
LOG_LENGTH = 60
