# Cycle ledger (2.3)

**Why.**  The recorder keeps `hvac_action`, `zone_heater_on`, target, current
temperature, window and fault of each climate entity, but the controller's
`predicted_peak`, `control_reason` and `control_mode` are unrecorded
attributes (they used to make up most of the recorder traffic).  So a
recorder export cannot answer "what peak did the controller predict when it
stopped this burst, and what peak did the room then reach?".  The cycle
ledger keeps exactly that, per burst, in a small bounded store.

**What it is not.**  It does not change control: it only reads the zone core
after each control step (verified on the closed-loop simulator: identical
bursts, valve requests, boiler switches and learned state with and without
it), it adds no entity, no attribute and no recorder row, and a ledger error
disables the ledger, never control.

## Retrieval

Settings -> Devices & services -> Adaptive Thermostat -> the zone -> ⋮ ->
**Download diagnostics** (Home Assistant's diagnostics integration, part of
`default_config`).  The JSON file contains, under `data`:

```json
{
  "schema": "adaptive_thermostat.cycle_ledger",
  "format": 1,
  "generated_at": "2026-11-14T12:36:00+00:00",
  "zone": "Birou",
  "entity_id": "climate.birou",
  "ledger_enabled": true,
  "limits": {"max_cycles": 96, "max_age_days": 14.0,
             "observe_after_predicted_peak_s": 1800.0, "observe_default_s": 5400.0,
             "observe_min_s": 1800.0, "observe_max_s": 10800.0,
             "peak_confirmation": "a later report (change or heartbeat) not above the maximum",
             "report_gap_flag_s": 2400.0},
  "config": {"target_tolerance": 0.1, "model_control_enabled": true, "min_burst_s": 120.0,
             "max_burst_s": 2700.0, "sensor_stale_timeout_s": 10800.0,
             "temp_sensor": "sensor.birou_temperature", "window_source": "contact",
             "linked_window_zones": []},
  "current": {"hvac_mode": "heat", "target": 23.0, "...": "the live diagnostics attributes"},
  "next_id": 412,
  "cycles": ["oldest first, see below"]
}
```

The same data lives in `.storage/adaptive_thermostat_cycles_<entry_id>`
(Home Assistant `Store`, version 1, key `data` = `{format, next_id, cycles}`).

## One cycle record

