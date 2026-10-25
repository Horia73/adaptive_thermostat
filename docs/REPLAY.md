# Replay of the recorded export (9 zones, 15-25 Oct 2026)

Generated with `python tools/replay/replay.py --export <dir> --markdown ...` on
the private 10-day export of the live installation (HA 2026.10.3, 9 zones, two
shared boilers).  The replay feeds every zone's real `ZoneCore` (the code the
integration runs) with the recorded temperature, valve, boiler, outdoor and
mode history; the recorded actuators drive the model, the controller output
is not executed.  Metrics start after a 2-day warm-up.

How to read the tables:

* **Prediction error**: MAE in °C of the new model vs *persistence* (the
  reading stays as it is).  One-step = at each sensor report; 1/2/4 h =
  open-loop simulation with the actual future valve/boiler inputs; "4 h
  heated" = windows that contain heating.  These rooms are held near target
  by the old controller, so persistence is a strong baseline.
* **Burst peak**: predicted vs observed maximum reading after each burst
  (new model, recorded inputs) next to the *old controller's own* recorded
  predicted-vs-actual peak (`last_cycle_peak_*`).  The old figure is the old
  code's self-assessment with its own peak definition, so it is indicative
  only.  Kitchen and bathrooms have cooking/shower peaks the model cannot
  know about.
