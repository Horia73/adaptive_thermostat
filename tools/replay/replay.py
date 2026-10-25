"""Replay recorded Home Assistant history through the Adaptive Thermostat engine.

For every zone the *real* :class:`engine.zone.ZoneCore` (the same code the
integration runs: estimator, learning gates, window detector, sensor health)
is fed with the zone's recorded temperature, valve, boiler, outdoor and mode
history.  Its control output is not executed (``control=False``); the
recorded actuators drive the model.  The harness reports:

* one-step prediction error at each sensor report vs persistence;
* open-loop 1/2/4 h prediction error with the actual future inputs vs
  persistence (all windows, and windows containing heat);
* burst-level peak prediction (new model vs the old controller's own
  predicted peak recorded in the export);
* window detections: old (recorded) vs new, and recall on objectively sharp
  drops;
* stale-sensor alarms (time and physics) and actuator-drift / race incidents
  in the recorded history that the new logic addresses;
* boiler short-cycling in the recorded history;
* how much over-target time was caused by heating vs passive gains (model
  counterfactual: same history without the zone's own heat).

Usage::

    python tools/replay/replay.py [--export DIR] [--zones a,b] [--json out.json] [--markdown out.md]

Without an export (``$ADAPTIVE_THERMOSTAT_EXPORT`` or ``--export``) it exits
cleanly; the tests skip.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "custom_components" / "adaptive_thermostat"))

from export_loader import Export, ZoneHistory, find_export, load_export, value_at  # noqa: E402

from engine.estimator import PARAM_INDEXES, EstimatorConfig, params_from_logs  # noqa: E402
from engine.model import SECONDS_PER_HOUR, ThermalParams, step  # noqa: E402
from engine.observer import ObserverConfig, effective_input_timeline  # noqa: E402
from engine.safety import SafetyConfig  # noqa: E402
from engine.window import WindowConfig  # noqa: E402
from engine.zone import ZoneConfig, ZoneCore  # noqa: E402

VALVE_OPEN_STATES = ("open", "closing")
UNAVAILABLE = ("unavailable", "unknown")
HORIZONS_H = (1.0, 2.0, 4.0)
TICK_S = 120.0
WARMUP_S = 2 * 86400.0
SNAPSHOT_S = 900.0
ATTRIBUTION_LOOKBACK_S = 3 * 3600.0
OVERRUN_GRACE_S = 180.0  # old system: valves legitimately open this long after zone off
LOCAL_TZ = timezone(timedelta(hours=3))


@dataclass
class ReplaySettings:
    """Knobs of a replay run."""

    estimator: EstimatorConfig = field(default_factory=EstimatorConfig)
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    forecast_every_s: float = 1800.0
    warmup_s: float = WARMUP_S
    # Model sensors that re-report an unchanged value every 30 min (Home
    # Assistant then updates last_reported): the time check cannot fire and
    # only the physics check can catch a frozen sensor.
    heartbeat_s: float | None = None


@dataclass
class Snapshot:
    """Estimator state at a point in time."""

    t: float
    x: list[float]
    t_out: float


@dataclass
class ZoneReplay:
    """Results for one zone."""

    name: str
    one_step: list[tuple[float, float, float, bool]] = field(default_factory=list)
    forecasts: dict[float, list[tuple[float, float, float, bool]]] = field(default_factory=dict)
    window_events: list[dict[str, Any]] = field(default_factory=list)
    stale_events: list[dict[str, Any]] = field(default_factory=list)
    burst_snapshots: list[tuple[float, Snapshot]] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    max_expected_rise: float = 0.0
    final: dict[str, Any] = field(default_factory=dict)
    u_timeline: list[tuple[float, float]] = field(default_factory=list)
    eff_timeline: list[tuple[float, float, float]] = field(default_factory=list)


# ---------------------------------------------------------------- inputs
def actuator_events(zone: ZoneHistory) -> list[tuple[float, float, bool]]:
    """``[(t, valve_open_fraction, boiler_on)]`` at every actuator change."""
    changes: list[tuple[float, str, Any]] = []
    for vid, series in zone.valve_states.items():
        for t, s in series:
            changes.append((t, "valve", (vid, s)))
    for t, s in zone.boiler_states:
        changes.append((t, "boiler", s))
    changes.sort(key=lambda c: c[0])
    open_by_valve = {v: False for v in zone.valves}
    boiler_on = zone.boiler is None
    out: list[tuple[float, float, bool]] = []
    for t, kind, val in changes:
        if kind == "valve":
            vid, state = val
            if state in UNAVAILABLE:
                continue  # an unavailable actuator keeps its last physical position
            open_by_valve[vid] = state in VALVE_OPEN_STATES
        else:
            if val in UNAVAILABLE:
                continue
            boiler_on = val == "on"
        out.append((t, sum(open_by_valve.values()) / max(1, len(open_by_valve)), boiler_on))
    return out


def heat_input_timeline(zone: ZoneHistory) -> list[tuple[float, float]]:
    """Raw heat input u(t) = valve open fraction x boiler on (change points)."""
    timeline: list[tuple[float, float]] = []
    for t, frac, boiler in actuator_events(zone):
        u = frac * (1.0 if boiler else 0.0)
        if not timeline or abs(timeline[-1][1] - u) > 1e-9:
            timeline.append((t, u))
    return timeline


def simulate(
    x: list[float],
    t_out: float,
    t0: float,
    t1: float,
    inputs: list[tuple[float, float, float]],
    *,
    own_heat: bool = True,
    track_max: bool = False,
) -> float:
    """Open-loop model prediction from snapshot ``x`` at ``t0`` to ``t1``.

    Returns the temperature at ``t1`` (or the maximum on the way).
    """
    params: ThermalParams = params_from_logs([x[i] for i in PARAM_INDEXES])
    temp, tm, q, d, g, k = x[0], x[1], x[2], x[3], x[4], x[5]
    lo, hi = 0, len(inputs)
    while lo < hi:
        mid = (lo + hi) // 2
        if inputs[mid][0] <= t0:
            lo = mid + 1
        else:
            hi = mid
    idx = lo
    u, b = (inputs[idx - 1][1], inputs[idx - 1][2]) if idx else (0.0, 0.0)
    cur = t0
    best = temp
    while cur < t1:
        nxt = min(t1, cur + 300.0) if track_max else t1
        if idx < len(inputs) and inputs[idx][0] < nxt:
            nxt = inputs[idx][0]
        temp, tm, q, d = step(
            temp,
            tm,
            q,
            d,
            u if own_heat else 0.0,
            t_out,
            params,
            (nxt - cur) / SECONDS_PER_HOUR,
            base=g + k * b,
        )
        best = max(best, temp)
        cur = nxt
        if idx < len(inputs) and inputs[idx][0] <= cur:
            u, b = inputs[idx][1], inputs[idx][2]
            idx += 1
    return best if track_max else temp


# ---------------------------------------------------------------- replay
def zone_config(zone: ZoneHistory, settings: ReplaySettings) -> ZoneConfig:
    cfg = zone.config
    return ZoneConfig(
        window_detection=bool(cfg.get("window_detection_enabled", True)),
        observer=settings.observer,
        estimator=settings.estimator,
        window=WindowConfig(slope_threshold=float(cfg.get("window_slope_threshold") or 2.5)),
        safety=settings.safety,
    )


def replay_zone(zone: ZoneHistory, settings: ReplaySettings | None = None) -> ZoneReplay:
    """Run the zone core over one zone's history."""
    settings = settings or ReplaySettings()
    result = ZoneReplay(name=zone.name, forecasts={h: [] for h in HORIZONS_H})
    result.u_timeline = heat_input_timeline(zone)
    acts = actuator_events(zone)
    result.eff_timeline = effective_input_timeline(acts, settings.observer)
    events: list[tuple[float, int, str, Any]] = []
    for t, v in zone.temps:
        events.append((t, 3, "temp", v))
    if settings.heartbeat_s and zone.temps:
        data_end = max(
            zone.temps[-1][0],
            zone.climate_rows[-1]["t"] if zone.climate_rows else 0.0,
            acts[-1][0] if acts else 0.0,
        )
        for (ta, va), (tb, _vb) in zip(zone.temps, [*zone.temps[1:], (data_end, None)]):
            if va is None:
                continue
            hb = ta + settings.heartbeat_s
            while hb < tb:
                events.append((hb, 3, "heartbeat", va))
                hb += settings.heartbeat_s
    for t, frac, boiler in acts:
        events.append((t, 1, "act", (frac, boiler)))
    for t, v in zone.outdoor:
        events.append((t, 0, "outdoor", v))
    for row in zone.climate_rows:
        events.append((row["t"], 2, "climate", row))
    events.sort(key=lambda e: (e[0], e[1]))
    if not events:
        return result
    start = events[0][0]
    t_first_temp = zone.temps[0][0] if zone.temps else start
    core = ZoneCore(zone_config(zone, settings), now=start)
    est = core.estimator
    change_times = [(t, t) for t, _ in zone.temps]
    state: dict[str, Any] = {
        "next_forecast": t_first_temp + settings.warmup_s,
        "next_snapshot": start,
        "window_open": False,
        "stale_open": None,
    }

    def snapshot(now: float) -> Snapshot:
        return Snapshot(now, list(est.x), est.t_out)

    def forecast(now: float) -> None:
        if not est.initialized or core.observer.displayed is None:
            return
        base = core.observer.displayed
        x = list(est.x)
        for h in HORIZONS_H:
            t1 = now + h * SECONDS_PER_HOUR
            actual = value_at(zone.temps, t1)
            if actual is None or t1 > zone.temps[-1][0]:
                continue
            last_change = value_at(change_times, t1)
            if last_change is not None and t1 - last_change > settings.safety.stale_timeout_s:
                continue
            pred = simulate(x, est.t_out, now, t1, result.eff_timeline)
            heated = value_at(result.u_timeline, now, 0.0) > 0 or any(
                now <= t <= t1 and u > 0 for t, u in result.u_timeline
            )
            result.forecasts[h].append((now, pred - actual, base - actual, heated))

    def tick(now: float) -> None:
        status = core.update(now, control=False)
        if status.window_open != state["window_open"]:
            if status.window_open:
                ev = status.window_event
                result.window_events.append(
                    {
                        "opened": now,
                        "slope": round(ev.slope_per_hour, 2) if ev else None,
                        "drop": round(ev.drop, 2) if ev else None,
                    }
                )
            elif result.window_events:
                result.window_events[-1]["closed"] = now
            state["window_open"] = status.window_open
        stale_open = state["stale_open"]
        if status.fault == "sensor_stale" and stale_open is None:
            state["stale_open"] = {
                "start": now,
                "kind": "physics" if core.health.physics_stale else "time",
                "message": status.fault_message,
                "last_change": core.health.last_change_t,
            }
            result.stale_events.append(state["stale_open"])
        elif stale_open is not None and status.fault != "sensor_stale":
            stale_open["end"] = now
            state["stale_open"] = None
        if not core.health.physics_stale and core.health.unanswered:
            result.max_expected_rise = max(result.max_expected_rise, float(core.health.unanswered))
        if now >= state["next_snapshot"] and est.initialized:
            result.snapshots.append(snapshot(now))
            state["next_snapshot"] = now + SNAPSHOT_S
        if now >= state["next_forecast"]:
            state["next_forecast"] = now + settings.forecast_every_s
            if status.fault is None:
                forecast(now)

    last_t = start
    last_u = 0.0
    for t, _prio, kind, val in events:
        while last_t + TICK_S < t:
            last_t += TICK_S
            tick(last_t)
        if kind == "temp":
            if (
                val is not None
                and est.initialized
                and core.observer.displayed is not None
                and abs(val - core.observer.displayed) > 1e-6
                and core.health.fault(t) is None
            ):
                est.advance(t, learn_params=core.observer.learn_params)
                result.one_step.append(
                    (
                        t,
                        est.temperature - val,
                        core.observer.displayed - val,
                        t - t_first_temp >= settings.warmup_s,
                    )
                )
            core.on_temperature(t, val)
        elif kind == "heartbeat":
            core.on_temperature(t, val, reported_t=t)
        elif kind == "act":
            frac, boiler = val
            u = frac * (1.0 if boiler else 0.0)
            core.on_actuators(t, frac, boiler)
            if u > 0 and last_u <= 0 and est.initialized:
                result.burst_snapshots.append((t, snapshot(t)))
            last_u = u
        elif kind == "outdoor":
            core.on_outdoor(t, val if val is not None else value_at(zone.outdoor_backup, t))
        elif kind == "climate":
            core.set_hvac(t, val.get("state") == "heat")
            target = val.get("temperature")
            if isinstance(target, (int, float)):
                core.set_target(float(target))

    prm = est.params
    result.final = {
        "q_s": round(prm.q_s, 3),
        "tau_f_min": round(prm.tau_f * 60, 2),
        "tau_e_min": round(prm.tau_e * 60, 1),
        "tau_r_h": round(prm.tau_r, 1),
        "tau_am_h": round(prm.tau_am, 2),
        "rho": round(prm.rho, 2),
        "lumped_tau_h": round(prm.lumped_tau_h, 1),
        "q_max": round(prm.q_max, 3),
        "steady_rise_K": round(prm.steady_rise, 1),
        "base_gain": round(est.base_gain, 3),
        "shared_gain": round(est.shared_gain, 3),
        "confidence_param": est.confidence(),
        "bursts_learned": est.stats.bursts_learned,
        "heat_hours_learned": round(est.stats.heat_hours_learned, 2),
        "resets": est.stats.resets,
        "param_sd": {k: round(v, 3) for k, v in est.param_sd().items()},
    }
    return result


