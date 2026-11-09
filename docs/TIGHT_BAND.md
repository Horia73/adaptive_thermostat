# Tight band in reading space (2.2)

Owner requirement: every zone's **displayed** temperature stays within
`target +/- target_tolerance` (23 +/- 0.1: readings 22.9-23.1), without new
short boiler cycles.  This document explains the 09.11 living-room incident,
what 2.2 changes, what the simulations say, and what is physically possible
per zone.  Everything here is reproducible with `tools/tightband/report.py`.

> **Data caveat.**  The analysis was made on a build host without the private
> recorder exports.  Everything that depends on the recorded history
> (BEFORE metrics, the recorded burst rise per zone, calibration of the
> simulator to the v2.1 period) is produced by
> `python tools/tightband/report.py --export <ha-export> --out <dir>` where
> the data lives.  The numbers below come from the learned models (docs/REPLAY.md,
> live living_2 parameters of 09.11), the sensors' report behaviour measured
> in the committed fixture, and closed-loop simulation.

## 1. How the sensors report

`tests/fixtures/export_mini` (birou, dormitor, baie_2, 0.1 °C sensors):

| step between reports | on the 30-min periodic grid | off the grid |
|---|---|---|
| 0.1 °C | 30 | 13 (most exactly 10 min after the previous report) |
| 0.2 °C | 1 | 30 (10-20 min after the previous report) |
| >= 0.3 °C | 0 | ~50 (1-10 min, fast changes) |

Single 0.1 °C steps are reported only with the periodic report (every 30
minutes after the last report); an unchanged periodic value reaches Home
Assistant as a *reported* event (heartbeat) and is not in the recorder.  So
"22.9 for 30 minutes" is mostly *silence*: it says the room was 22.85-22.95
when 22.9 was reported and has moved less than ~0.2 °C since.
`simulator.zigbee_sensor()` reproduces this behaviour.

## 2. Root cause of the 09.11 living_2 episode

13:54-14:24 local: reading 22.9, no heat; 14:24:38 22.8 reported; 14:24:56
burst (~2.6 min, i.e. the 120 s minimum plus valve/boiler overheads); then
predicted peak 23.02 (free response in the tail) with a ~23 min tail; 24 h
range 22.8-23.2.

v2.1 model control fired when waiting 10 minutes would let the predicted
minimum drop below

    low = target - max(deadband, min(0.25, rise / 2)),
    rise = predicted peak of the 120 s burst - predicted peak without heat.

**Finding 1 - the learned peak correction widens the oscillation by design.**
The 120 s burst's predicted peak includes `peak_correction` (median observed
minus predicted burst peak, up to +/-0.3 K), the no-heat peak does not, so
the correction is counted fully in `rise` and lowers `low` by half of it.
For the live living_2 model (cooling 0.2 K/h):

| peak correction | rise | low | peak of a minimum burst fired at low |
|---|---|---|---|
| 0.0 | 0.10 | 22.90 | 23.00 |
| 0.1 | 0.20 | 22.90 | 23.10 |
| 0.2 | 0.30 | 22.85 | 23.20 |
| 0.3 | 0.40 | 22.80 | 23.30 |

With a correction of about 0.2 K the controller deliberately centres a
22.85-23.2 oscillation on 23.0: exactly the recorded 22.8-23.2.  `low =
22.85` is the bottom edge of the 22.9 bin, so it is reached only when the
estimate leaves the bin - in practice when the next periodic report shows
22.8.  The reproduction (live living_2 model, 22.9 at minute 0, 22.8 at the
periodic report at minute 30):

| peak correction | cooling K/h | v2.1 fires at | v2.1 predicted peak | 2.2 fires at | 2.2 predicted peak |
|---|---|---|---|---|---|
| 0.0 | 0.05 | 20 min | 23.06 | 13 min | 23.07 |
| 0.0 | 0.1 | 7 min | 23.05 | 10 min | 23.05 |
| 0.0 | 0.2 | 0 min | 23.03 | 6 min | 23.02 |
| 0.2 | 0.05 | 30 min (after 22.8 shown) | 23.17 | 30 min (after 22.8 shown) | 23.17 |
| 0.2 | 0.1 | 30 min (after 22.8 shown) | 23.16 | 30 min (after 22.8 shown) | 23.16 |
| 0.2 | 0.2 | 23 min | 23.18 | 15 min | 23.20 |