* **Over target + 0.5**: share of heat-mode time above target + 0.5 °C, split
  by a model counterfactual (same 3 h of history without the zone's own heat):
  "heating" = would not have been over target without that heat.

## Prediction error (MAE, °C; new model / persistence)

| zone | one-step | 1 h | 2 h | 4 h | 4 h heated | burst peak: new / old model |
|---|---|---|---|---|---|---|
| dormitor | 0.117 / 0.15 | 0.115 / 0.13 | 0.153 / 0.158 | 0.193 / 0.19 | 0.184 / 0.189 | 0.112 (n=88) / 0.117 (n=125) |
| hol_intrare | 0.108 / 0.13 | 0.119 / 0.1 | 0.165 / 0.156 | 0.229 / 0.238 | 0.301 / 0.312 | 0.135 (n=26) / 0.157 (n=35) |
| birou | 0.079 / 0.135 | 0.135 / 0.112 | 0.184 / 0.157 | 0.22 / 0.222 | 0.258 / 0.204 | 0.106 (n=13) / 0.107 (n=34) |
| coridor | 0.107 / 0.129 | 0.114 / 0.097 | 0.146 / 0.136 | 0.188 / 0.17 | 0.191 / 0.171 | 0.129 (n=32) / 0.098 (n=42) |
| bucatarie | 0.276 / 0.185 | 0.275 / 0.279 | 0.365 / 0.397 | 0.401 / 0.457 | 0.397 / 0.415 | 0.286 (n=33) / 0.161 (n=35) |
| living_2 | 0.101 / 0.137 | 0.115 / 0.125 | 0.165 / 0.175 | 0.262 / 0.261 | 0.256 / 0.267 | 0.115 (n=53) / 0.111 (n=72) |
| dressing | 0.168 / 0.144 | 0.16 / 0.151 | 0.225 / 0.234 | 0.308 / 0.323 | 0.553 / 0.964 | 0.216 (n=1) / 0.468 (n=6) |
| baie_1 | 0.204 / 0.155 | 0.305 / 0.272 | 0.468 / 0.428 | 0.679 / 0.658 | 0.976 / 0.997 | 0.407 (n=22) / 0.199 (n=60) |
| baie_2 | 0.117 / 0.124 | 0.131 / 0.107 | 0.179 / 0.164 | 0.221 / 0.211 | 0.269 / 0.293 | 0.293 (n=16) / 0.172 (n=18) |

## Window detection, safety, drift

| zone | windows old→new | sharp drops caught | stale-time alarms* | physics alarms | drift incidents (h open / h with boiler on) | zone on while OFF | over target+0.5 (heating / passive) |
|---|---|---|---|---|---|---|---|
| dormitor | 9→6 | 2/3 | 0 | 0 | 0 (0 / 0) | 0 (0.0 min) | 0.3% (100.0% / 0.0%) |
| hol_intrare | 0→0 | 0/0 | 0 | 0 | 0 (0 / 0) | 0 (0.0 min) | 35.5% (0.0% / 100.0%) |
| birou | 3→1 | 0/0 | 1 | 0 | 0 (0 / 0) | 0 (0.0 min) | 34.3% (5.5% / 94.5%) |
| coridor | 1→1 | 0/0 | 1 | 1 | 0 (0 / 0) | 0 (0.0 min) | 37.8% (0.0% / 100.0%) |
| bucatarie | 17→5 | 4/5 | 0 | 0 | 0 (0 / 0) | 1 (0.0 min) | 46.3% (0.4% / 99.6%) |
| living_2 | 0→0 | 0/1 | 0 | 0 | 0 (0 / 0) | 0 (0.0 min) | 11.7% (5.6% / 94.4%) |
| dressing | 8→3 | 2/3 | 0 | 0 | 0 (0 / 0) | 1 (0.0 min) | 98.8% (0.0% / 100.0%) |
| baie_1 | 8→4 | 4/4 | 0 | 0 | 0 (0 / 0) | 0 (0.0 min) | 58.4% (4.9% / 95.1%) |
| baie_2 | 0→0 | 0/0 | 0 | 0 | 1 (17.74 / 0.81) | 0 (0.0 min) | 48.2% (8.0% / 92.0%) |

\* worst case: assumes the sensors never re-report an unchanged value (with heartbeats Home Assistant's `last_reported` keeps them fresh).

## Learned parameters

| zone | q_s K/h | tau_f min | tau_e min | tau_r h | tau_am h | rho | g K/h | k K/h | param conf |
|---|---|---|---|---|---|---|---|---|---|
| dormitor | 1.827 | 3.27 | 10.4 | 49.9 | 1.01 | 5.05 | 0.125 | 0.078 | 1.0 |
| hol_intrare | 3.253 | 3.45 | 9.9 | 43.1 | 1.31 | 4.13 | 0.225 | 0.323 | 0.981 |
| birou | 2.168 | 10.35 | 13.5 | 30.0 | 1.32 | 8.93 | 0.253 | 0.064 | 0.938 |
| coridor | 1.266 | 6.83 | 8.2 | 36.5 | 0.84 | 11.17 | 0.296 | 0.229 | 0.888 |
| bucatarie | 2.528 | 4.75 | 9.2 | 27.0 | 2.74 | 5.01 | 0.44 | 0.432 | 1.0 |
| living_2 | 2.319 | 7.93 | 10.9 | 27.3 | 1.21 | 3.75 | 0.364 | 0.034 | 0.96 |
| dressing | 2.279 | 5.42 | 20.5 | 32.0 | 1.84 | 6.16 | 0.281 | 0.396 | 0.626 |
| baie_1 | 0.849 | 2.51 | 20.5 | 40.3 | 4.81 | 1.89 | 0.284 | 0.309 | 0.98 |
| baie_2 | 2.241 | 6.39 | 13.5 | 51.3 | 2.15 | 9.91 | 0.124 | 0.263 | 0.891 |

## Boilers (recorded history)

* `switch.centrala3`: {"starts": 377, "starts_per_day": 36.3, "runs_under_2min": 206, "runs_under_1min": 104, "median_run_min": 1.83, "off_gaps_under_3min": 32}
* `switch.centrala2`: {"starts": 169, "starts_per_day": 16.2, "runs_under_2min": 62, "runs_under_1min": 37, "median_run_min": 2.83, "off_gaps_under_3min": 15}

## Same replay assuming sensors re-report unchanged values (heartbeats)

With heartbeats Home Assistant keeps `last_reported` fresh, so a frozen
sensor is invisible to the time check (`--heartbeat 1800`):

| zone | stale-time alarms | physics alarms |
|---|---|---|
| birou | 0 | 1: from 18.10 19:24, held for 138.9 h (reading frozen since 18.10 16:18) |
| coridor | 0 | 1: 23.10 11:00, 0.2 h (the only false alarm in 90 zone-days) |
| all other zones | 0 | 0 |

## Incidents in the recorded history and what 2.0 does about them

* **Frozen sensor (birou)**: reading stuck at 22.9 °C for 142 h; the old
  controller delivered 11.2 h of heat (valve open x boiler on) into it.  2.0
  raises `sensor_stale` after 3.0 h (time check, sensor silent) or 3.1 h
  (physics check, sensor still reporting the frozen value) and stops heating
  until the reading moves.
* **Valve drift (baie_2)**: `valve.valva_baie_2` physically open for 17.7 h
  from 24.10 14:15 while the climate entity said idle; the boiler fired for
  other zones during 0.8 h of it.  2.0 sees the valve report `open` without a
  command and closes it within one control step (plus 18 s of travel), with a
  logbook entry and a `valve_error` notice.
* **Concurrent turn-ons**: 18 episodes in which three or more different burst
  plans were written within two minutes (dormitor 6, baie_1 4, living_2 3,
  birou 2, coridor/bucătărie/dressing 1), and 2 episodes of `zone_heater_on`
  true while the mode was off (bucătărie, dressing).  In 2.0 intents never
  start actuator work, one serialized step per zone owns all of it, and a
  single timer exists per zone (HA tests: 30 rapid set_temperature calls ->
  one open command, one boiler start, one burst).
* **Boiler short-cycling**: centrala3 started 377 times in 10 days, 206 runs
  shorter than 2 min (104 under 1 min); centrala2 169 starts, 62 under 2 min.
  2.0 enforces a minimum burst (120 s heat), boiler minimum on/off times and
  waits for the radiator tail before firing again.
* **Window detection**: 46 recorded detections (old instantaneous slope) ->
  20 with the new regression detector; 12 of 16 objectively sharp drops (>= 0.6 °C
  below the 30-min maximum, not a spike decay) are caught.  The four not
  caught are a cooking-spike decay, two moderate drops (-1.2 to -1.6 °C/h,
  below the configured 2.5 °C/h, also missed by the old detector) and a
  6-minute dip with only two reports.
* **Over-target time is passive**: 0.3-98.8 % of heat-mode time is above
  target + 0.5 °C, and in every zone except dormitor at least 92 % of it
  would have happened without the zone's own heat (neighbours, sun, cooking,
  showers, shared pipes).  Heating-caused overshoot is the controller's
  responsibility; passive gains cannot be removed without cooling.