# -------------------------------------------------------------- analyses
def _mae(values: list[float]) -> float:
    return sum(abs(v) for v in values) / len(values) if values else float("nan")


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(value, digits)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d.%m %H:%M")


def merged_bursts(u_timeline: list[tuple[float, float]], merge_s: float = 600.0) -> list[tuple[float, float]]:
    raw: list[tuple[float, float]] = []
    on: float | None = None
    for t, u in u_timeline:
        if u > 0 and on is None:
            on = t
        elif u <= 0 and on is not None:
            raw.append((on, t))
            on = None
    merged: list[tuple[float, float]] = []
    for b in raw:
        if merged and b[0] - merged[-1][1] < merge_s:
            merged[-1] = (merged[-1][0], b[1])
        else:
            merged.append(b)
    return merged


def burst_prediction(zone: ZoneHistory, res: ZoneReplay, start_after: float) -> dict[str, Any]:
    """Peak after each burst: new model vs the old controller's own prediction."""
    bursts = merged_bursts(res.u_timeline)
    snaps = {round(t): s for t, s in res.burst_snapshots}
    errors_new: list[float] = []
    errors_naive: list[float] = []
    for i, (b0, b1) in enumerate(bursts):
        if b0 < start_after:
            continue
        end = b1 + 2 * 3600.0
        if i + 1 < len(bursts):
            end = min(end, bursts[i + 1][0])
        snap = snaps.get(round(b0))
        start_val = value_at(zone.temps, b0)
        readings = [v for t, v in zone.temps if b0 < t <= end and v is not None]
        if snap is None or start_val is None or not readings:
            continue
        if any(e["opened"] < end and e.get("closed", e["opened"]) > b0 for e in res.window_events):
            continue
        if any(e["start"] < end and e.get("end", 1e18) > b0 for e in res.stale_events):
            continue
        observed = max(*readings, start_val)
        predicted = simulate(snap.x, snap.t_out, b0, end, res.eff_timeline, track_max=True)
        errors_new.append(predicted - observed)
        errors_naive.append(start_val - observed)
    # The old controller logged its predicted vs actual peak per cycle.
    old: list[float] = []
    seen: set[tuple[Any, Any]] = set()
    for row in zone.climate_rows:
        if row["t"] < start_after:
            continue
        key = (row.get("last_cycle_peak_predicted"), row.get("last_cycle_peak_actual"))
        if None in key or key in seen:
            continue
        seen.add(key)
        old.append(float(key[0]) - float(key[1]))
    return {
        "bursts": len(errors_new),
        "new_model_peak_mae": _round(_mae(errors_new)),
        "new_model_peak_bias": _round(sum(errors_new) / len(errors_new)) if errors_new else None,
        "no_rise_baseline_mae": _round(_mae(errors_naive)),
        "old_model_cycles": len(old),
        "old_model_peak_mae": _round(_mae(old)),
    }


