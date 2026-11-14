"""Diagnostics download: the zone's cycle ledger and current controller state.

Settings -> Devices & services -> Adaptive Thermostat -> (zone) -> ... ->
*Download diagnostics*.  Nothing here is written to the recorder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .engine import ledger as cycle_ledger
from .runtime import ZoneRuntime

LEDGER_SCHEMA = "adaptive_thermostat.cycle_ledger"


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Cycle ledger of one zone (oldest first) plus the live diagnostics."""
    runtime: ZoneRuntime = entry.runtime_data
    ledger = runtime.ledger
    data = ledger.to_dict() if ledger is not None else {"next_id": None, "cycles": []}
    return {
        "schema": LEDGER_SCHEMA,
        "format": cycle_ledger.LEDGER_FORMAT,
        "generated_at": cycle_ledger.iso(dt_util.utcnow().timestamp()),
        "zone": runtime.name,
        "entity_id": runtime.entity_id,
        "ledger_enabled": ledger is not None,
        "limits": {
            "max_cycles": ledger.max_cycles if ledger is not None else cycle_ledger.MAX_CYCLES,
            "max_age_days": round(cycle_ledger.MAX_AGE_S / 86400.0, 1),
            "observe_after_predicted_peak_s": cycle_ledger.OBSERVE_AFTER_PEAK_S,
            "observe_default_s": cycle_ledger.OBSERVE_DEFAULT_S,
            "observe_min_s": cycle_ledger.OBSERVE_MIN_S,
            "observe_max_s": cycle_ledger.OBSERVE_MAX_S,
            "peak_confirmation": "a later report (change or heartbeat) not above the maximum",
            "report_gap_flag_s": cycle_ledger.REPORT_GAP_FLAG_S,
        },
        "config": {
            "target_tolerance": runtime.deadband,
            "model_control_enabled": runtime.model_control,
            "min_burst_s": runtime.min_burst_s,
            "max_burst_s": runtime.max_burst_s,
            "sensor_stale_timeout_s": runtime.stale_timeout_s,
            "temp_sensor": runtime.temp_sensor_id,
            "window_source": runtime.window_source,
            "linked_window_zones": runtime.linked_window_zone_ids,
        },
        "current": {
            "hvac_mode": runtime.hvac_mode.value,
            "target": runtime.target,
            "current_temperature": runtime.current_temperature,
            "zone_heater_on": runtime.zone_heater_on,
            "fault": runtime.fault,
            **runtime.diagnostics(),
        },
        "next_id": data.get("next_id"),
        "cycles": data.get("cycles", []),
    }