So **if** living_2's learned correction is ~0.2 K (not in the recorder; the
report harness measures its proxy, the recorded rise of minimum-length
bursts), a 120 s burst lifts the *reading* by ~0.3 K - the whole 22.85-23.15
window - and +/-0.1 is physically out of reach with 2-minute bursts.  2.2
then says so (`band_feasible: false`) and centres the oscillation the same
way; the only levers are shorter heat pulses (piggyback, below) or a wider
band.

**Finding 2 - the estimate is not tied to the reported bin.**  A changed
report was a soft measurement (sigma 0.05 K, the observer treats every step
as a fresh crossing and places it at the bin edge), so after 23.0 -> 22.9
the estimate typically stayed in the upper part of the bin; between reports
only a +/-0.15 K silence band applied (the +/-0.05 K bound of the hypothesis
applies only to heartbeats, and there was none: the next periodic report
already carried 22.8).  In the simulator the estimate was on average 0.017 K
warmer than the room when bursts fired (rms 0.06 K).

**Hypothesis check.**  Trigger formula and the +/-0.05 silence bound: the
formula is right; `low` reaches 22.85 only with a correction >= ~0.2 K (the
live parameters alone give 22.90), and the +/-0.05 bound did not apply
during the hold.  `aim = target`: the burst length is the shortest with peak
>= target, so a minimum burst started at `low` peaks at `low + rise` (23.2
at a 0.2 correction).  Start delay and lookahead: both are in the
prediction; the 10-minute lookahead made v2.1 fire early rather than late
when the model is right.  `peak_correction`: offset-free correction of burst
peaks (median of the last eight validations), applied to every burst
prediction - and, as shown, also to the centring rise.

## 3. What 2.2 changes

Reading-space band for the true temperature (target 23, tolerance 0.1, 0.1
display step):

    floor   = target - tolerance - 0.05 + 0.01            = 22.86
    ceiling = target + tolerance + 0.05 - 0.01            = 23.14
    trigger = floor + min(0.06, 0.5 * sd(T)) + trigger_correction
    aim     = target

* **Trigger**: heat is requested when the predicted minimum - with the
  boiler's start delay and a 5-minute lookahead - would fall below the
  trigger.  `sd(T)` is the estimator's temperature uncertainty: it grows from
  ~0.05 to ~0.1 K over a 30-minute report gap, so the controller fires
  earlier the longer the sensor has been silent.  `trigger_correction` is
  learned per zone: +0.02 K whenever a reading below the band still appears
  after a burst the controller timed itself, -0.002 K after each clean one
  (max 0.1 K, persisted, reset with the model).
* **Feasibility**: the lift of the smallest burst is `peak - dip` of its
  predicted trajectory.  If it exceeds `ceiling - trigger` the band cannot
  be held; the trigger is moved down to centre the oscillation (both edges
  exceeded equally, never more than 0.25 K below the target).  The trigger
  no longer depends on the rise otherwise (fixes finding 1 for feasible
  zones).
* **Estimator/observer**: after each changed report the estimate is kept
  inside the reported bin (+/-0.06 K, inequality pseudo-measurement as for
  heartbeats).  Estimate minus room temperature when bursts fired, simulated
  living_2 / dormitor: +0.017 / +0.016 K -> +0.008 / 0.000 K, rms 0.059 /
  0.066 -> 0.049 / 0.054 K (finding 2).
* **Aim**: the target (the peak lands in [target, target + tolerance]).
  Aiming at target + tolerance/2 was evaluated (table below): it saves boiler
  starts but costs 2-4 points of in-band time, because peaks scatter by
  ~0.1 K and passive gains only push upwards.
* **Piggyback**: while the shared boiler fires for another zone, a zone that
  will need heat within 20 minutes fires now if even the smallest burst keeps
  the peak within 0.05 K of the aim; such bursts may be as short as 60 s
  (valve-level: the boiler is already running, its minimum run is still
  enforced by the boiler policy, so this cannot add a start or a short run).
* Unchanged: minimum off time, tail wait, emergency override (0.6 K below
  target), window/contact/linked-zone blocking, faults, the fallback
  controller.
* Attributes: `heat_trigger_below`, `aim_peak`, `min_burst_rise`,
  `band_feasible`, `trigger_correction` (climate entity, unrecorded like the
  other diagnostics); `heat_trigger_below` and `aim_peak` also on the
  recorded *Predicted peak* sensor.

## 4. Simulation