def window_analysis(zone: ZoneHistory, res: ZoneReplay) -> dict[str, Any]:
    old_events = 0
    prev = False
    for row in zone.climate_rows:
        flag = bool(row.get("window_open_detected"))
        if flag and not prev:
            old_events += 1
        prev = flag
    # Objectively sharp drops: >= 0.6 K below the 30 min maximum and not the
    # decay of a short spike (no >= 0.4 K rise in the hour before that max).
    temps = [(t, v) for t, v in zone.temps if v is not None]
    drops: list[float] = []
    for i, (t, v) in enumerate(temps):
        window = [(tt, vv) for tt, vv in temps[max(0, i - 30) : i] if t - tt <= 1800]
        if not window:
            continue
        t_max, v_max = max(window, key=lambda p: p[1])
        if v_max - v < 0.6:
            continue
        before = [vv for tt, vv in temps[max(0, i - 60) : i] if t_max - 3600 <= tt <= t_max]
        if before and v_max - min(before) >= 0.4:
            continue
        if drops and t - drops[-1] < 3600:
            continue
        drops.append(t)
    # Caught: a detection was active at some point from 30 min before to
    # 15 min after the drop.
    caught = sum(
        1
        for t in drops
        if any(
            e["opened"] <= t + 900 and e.get("closed", float("inf")) >= t - 1800 for e in res.window_events
        )
    )
    return {
        "old_detections": old_events,
        "new_detections": len(res.window_events),
        "sharp_drops": len(drops),
        "sharp_drops_caught": caught,
        "new_events": [
            {"at": _iso(e["opened"]), "slope": e["slope"], "drop": e["drop"]} for e in res.window_events
        ],
    }