Times are UTC ISO 8601 (as the recorder's `last_changed`), durations are
whole seconds, temperatures °C.  `null` = not applicable / not available.

| field | meaning |
|---|---|
| `id` | per-zone sequence number (survives restarts) |
| `state` | `heating`, `observing` (tail) or `closed` |
| `requested_at` | the controller requested heat (valves commanded open) |
| `heat_started_at` | heat started to flow (valve open and boiler firing, measured); `null` = never |
| `stopped_at` | the burst ended (valves commanded closed) |
| `closed_at` | the observation ended |
| `end` | `observation_complete` (window elapsed), `next_cycle` (next burst requested), `no_heat`, `restart` |
| `clean` | `true` = comparable: see below |
| `flags` | perturbations, in the order seen (bounded to 16) |
| `first_perturbation`, `first_perturbation_after_request_s` | the first of them and when |
| `target_min`, `target_max` | setpoint range from request to the end of the observation |
| `start.mode`, `start.reason` | `model`/`fallback`; `fire`, `piggyback`, ... |
| `start.target`, `start.reading`, `start.outdoor` | at the request |
| `start.planned_s` | planned heat time at the request |
| `start.predicted_peak`, `start.time_to_peak_s`, `start.tail_s` | prediction of the planned burst (model mode) |
| `start.aim_peak`, `start.heat_trigger_below`, `start.band_feasible`, `start.confidence` | reading-space plan at the request |
| `stop.mode`, `stop.reason`, `stop.forced` | e.g. `model` / `predicted peak reaches aim`, `max burst`, `planned length reached` (fallback), `reading above band`, `off`, `window open (contact)`, `sensor_stale`, `heat did not start`; `forced` = safety stop |
| `stop.planned_s` | last planned heat time (re-planned every step in model mode) |
| `stop.heat_s` | heat time actually delivered (heat flowing) |
| `stop.commanded_s` | request to stop (includes valve travel and boiler start delay) |
| `stop.predicted_peak`, `stop.time_to_peak_s`, `stop.tail_s` | the stopping decision's prediction: the peak if heat stops now, including pump overrun and the learned peak correction (model mode; `null` in fallback and for forced stops) |
| `stop.model_peak_as_run` | the model's peak for the burst as actually run, from its start state (the value the confidence check uses; also in fallback) |
| `stop.confidence`, `stop.peak_correction`, `stop.trigger_correction` | controller state at the stop |
| `stop.target`, `stop.reading`, `stop.outdoor`, `stop.slope_per_hour` | at the stop |
| `observed.window_s` | observation length: predicted time to peak + 30 min (model), else the model check's horizon, else 90 min; clamped to 30 min - 3 h |
| `observed.max`, `observed.max_after_stop_s` | highest reading from the stop to the end of the observation, and when |
| `observed.reports_after_max` | reports after the maximum (0 = maximum not confirmed) |
| `observed.min`, `observed.last`, `observed.readings` | lowest / last reading, number of reports (heartbeats included) |
| `observed.max_report_gap_s` | longest silence of the sensor in the observation |
| `observed.max_before_perturbation` | highest reading before the first perturbation |
| `observed.truncated_after_stop_s` | observation cut by the next burst after this long |
| `observed.peak_error` | `observed.max - stop.predicted_peak` (positive = room went higher than predicted) |

### Flags

| flag | set when |
|---|---|
| `forced_stop` | the burst was stopped by a safety condition (off, window, fault, no heat) |
| `no_heat` | heat never flowed (`end` = `no_heat`) |
| `mode_changed` | model/fallback switched during the burst |
| `target_changed` | setpoint differs from the one at the request |
| `window_open`, `linked_window_open` | own window (contact or autodetect) / a linked zone's window open |
| `manual_off`, `auto_off`, `hvac_off` | user switched OFF, auto on/off switched OFF, OFF by another path |
| `fault:<code>` | `sensor_stale`, `sensor_unavailable`, `valve_mismatch`, `valve_unavailable`, `central_unavailable` |
| `reading_unavailable` | the temperature sensor reported unavailable/unknown |
| `sensor_gap` | more than 40 min without any report (a periodic report is due every 30 min) |
| `reheat` | the next burst was requested before the predicted peak time, or before the maximum was confirmed |
| `peak_unconfirmed` | the observation window elapsed with the maximum as the last report |
| `model_reset` | `reset_adaptive_profile` during the cycle |
| `restart` | Home Assistant / the integration restarted before the record closed (`end` = `restart`) |

**`clean`** = the burst heated, stopped by itself, the observation ended with
`observation_complete` or `next_cycle`, and no flag was raised: constant
target, no window, no off, no fault, no unavailable reading or long gap, no
early new burst, confirmed maximum.  For prediction accuracy, compare
`observed.max` with `stop.predicted_peak` (model) or `stop.model_peak_as_run`
on clean records.

## Limits

* **Passive gains are not excluded.**  Sun, cooking, people or a neighbouring
  room warm the reading exactly like the tail of a burst; `clean` cannot rule
  them out, and a measured peak cannot prove they were absent.
* **Temperature never proves a door or window position.**  Only contacts (or
  the autodetector's own decision) set `window_open`; an open interior door
  is invisible.
* Readings are the sensor's displayed values (0.1 °C steps, reported on
  change or every 30 min); predictions are model temperatures.  An error of
  ±0.05-0.1 °C is within one display step.
* A restart loses the rest of an open observation (`restart`); a crash or
  power loss can lose up to one hour of records (saves are delayed one hour;
  unload and Home Assistant's final write save immediately).
* Retention: the last 96 cycles per zone and at most 14 days (~1-3 days of
  winter cycling).  Download the diagnostics regularly for longer analyses.
* Storage: about 1.8 kB per record, at most ~175 kB per zone file, written at
  most once an hour per zone while cycles happen.