Closed loop, whole installation: 9 zones on `switch.centrala2` (birou,
dressing, hol_intrare, baie_1) and `switch.centrala3` (living_2, coridor,
bucatarie, dormitor, baie_2), 2-minute boiler minimum run and 3-minute
minimum off time, the real engine of each version (2.1.0 =
`9e505dd`, extracted from git, vs this branch), rooms derived from the
learned models (radiator water side with a shared, ramping supply
temperature, building mass, pipe gains, sun, cooking/shower spikes), the
sensors' measured report behaviour with 0.1 °C quantisation, estimators
seeded with the learned models.  Metrics: displayed reading during heat-mode
time, from day 2; 3 seeds x 7 days per cell.

Scenarios (no recorder data here, see the caveat):

* `ideal` - rooms behave like their models, small unmodelled gains (0.1 K/h).
* `field_like` - bursts lift the room twice as much as the models' q_s says
  at first, unmodelled gains 0.3 K/h: this reproduces living_2's recorded
  signature (minimum-length bursts, readings 22.8-23.2, heat only after 22.8
  shows) and centrala3 at ~40 starts/day (recorded ~41).
* `model_underpredicts` - the models keep under-predicting burst peaks
  (parameters frozen, real radiators 2.5x stronger): learned peak
  corrections of 0.1-0.2 K, as suspected for living_2.

Mean of the 9 zones (reading space):

| scenario | controller | in +/-0.1 % | in +/-0.2 % | over % (heating-caused) | under % | late fires / fires | starts/day centrala2 / centrala3 | runs < 2 min |
|---|---|---|---|---|---|---|---|---|
| ideal | 2.1 | 83.2 | 91.3 | 15.6 (1.5) | 1.2 | 50 / 721 | 38.4 / 48.8 | 0 |
| ideal | 2.2 | 82.7 | 90.8 | 16.3 (1.8) | 1.0 | 38 / 780 | 30.9 / 36.2 | 0 |
| field_like | 2.1 | 53.6 | 70.5 | 42.7 (4.5) | 3.7 | 168 / 503 | 30.4 / 39.8 | 0 |
| field_like | 2.2 | 53.1 | 70.4 | 43.1 (4.7) | 3.8 | 148 / 542 | 28.0 / 39.6 | 0 |
| model_underpredicts | 2.1 | 73.2 | 88.3 | 22.7 (4.7) | 4.1 | 152 / 465 | 29.2 / 36.6 | 0 |
| model_underpredicts | 2.2 | 74.1 | 89.0 | 22.3 (4.9) | 3.6 | 113 / 484 | 25.9 / 36.1 | 0 |

Per zone tables: `tight_band_report.md` produced by the harness.  What the
numbers say:

* **Time in band does not change materially** (+/-1 point on average; per zone
  -4.6 to +6.1 points: dormitor loses 3.5-4.6 points in two scenarios and
  birou 4.0 in `field_like`; bucatarie, hol_intrare and dressing gain).  The controller can only trade undershoot
  against heating-caused overshoot; v2.1 already sat close to the best trade
  when its model was right.
* **Fewer late fires** (heat only after the reading left the band): -12 to
  -26 %; less undershoot in 17 of 27 zone/scenario cells.
* **Fewer boiler starts**: -20/-26 % (ideal), -8/-0.5 % (field_like),
  -11/-1 % (model_underpredicts); never a run under 2 minutes.
* **Most time outside the band is passive** (sun, neighbours, cooking, the
  shared pipes: hol_intrare, bucatarie, dressing and baie_1 are above the
  band 45-71 % of the time in `field_like` with only 3-5 % caused by their
  own heat, matching the replay of the old export where >= 92 % of the
  over-target time was passive).  No heating controller can remove it.

Design choices tested on the same harness (4 seeds, `field_like` / `ideal`;
variants of the first design, whose piggyback allowed the smallest burst up
to the ceiling):

| variant | in +/-0.1 % | under % | starts/day (both boilers) |
|---|---|---|---|
| 2.1 | 54.2 / 83.9 | 3.9 / 1.2 | 34.7 / 43.9 |
| aim = target (chosen) | 53.8 / 82.5 | 3.6 / 1.0 | 32.0 / 33.4 |
| aim = target + tolerance/4 | 52.7 / 81.9 | 3.4 / 1.0 | 31.2 / 31.9 |
| aim = target + tolerance/2 | 51.4 / 79.5 | 3.0 / 0.8 | 29.2 / 28.7 |
| + 60 s piggyback bursts | 53.2 / 83.4 | 3.8 / 1.0 | 30.4 / 33.5 |
| no piggyback | 53.0 / 82.3 | 3.6 / 1.1 | 35.8 / 43.2 |