def drift_analysis(zone: ZoneHistory) -> dict[str, Any]:
    """Valves physically open while the old climate entity said the zone was off."""
    zone_on = [(row["t"], bool(row.get("zone_heater_on"))) for row in zone.climate_rows]
    if not zone_on:
        return {"incidents": 0, "open_hours": 0.0, "heat_hours": 0.0, "longest_h": 0.0, "details": []}
    # Times from which an open valve is unexpected: zone off for longer than the overrun grace.
    off_from: list[tuple[float, float | None]] = []
    prev_on = zone_on[0][1]
    for t, on in zone_on:
        if prev_on and not on:
            off_from.append((t, t + OVERRUN_GRACE_S))
        elif on:
            off_from.append((t, None))
        prev_on = on
    end_t = zone.temps[-1][0] if zone.temps else zone_on[-1][0]
    incidents: list[dict[str, Any]] = []
    for valve, series in zone.valve_states.items():
        opened: float | None = None
        for t, s in [*series, (end_t, "closed")]:
            if s in UNAVAILABLE:
                continue
            is_open = s in ("open", "closing", "opening")
            if is_open and opened is None:
                opened = t
            elif not is_open and opened is not None:
                bad = _unexpected_open_seconds(opened, t, zone_on, off_from)
                if bad[0] >= 300.0 and bad[1] is not None:
                    incidents.append(
                        {
                            "valve": valve,
                            "from": _iso(bad[1]),
                            "hours": round(bad[0] / 3600.0, 2),
                            "boiler_on_hours": round(_overlap_on(zone.boiler_states, bad[1], t) / 3600.0, 2),
                        }
                    )
                opened = None
    return {
        "incidents": len(incidents),
        "open_hours": round(sum(i["hours"] for i in incidents), 2),
        "heat_hours": round(sum(i["boiler_on_hours"] for i in incidents), 2),
        "longest_h": round(max((i["hours"] for i in incidents), default=0.0), 2),
        "details": sorted(incidents, key=lambda i: -i["hours"])[:3],
    }


