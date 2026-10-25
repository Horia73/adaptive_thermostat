# Adaptive Thermostat for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

A thermostat for **hydronic radiators with motorised zone valves and a shared
boiler**.  Every zone learns a physical model of its radiator and room from
ordinary Home Assistant history and uses it to decide *when* to heat and *for
how long*, so the residual heat in the radiator lands the room on the target
instead of overshooting it.

**The Lovelace card lives in its own repository: [Adaptive Thermostat Card](https://github.com/Horia73/adaptive-thermostat-card).**

* Requires Home Assistant **2025.3** or newer (tested on 2026.10.3).
* No cloud, no extra Python packages: the whole engine is pure Python.

---

## Contents

1. [Installation](#installation)
2. [Configuration and options](#configuration-and-options)
3. [How it works](#how-it-works)
4. [Entities, attributes and services](#entities-attributes-and-services)
5. [Upgrading from 1.x, rollback](#upgrading-from-1x-and-rollback)
6. [Development, tests and replay](#development-tests-and-replay)
7. [Troubleshooting](#troubleshooting)

---

## Installation

1. HACS -> Integrations -> three dots -> *Custom repositories* ->
   `https://github.com/Horia73/adaptive_thermostat` (category *Integration*).
2. Install *Adaptive Thermostat*, restart Home Assistant.
3. Install the card the same way from its repository (category *Lovelace*) and
   check that `/hacsfiles/adaptive-thermostat-card/adaptive-thermostat-card.js`
   is listed under *Settings -> Dashboards -> Resources*.
4. *Settings -> Devices & services -> Add integration -> Adaptive Thermostat*,
   one entry per heating zone.

## Configuration and options

All settings are in the setup flow and can be changed later with
**Reconfigure** on the integration entry.  Entries created by 1.x keep working
unchanged; options added in 2.0 use the defaults below until you change them.

| Step | Option | Default | Notes |
|---|---|---|---|
| Zone | Zone heater/valve(s) | - | `valve`, `switch`, `input_boolean` or `climate`; several valves per zone are supported |
| | Central heater (boiler) | - | optional `switch`/`input_boolean`/`climate`; zones sharing it are coordinated |
| | Temperature, outdoor, backup outdoor, humidity, door/window, motion sensors | - | as before |
| Timing | Turn-on delay | 10 s | boiler starts this long after the valves are confirmed open |
| | Turn-off delay | 120 s | pump overrun: the last zone's valves stay open this long after the boiler stops |
| | **Boiler minimum on time** *(2.0)* | 120 s | anti short-cycling (largest value of all zones on that boiler wins) |
| | **Boiler minimum off time** *(2.0)* | 180 s | anti short-cycling |
| Auto on/off | Enable, on below, off above | off, 10 °C, 18 °C | uses a 20-minute time-based filter of the outdoor temperature |
| Adaptive | Target tolerance | 0.1 °C | comfort band half-width (now actually used, it was ignored in 1.x) |
| | Window detection, slope threshold | off, 2.5 °C/h | see [window detection](#window-detection) |
| | **Stale sensor timeout** *(2.0)* | 180 min | see [safety](#safety-and-faults) |
| | **Minimum burst** *(2.0)* | 120 s | shortest heat time per burst |
| | **Maximum burst** *(2.0)* | 45 min | longest heat time per burst |
| | **Use the learned model** *(2.0)* | on | off = always use the simple fallback controller |
| Presets | Home / Sleep / Away | 23 / 21 / 18 °C | as before |

## How it works

### Architecture

```
 services (set mode / target / preset)        sensors, valves, boiler (state events)
          |  record intent, write state,               |  fed to the engine immediately,
          |  request a step (never wait)               |  request a step
          v                                            v
 +------------------------- ZoneRuntime (one per zone) ---------------------------+
 |  one serialized control step (coalesced, never concurrent), one timer          |
 |                                                                                 |
 |  ZoneCore (pure Python, also used by the simulator and the replay harness)      |
 |    observer -> estimator (EKF) | sensor health | window detector | controller   |
 |    burst state machine: idle -> requested -> heating (counts real heat) -> idle |
 |                                                                                 |
 |  reconciler: desired valve state = burst requested OR overrun hold             |
 |    compare with ACTUAL states, send idempotent commands, confirm by state      |
 |    change + deadline, retry/back-off, drift -> close + flag, unavailable->fault|
 +------------------------------------------|--------------------------------------+
                                            | demand, valves open/closed (sync)
                         CentralHeaterCoordinator (one per boiler entity)
                           turn-on delay, min on/off, last-zone-off,
                           pump-overrun holds, no start while idle valves open
```

* **Intent and one reconciler per zone.** Service calls only change the
  desired state and return at once (the UI updates immediately).  A single
  control step per zone, scheduled as a coalesced task, does everything else,
  so updates are never dropped or run in parallel.  Mode OFF stops a burst at
  once, including one whose valves are still opening; heat is only counted and
  only allowed while the zone wants it.
* **Exactly one timer per zone.**  The next wake-up is the earliest of: end of
  the planned burst, a command confirmation deadline, the end of an overrun
  hold, or a 60 s tick.
* **Actuators are confirmed, not polled.**  Commands run as tracked tasks; the
  valve's own state changes (`opening` -> `open`) wake the step; a command that
  does not reach its position within 60 s is retried, three failures raise
  `valve_mismatch` and back off for 5 minutes.
* **Drift is corrected.**  A valve that opens without a command (e.g. after a
  gateway blip) is closed straight away and reported (`valve_error` text and a
  logbook entry).  An actuator that is unavailable for more than 90 s is a
  fault; when it comes back the desired state is re-applied.
* **Start-up reconciles the real state.**  After a restart nothing is assumed:
  valves that are open but should not be are closed (with the pump-overrun hold
  if the boiler is running).
* **Shared boiler coordinator.**  All zones on a boiler report to one object.
  Its decisions are synchronous, so two zones stopping in the same instant are
  handled one after the other: the first closes, the last turns the boiler
  off and keeps its valves open for the overrun.  A normal end of burst
  honours the boiler's minimum on time; a forced stop (OFF, window, fault)
  stops at once.  The boiler never starts while a zone that does *not* want
  heat still has a valve open, so a zone switched OFF cannot receive heat from
  another zone's burst.

### Thermal model

Each zone is a grey-box resistor-capacitor model of the radiator (emitter),
the room air and the room's building mass.  With `u` the measured heat input
(open valve fraction while the boiler fires, followed by a decaying pump
overrun), `b` the boiler firing for *other* zones while this zone's valves are
closed, and time in hours:

```
dq/dt   = u (q_s - q) / tau_f  -  q / tau_e          radiator heat flux into the air (K/h)
dT/dt   = q - (T - T_out)/tau_r - (T - T_m)/tau_am + g + k b + d
dT_m/dt = (T - T_m) / (rho tau_am)                    building mass
dd/dt   = -d / tau_d                                  transient gains (sun, cooking)
```

`q_s` is the warming rate of a fully charged radiator, `tau_f` its fill time
(hot water flowing) and `tau_e` its discharge time, so the radiator charges
quickly and keeps heating after the valve closes (the tail).  `tau_r` is the
air-to-outdoor loss time, `tau_am` and `rho` the coupling to and size of the
building mass, `g` a slowly varying base gain (neighbouring flats, appliances),
`k` the heat a zone gets while the shared boiler serves other zones
(distribution pipes) and `d` a transient disturbance that relaxes to zero.
For piecewise-constant inputs the model is linear and is advanced with its
exact closed-form solution (checked against RK4 to 1e-12).

Why three nodes: on the recorded history a single room capacity predicted
too much rise after bursts and too much cooling without them; adding the mass
node brought the 4-hour forecast from 1.8x the error of "temperature stays the
same" to parity, and well below it in heated periods (see the replay report).

### Estimator

A joint extended Kalman filter estimates the states and the logarithms of
the parameters from **every** sensor report:

* irregular sampling needs no special handling: the filter advances with the
  exact model to each event, splitting at actuator changes;
* reports are 0.1 °C-quantised; a report that jumps by >= 0.15 °C is a
  threshold crossing and is corrected to the bin edge;
* silence is information: a report-on-change sensor that stays quiet keeps the
  estimate inside a +/-0.15 °C band (inequality pseudo-measurement, applied only
  when the estimate leaves the band);
* robustness: Huber weighting of large innovations (narrow gate when the room
  was not heated recently, wide right after heating), a trust region on every
  parameter step, a floor on parameter uncertainty (a disturbed episode cannot
  lock the model onto wrong values), physical bounds;
* learning stops during window events, sensor or actuator faults, when heat
  produced no response, and for 15 minutes after mode changes and start-up.

The **model confidence** combines the uncertainty of the heat delivered by a
10-minute burst, how much heating has been learned from, the innovation
consistency, and validation of real bursts (predicted vs observed peak, last
eight bursts).  The median validation error is also used as an offset-free
correction of predicted peaks.

### Controller

* **Model control** (confidence >= 60 %, back to fallback below 40 %): every
  tick it simulates "fire now" and "fire in 10 minutes".  If waiting would let
  the room drop below the band, it fires; the burst is the shortest one whose
  predicted peak *including the radiator tail and pump overrun* reaches the
  target.  The firing level is set half a minimum-burst rise below the target
  so the oscillation is centred.  While heating it re-plans and stops as soon
  as the predicted peak reaches the target.  After a burst it waits for the
  predicted tail before firing again (unless the room is 0.6 °C below target).
* **Fallback** (fresh install, after a model reset, or when disabled): fire
  at `target - tolerance`, burst length proportional to the error, long
  lock-out after each burst, early stop at `target + tolerance`, and a relay
  auto-tune that lengthens bursts that made too little progress and shortens
  them after an overshoot.  It is what the model learns from on day one.
* Limits: minimum/maximum burst, minimum off time, valve travel, boiler delays
  and anti short-cycling.  Heat time counts only while heat actually flows.

### Safety and faults

The entity exposes `fault` (null or one of the codes below) and a short
`fault_message`.  Any fault stops heating for that zone.

| `fault` | When |
|---|---|
| `sensor_unavailable` | temperature sensor unavailable for 5 min (or never reported) |
| `sensor_stale` | *time*: no report (Home Assistant's `last_reported`) for the stale timeout once heat has been delivered since the last report, or twice the timeout when the zone is idle (report-on-change sensors are routinely silent for 4-6 h); *physics*: two consecutive heat episodes that the model says must each have warmed the air by >= 0.3 °C (heat vs no heat), with not a single change of the reading |
| `valve_unavailable` | a zone valve unavailable for more than 90 s |
| `valve_mismatch` | a valve did not reach the commanded position after three attempts |
| `central_unavailable` | the boiler entity is unavailable, or does not follow its commands |

`valve_error` (shown by the card) carries the text of valve/boiler faults and,
for 15 minutes, a notice when a valve moved without a command and was put back.
No heating ever happens while the mode is OFF.

### Window detection

Slope-only detection, robust to 0.1 °C quantisation: a least-squares slope over
the last 20 minutes of reports (at least three reports spanning >= 5 min) must
fall below 0.8 x the configured threshold, the temperature must be >= 0.4 °C
below the window maximum, and a fall back from a short heat spike (oven,
shower) is ignored unless it goes below the pre-spike level.  The alert clears
when the temperature stops falling (slope >= -0.3 °C/h over 15 min) or after
2 hours; `dismiss_window_alert` clears it and keeps the detector quiet for 30
minutes.  A configured door/window contact sensor opens/closes the alert
directly (only when window detection is enabled, as in 1.x).

## Entities, attributes and services

**Climate entity** (unchanged entity and unique IDs).  Attributes used by the
card: `current_temperature`, `hvac_action`, `hvac_modes`, `min_temp`,
`max_temp`, `preset_mode`, `preset_modes`, `humidity_sensor`, `outdoor_sensor`,
`window_open_detected`, `window_alert`, `valve_error`, `temperature`,
`target_temp_step`, `friendly_name`, and new `fault` / `fault_message`.  Also
`manual_override` and `zone_heater_on`.  Diagnostics (model confidence,
predicted peak, planned burst, learned parameters, ...) are attributes too but
are excluded from the recorder.  The climate now belongs to the zone's device
and supports `climate.turn_on` / `turn_off`.

**Recorder footprint.**  1.x wrote the climate state every ~18 s with ~60
attributes (~115 MB/day for 9 zones).  2.0 writes it only when something
card-visible changes, plus a diagnostics refresh at most every 15 minutes.

**Sensors** (per zone): Temperature Slope and Hourly Temperature Slope
(least-squares slopes over 30/60 min of reports), Cycle On Duration, Cycle
Tail Duration, Cycle Time To Target (existing entity IDs kept), Heater Active
(binary sensor), and new diagnostic sensors *Model confidence* (%),
*Predicted peak* (°C) and *Last fault*.

**Services**

* `adaptive_thermostat.reset_manual_override` - let auto on/off act again.
* `adaptive_thermostat.dismiss_window_alert` - clear a false window alert.
* `adaptive_thermostat.reset_adaptive_profile` - forget the learned model of a
  zone (fallback control is used until it is relearned).

A logbook entry is written for every real heating start/stop, for window
detections and for corrected valve drift.

## Upgrading from 1.x and rollback

**Upgrade.** Install 2.0.0 and restart.  The stored state
(`.storage/adaptive_thermostat_runtime_state`) is migrated from version 1.1 to
1.2: mode, target, current preset, preset temperatures and manual override are
kept; the 1.x learned parameters (`thermal_state`) are discarded because they
came from an incorrect model.  Each zone starts with the fallback controller
and switches to model control by itself once confident (typically within a
day of heating).  Config entries are untouched; new options take their
defaults.

**Rollback to 1.1.6.**  HACS -> Adaptive Thermostat -> *Redownload* -> pick
1.1.6, restart.  Only the *minor* storage version changed, so 1.1.6 loads the
2.0 store as-is (it ignores the new `core` key) and keeps your mode, target and
presets; it starts its own learning from defaults.  The 2.0 options stored in
the config entry are ignored by 1.1.6.  Upgrading again later migrates as above.

## Development, tests and replay

```bash
uv python install 3.14
uv venv --python 3.14 .venv
uv pip install --python .venv "pytest-homeassistant-custom-component==0.13.374" ruff mypy  # HA 2026.10.3
.venv/bin/python -m pytest              # engine, simulator, Home Assistant and replay tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components/adaptive_thermostat
```

* `custom_components/adaptive_thermostat/engine/` - the pure-Python engine
  (model, estimator, observer, controller, boiler policy, window detector,
  sensor health, zone core).
* `tests/engine` - unit tests; `tests/sim` - closed-loop tests on a realistic
  multi-zone simulator (asymmetric water-side radiators, shared supply
  temperature, pump overrun, pipe gains, building mass, sun, cooking spikes,
  windows, a lagging report-on-change 0.1 °C sensor with heartbeats and
  drop-outs, valve travel); `tests/ha` - Home Assistant tests with a fake plant
  (valve travel, stuck and self-opening valves, unavailability).
* `tools/replay/replay.py` replays a recorder export through the same engine
  and reports prediction errors, window detections, safety incidents and the
  heating-vs-passive split of over-target time.  Point it at an export with
  `--export DIR` or `ADAPTIVE_THERMOSTAT_EXPORT`; without one it exits (the
  full-export test is skipped).  `tests/fixtures/export_mini` is a small trimmed
  fixture used by the replay tests.  Results on the 9-zone, 10-day export are
  in [docs/REPLAY.md](docs/REPLAY.md).

## Troubleshooting

* **`sensor_stale` with "No temperature update"**: the sensor has not reported
  for the stale timeout; check its battery/radio.  If your sensors only report
  on change and your rooms are very stable, raise *Stale sensor timeout*.
* **`sensor_stale` with "stuck ... while heating"**: the reading did not move
  after two heat episodes that should each have warmed the room noticeably.
  It clears by itself as soon as the reading changes.
* **`valve_mismatch` / `valve_unavailable`**: check the valve and its gateway;
  heating resumes automatically when the valve works again.
* Enable debug logging with
  `logger: logs: custom_components.adaptive_thermostat: debug`.
* Issues: <https://github.com/Horia73/adaptive_thermostat/issues>; card issues
  belong in the card repository.

## License

This project is licensed under the MIT License (no LICENSE file is committed yet).