Unrestricted piggyback (smallest burst allowed up to the ceiling) cost
living_2 and dormitor ~3 points of in-band time in `field_like`; limiting it
to peaks within 0.05 K of the aim restored them (living_2 63.4 vs 62.3 % for
2.1) while keeping most of the start reduction.  Short (60 s) piggyback
bursts are band-neutral and save 0-8 % of starts; they are enabled because
they are the only way to give a zone less heat than a 2-minute burst without
a boiler start.

## 5. Feasibility of +/-0.1 per zone

The reading stays inside 23 +/- 0.1 while the room is 22.85-23.15 (0.3 K).
The smallest burst's lift plus the estimate uncertainty at both ends
(+/-0.05 K) must fit: lift <= 0.2 K.

| zone | model lift, 120 s | model lift, 60 s (piggyback) | simulated reading lift, field_like | +/-0.1 | realistic band |
|---|---|---|---|---|---|
| birou | 0.03 | 0.01 | 0.10 | yes (heating side) | +/-0.1 |
| coridor | 0.01 | 0.00 | 0.10 | yes | +/-0.1 |
| baie_1 | 0.06 | 0.03 | 0.13 | heating side yes, passive gains dominate | +/-0.1 below, not above |
| living_2 | 0.10 | 0.06 | 0.20 | borderline; **no if its peak correction is ~0.2** (reading lift ~0.3) | +/-0.15 |
| dormitor | 0.09 | 0.06 | 0.20 | borderline | +/-0.1 to +/-0.15 |
| bucatarie | 0.09 | 0.06 | 0.20 | borderline, cooking gains dominate | +/-0.15 |
| baie_2 | 0.07 | 0.04 | 0.23 | no | +/-0.15 |
| dressing | 0.13 | 0.08 | 0.30 | no | +/-0.15 to +/-0.2 |
| hol_intrare | 0.18 | 0.13 | 0.40 | no | +/-0.2 |

"Model lift" is from the learned models (docs/REPLAY.md; living_2 live
09.11) at 0.2 K/h cooling; the simulated reading lift is quantised to 0.1 K
and includes the field_like assumption of radiators twice as strong as their
models.  The decisive number per zone is the **recorded** reading lift of
minimum-length bursts; the harness reports it from the export (`recorded rise
(min bursts)`) and uses the largest of the three for the verdict.

Plainly: +/-0.1 on the display is achievable on the heating side in birou and
coridor, borderline in living_2, dormitor, bucatarie and baie_1, and not with
2-minute bursts in baie_2, dressing and hol_intrare.  Above the band,
hol_intrare, bucatarie, dressing and the bathrooms are dominated by passive
gains in every scenario.

## 6. Running the harness on the recorded data

```bash
python tools/tightband/report.py \
    --export <run>/ha-export \
    --old-export <old run>/ha-export \
    --out <reports dir> --workers 4 --seeds 2
```

It writes `tight_band_report.md/json`: BEFORE (recorded, whole export and the
v2.1 period from 07.11 15:57:41 UTC), calibration residuals (per zone the
radiator scale and unmodelled-gain level from a 5x4 grid that make the
simulated 2.1 controller reproduce the recorded in-band time, under/over
time, bursts per day and burst lift), SIM 2.1 vs SIM 2.2 on the calibrated
rooms driven by the recorded outdoor temperature, targets, modes and window
contacts, the feasibility table with the recorded lift, and the recorded
living_2 readings of the last three hours.  About 24 whole-installation runs
of ~10 days; ~15 minutes with 4 workers.
`python tools/tightband/synth_export.py --out DIR` writes a synthetic export
in the same format (used to test the harness end to end).

## 7. Residual risks

* Not validated on the recorded data here; calibration quality and the
  recorded lift per zone decide the final verdict.
* The trigger correction adapts on reading-space outcomes; in zones where
  passive drops (windows without contacts, drafts) cause readings below the
  band it will drift up to its 0.1 K cap and fire earlier than needed.
* 60 s piggyback bursts open a valve for ~80 s; on valves with slower travel
  than the 18 s seen here the delivered heat is smaller than modelled (the
  validation/peak correction absorbs it over time).
* The feasibility flag uses the corrected rise; a large peak correction that
  is really a sensor artefact (radiator plume on the sensor) makes the band
  look infeasible and lowers the trigger, as in 2.1.