def _unexpected_open_seconds(
    t0: float,
    t1: float,
    zone_on: list[tuple[float, bool]],
    off_from: list[tuple[float, float | None]],
) -> tuple[float, float | None]:
    """Seconds of [t0, t1] where the zone was off past the overrun grace."""
    bad = 0.0
    first: float | None = None
    marks = sorted({t0, t1, *(t for t, _ in zone_on if t0 < t < t1)})
    for a, b in zip(marks, marks[1:]):
        if value_at(zone_on, a, False):
            continue
        grace = value_at(off_from, a, None)
        begin = max(a, grace if grace is not None else a)
        if b > begin:
            bad += b - begin
            first = begin if first is None else first
    return bad, first


def _overlap_on(series: list[tuple[float, str]], t0: float, t1: float) -> float:
    total = 0.0
    for (ta, sa), (tb, _sb) in zip(series, [*series[1:], (t1, "off")]):
        lo, hi = max(ta, t0), min(tb, t1)
        if hi > lo and sa == "on":
            total += hi - lo
    return total


def race_analysis(zone: ZoneHistory) -> dict[str, Any]:
    """zone_heater_on True while the mode is off, and concurrent turn-on plans."""
    episodes = 0
    seconds = 0.0
    prev_t: float | None = None
    prev_bad = False
    for row in zone.climate_rows:
        bad = row.get("state") == "off" and bool(row.get("zone_heater_on"))
        if prev_bad and prev_t is not None:
            seconds += row["t"] - prev_t
        if bad and not prev_bad:
            episodes += 1
        prev_t, prev_bad = row["t"], bad
    plans = [r for r in zone.climate_rows if r.get("planned_zone_on_duration")]
    concurrent = 0
    i = 0
    while i < len(plans):
        j = i
        values = set()
        while j < len(plans) and plans[j]["t"] - plans[i]["t"] <= 120.0:
            values.add(round(float(plans[j]["planned_zone_on_duration"])))
            j += 1
        if len(values) >= 3:
            concurrent += 1
            i = j
        else:
            i += 1
    return {
        "zone_on_while_off_episodes": episodes,
        "zone_on_while_off_minutes": round(seconds / 60.0, 1),
        "concurrent_plan_episodes": concurrent,
    }


def boiler_cycling(series: list[tuple[float, str]]) -> dict[str, Any]:
    clean = [(t, s) for t, s in series if s in ("on", "off")]
    runs: list[float] = []
    offs: list[float] = []
    for (ta, sa), (tb, _sb) in zip(clean, clean[1:]):
        (runs if sa == "on" else offs).append(tb - ta)
    days = (clean[-1][0] - clean[0][0]) / 86400.0 if len(clean) > 1 else 1.0
    return {
        "starts": len(runs),
        "starts_per_day": round(len(runs) / days, 1),
        "runs_under_2min": sum(1 for r in runs if r < 120),
        "runs_under_1min": sum(1 for r in runs if r < 60),
        "median_run_min": round(sorted(runs)[len(runs) // 2] / 60.0, 2) if runs else None,
        "off_gaps_under_3min": sum(1 for o in offs if o < 180),
    }


def overshoot_attribution(zone: ZoneHistory, res: ZoneReplay, start_after: float) -> dict[str, Any]:
    """Over-target time (> target + 0.5 K) split into heating-caused vs passive."""
    snaps = res.snapshots
    if not snaps:
        return {}
    rows = zone.climate_rows
    heat_mode = over = heating = 0
    idx = 0
    for snap in snaps:
        t = snap.t
        if t < start_after:
            continue
        row = _row_at(rows, t)
        if row is None or row.get("state") != "heat":
            continue
        target = row.get("temperature")
        reading = value_at(zone.temps, t)
        if not isinstance(target, (int, float)) or reading is None:
            continue
        heat_mode += 1
        if reading <= target + 0.5:
            continue
        over += 1
        while idx + 1 < len(snaps) and snaps[idx + 1].t <= t - ATTRIBUTION_LOOKBACK_S:
            idx += 1
        ref = snaps[idx]
        if ref.t > t - ATTRIBUTION_LOOKBACK_S + SNAPSHOT_S:
            continue
        with_heat = simulate(ref.x, ref.t_out, ref.t, t, res.eff_timeline)
        without = simulate(ref.x, ref.t_out, ref.t, t, res.eff_timeline, own_heat=False)
        if reading - (with_heat - without) <= target + 0.5:
            heating += 1
    return {
        "heat_mode_samples": heat_mode,
        "over_target_pct": round(100.0 * over / heat_mode, 1) if heat_mode else None,
        "heating_caused_pct_of_over": round(100.0 * heating / over, 1) if over else None,
        "passive_pct_of_over": round(100.0 * (over - heating) / over, 1) if over else None,
    }


def _row_at(rows: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid]["t"] <= t:
            lo = mid + 1
        else:
            hi = mid
    return rows[lo - 1] if lo else None


def stale_analysis(zone: ZoneHistory, res: ZoneReplay) -> dict[str, Any]:
    time_alarms = [e for e in res.stale_events if e["kind"] == "time"]
    physics = [e for e in res.stale_events if e["kind"] == "physics"]
    out: dict[str, Any] = {
        "time_alarms": len(time_alarms),
        "time_alarm_hours": round(
            sum((e.get("end", e["start"]) - e["start"]) / 3600 for e in time_alarms), 1
        ),
        "physics_alarms": len(physics),
        "physics_alarm_details": [
            {
                "from": _iso(e["start"]),
                "reading_unchanged_since": _iso(e.get("last_change")),
                "lasted_h": round((e.get("end", e["start"]) - e["start"]) / 3600.0, 1),
            }
            for e in physics[:5]
        ],
        "max_unanswered_episodes_healthy": int(res.max_expected_rise),
    }
    # Longest stretch without a numeric change (ends at the next change, the
    # sensor going unavailable, or the end of the data).
    longest = (0.0, 0.0, 0.0)
    temps = zone.temps
    end_of_data = max(
        zone.climate_rows[-1]["t"] if zone.climate_rows else 0.0, temps[-1][0] if temps else 0.0
    )
    for i, (ta, va) in enumerate(temps):
        if va is None:
            continue
        tb = temps[i + 1][0] if i + 1 < len(temps) else end_of_data
        if tb - ta > longest[0]:
            longest = (tb - ta, ta, tb)
    if longest[0] > 12 * 3600:
        _, t0, t1 = longest
        heat = 0.0
        tl = res.u_timeline
        for (ta, ua), (tb, _ub) in zip(tl, [*tl[1:], (t1, 0.0)]):
            lo, hi = max(ta, t0), min(tb, t1)
            if hi > lo and ua > 0:
                heat += (hi - lo) * ua
        detections = [e for e in res.stale_events if t0 <= e["start"] <= t1]
        out["longest_frozen"] = {
            "from": _iso(t0),
            "hours": round((t1 - t0) / 3600.0, 1),
            "old_system_heat_hours": round(heat / 3600.0, 1),
            "new_detection_after_h": round((detections[0]["start"] - t0) / 3600.0, 2) if detections else None,
            "new_detection_kind": detections[0]["kind"] if detections else None,
        }
    return out


def summarize(zone: ZoneHistory, res: ZoneReplay, start_after: float) -> dict[str, Any]:
    """All metrics for one zone."""
    one = [r for r in res.one_step if r[3]]
    out: dict[str, Any] = {
        "one_step": {
            "n": len(one),
            "model_mae": _round(_mae([r[1] for r in one])),
            "naive_mae": _round(_mae([r[2] for r in one])),
        }
    }
    for h, rows in res.forecasts.items():
        for label, subset in (("all", rows), ("heated", [r for r in rows if r[3]])):
            out.setdefault(f"{h:g}h", {})[label] = {
                "n": len(subset),
                "model_mae": _round(_mae([r[1] for r in subset])),
                "naive_mae": _round(_mae([r[2] for r in subset])),
            }
    out["bursts"] = burst_prediction(zone, res, start_after)
    out["window"] = window_analysis(zone, res)
    out["stale"] = stale_analysis(zone, res)
    out["drift"] = drift_analysis(zone)
    out["race"] = race_analysis(zone)
    out["overshoot"] = overshoot_attribution(zone, res, start_after)
    out["params"] = res.final
    return out


def run(
    export: Export, zones: set[str] | None = None, settings: ReplaySettings | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {"zones": {}, "boilers": {}}
    for zone in export.zones.values():
        if zones and zone.name not in zones:
            continue
        t0 = time.time()
        res = replay_zone(zone, settings)
        start_after = (zone.temps[0][0] if zone.temps else export.start) + WARMUP_S
        report["zones"][zone.name] = summarize(zone, res, start_after)
        report["zones"][zone.name]["runtime_s"] = round(time.time() - t0, 1)
    seen: set[str] = set()
    for zone in export.zones.values():
        if zone.boiler and zone.boiler not in seen and zone.boiler_states:
            seen.add(zone.boiler)
            report["boilers"][zone.boiler] = boiler_cycling(zone.boiler_states)
    return report


def markdown(report: dict[str, Any]) -> str:
    """Compact per-zone tables."""
    zones = report["zones"]
    lines = ["# Replay of the recorded export", ""]
    lines.append("## Prediction error (MAE, °C; new model / persistence)")
    lines.append("")
    lines.append("| zone | one-step | 1 h | 2 h | 4 h | 4 h heated | burst peak: new / old model |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, z in zones.items():
        b = z["bursts"]
        lines.append(
            f"| {name} | {z['one_step']['model_mae']} / {z['one_step']['naive_mae']} "
            f"| {z['1h']['all']['model_mae']} / {z['1h']['all']['naive_mae']} "
            f"| {z['2h']['all']['model_mae']} / {z['2h']['all']['naive_mae']} "
            f"| {z['4h']['all']['model_mae']} / {z['4h']['all']['naive_mae']} "
            f"| {z['4h']['heated']['model_mae']} / {z['4h']['heated']['naive_mae']} "
            f"| {b['new_model_peak_mae']} (n={b['bursts']}) / {b['old_model_peak_mae']} (n={b['old_model_cycles']}) |"
        )
    lines += ["", "## Window detection, safety, drift", ""]
    lines.append(
        "| zone | windows old→new | sharp drops caught | stale-time alarms* | physics alarms | "
        "drift incidents (h open / h with boiler on) | zone on while OFF | over target+0.5 (heating / passive) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, z in zones.items():
        w, s, d, r, o = z["window"], z["stale"], z["drift"], z["race"], z["overshoot"]
        lines.append(
            f"| {name} | {w['old_detections']}→{w['new_detections']} | {w['sharp_drops_caught']}/{w['sharp_drops']} "
            f"| {s['time_alarms']} | {s['physics_alarms']} | {d['incidents']} ({d['open_hours']} / {d['heat_hours']}) "
            f"| {r['zone_on_while_off_episodes']} ({r['zone_on_while_off_minutes']} min) "
            f"| {o.get('over_target_pct')}% ({o.get('heating_caused_pct_of_over')}% / {o.get('passive_pct_of_over')}%) |"
        )
    lines += [
        "",
        "\\* worst case: assumes the sensors never re-report an unchanged value "
        "(with heartbeats Home Assistant's `last_reported` keeps them fresh).",
        "",
        "## Learned parameters",
        "",
        "| zone | q_s K/h | tau_f min | tau_e min | tau_r h | tau_am h | rho | g K/h | k K/h | param conf |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, z in zones.items():
        p = z["params"]
        lines.append(
            f"| {name} | {p['q_s']} | {p['tau_f_min']} | {p['tau_e_min']} | {p['tau_r_h']} | {p['tau_am_h']} "
            f"| {p['rho']} | {p['base_gain']} | {p['shared_gain']} | {p['confidence_param']} |"
        )
    lines += ["", "## Boilers (recorded history)", ""]
    for name, b in report["boilers"].items():
        lines.append(f"* `{name}`: {json.dumps(b)}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--export", help="export directory (default: $ADAPTIVE_THERMOSTAT_EXPORT)")
    parser.add_argument("--zones", help="comma separated zone names")
    parser.add_argument("--json", help="write the full report as JSON")
    parser.add_argument("--markdown", help="write summary tables as Markdown")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=None,
        help="assume the sensors re-report unchanged values every N seconds",
    )
    args = parser.parse_args(argv)
    path = find_export(args.export)
    if path is None:
        print("No export found; set ADAPTIVE_THERMOSTAT_EXPORT or pass --export.")
        return 0
    export = load_export(path)
    settings = ReplaySettings(heartbeat_s=args.heartbeat)
    report = run(export, set(args.zones.split(",")) if args.zones else None, settings)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str))
    md = markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
