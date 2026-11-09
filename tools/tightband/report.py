"""Tight-band report: recorded history vs simulated old and new controller.

Usage::

    # With the private export(s) (run where the data lives):
    python tools/tightband/report.py --export <new export> [--old-export <old export>] --out <dir>

    # Without data: scenario ensemble on the models in docs/REPLAY.md
    python tools/tightband/report.py --synthetic --out <dir>

With an export the harness

1. measures the recorded history in reading space (BEFORE): band statistics
   during heat-mode time (window-open time excluded), bursts and boiler starts
   per day, the observed rise of minimum-length bursts, "late fires" (heat
   only after the reading left the band), for the whole export and for the
   v2.1 period;
2. takes each zone's learned model (``thermal_params*`` in config.json, else
   a replay of the export, else the docs/REPLAY.md models);
3. calibrates the simulator per zone: radiator scale and unmodelled-gain
   level are chosen from a grid so that the *simulated old controller*
   reproduces the recorded band statistics, bursts per day and burst rise;
4. simulates the old controller (git ref ``--old-ref``, default the 2.1.0
   release commit) and the current one on the calibrated rooms, driven by
   the recorded outdoor temperature, targets, modes and window contacts;
5. writes ``tight_band_report.md`` and ``tight_band_report.json``.

Simulations are independent processes (``--workers``); one 10-day run of
the whole installation takes about two minutes per worker.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import dataclasses
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from history import V21_LIVE, analyse, recorded_inputs  # noqa: E402
from plants import ZoneModel, model_burst_rise  # noqa: E402
from scenario import DEFAULT_BOILERS, DEFAULT_MODELS, run_scenario  # noqa: E402

OLD_REF = "9e505dd"  # Adaptive Thermostat 2.1.0 (the version running live)
SCALES = (1.0, 1.5, 2.0, 2.5, 3.0)
NOISES = (0.1, 0.2, 0.3, 0.45)
# Synthetic scenarios (no export): radiator scale, unmodelled gains, outdoor.
SCENARIOS: dict[str, dict[str, float]] = {
    # Rooms behave like their learned models, small unmodelled gains.
    "ideal": {"scale": 1.0, "noise": 0.1, "tout": 7.0},
    # Bursts lift the room twice as much as the model's q_s says at first,
    # unmodelled gains of 0.3 K/h: reproduces living_2's recorded signature
    # (minimum-length bursts, 22.8-23.2, heat only after 22.8 shows).
    "field_like": {"scale": 2.0, "noise": 0.3, "tout": 8.0},
    # The model keeps under-predicting burst peaks (frozen parameters): the
    # learned peak correction grows to 0.1-0.2 K, as suspected for living_2.
    "model_underpredicts": {"scale": 2.5, "noise": 0.1, "tout": 8.0, "freeze": 1.0},
}


# ------------------------------------------------------------------ helpers
def _models_from_export(export: Any, *, replay: bool) -> tuple[dict[str, ZoneModel], dict[str, str]]:
    models: dict[str, ZoneModel] = {}
    source: dict[str, str] = {}
    for zone in export.zones.values():
        valves = max(1, len(zone.valves))
        learned = next(
            (
                v
                for k, v in sorted(zone.config.items(), reverse=True)
                if k.startswith("thermal_params") and isinstance(v, dict) and "q_s" in v
            ),
            None,
        )
        if learned is not None:
            models[zone.name] = ZoneModel.from_dict(learned, valves=valves)
            source[zone.name] = "config thermal_params"
        elif replay:
            from replay import replay_zone

            final = replay_zone(zone).final
            models[zone.name] = ZoneModel.from_dict(final, valves=valves)
            source[zone.name] = "replay of the export"
        elif zone.name in DEFAULT_MODELS:
            models[zone.name] = dataclasses.replace(DEFAULT_MODELS[zone.name], valves=valves)
            source[zone.name] = "docs/REPLAY.md (old export)"
    return models, source


def _boilers_from_export(export: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for zone in export.zones.values():
        out.setdefault(zone.boiler or "none", []).append(zone.name)
    return out


def _outdoor_fn(series: list[tuple[float, float]]):
    times = [t for t, _ in series]
    values = [v for _, v in series]

    def fn(t: float) -> float:
        if not times:
            return 7.0
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return values[max(0, lo - 1)]

    return fn


def _sim_job(job: dict[str, Any]) -> dict[str, Any]:
    """One whole-installation run (executed in a worker process)."""
    from legacy import load_engine
    import simulator

    engine = load_engine(job["engine"]) if job["engine"] != "current" else None
    rec = job.get("recorded")
    kwargs: dict[str, Any] = dict(
        days=job["days"],
        warmup_days=job["warmup_days"],
        seed=job["seed"],
        models={k: ZoneModel(**v) for k, v in job["models"].items()},
        boilers=job["boilers"],
        heat_scales=job["scales"],
        noise_kh=job["noises"],
        t0=job["t0"],
        periods=job.get("periods"),
        label=job["label"],
        sensor=simulator.zigbee_sensor,
    )
    if rec is not None:
        kwargs.update(
            outdoor_fn=_outdoor_fn([tuple(x) for x in rec["outdoor"]]),
            schedules={k: [tuple(e) for e in v] for k, v in rec["schedules"].items()},
            targets={k: v[0] for k, v in rec["initial"].items()},
            initial_hvac={k: v[1] for k, v in rec["initial"].items()},
            excluded={k: [tuple(w) for w in v] for k, v in rec["windows"].items()},
        )
    else:
        kwargs.update(outdoor_mean=job["tout"], freeze_params=bool(job.get("freeze")))
    res = run_scenario(engine, **kwargs)
    return {
        "job": {k: job[k] for k in ("label", "engine", "seed")},
        "zones": res.zones,
        "boilers": res.boilers,
    }


def _run_all(jobs: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    if workers <= 1:
        return [_sim_job(j) for j in jobs]
    with ProcessPoolExecutor(workers) as ex:
        return list(ex.map(_sim_job, jobs))


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return round(statistics.mean(vals), 2) if vals else None


def _aggregate(runs: list[dict[str, Any]], period: str) -> dict[str, Any]:
    """Mean over seeds of per-zone and per-boiler metrics for one period."""
    zones: dict[str, Any] = {}
    names = runs[0]["zones"].keys()
    for name in names:
        per = [r["zones"][name]["periods"][period] for r in runs]
        zones[name] = {
            "band": {k: _mean([p["band"][k] for p in per]) for k in per[0]["band"]},
            "bursts": {
                k: _mean([p["bursts"][k] for p in per]) for k in ("per_day", "under_2min", "median_min")
            },
            "rise": {k: _mean([p["rise"][k] for p in per]) for k in per[0]["rise"]},
            "late": {
                k: _mean([p["late"][k] for p in per]) for k in ("late_fires", "fires", "late_share_pct")
            },
            "peak_correction": _mean([r["zones"][name]["peak_correction"] for r in runs]),
            "trigger_correction": _mean([r["zones"][name]["trigger_correction"] for r in runs]),
        }
    boilers = {
        b: {
            k: _mean([r["boilers"][b][period][k] for r in runs])
            for k in ("per_day", "under_2min", "median_min")
        }
        for b in runs[0]["boilers"]
    }
    return {"zones": zones, "boilers": boilers}


def _distance(sim: dict[str, Any], rec: dict[str, Any]) -> float:
    """How far a simulated zone is from the recorded one (calibration cost)."""
    sb, rb = sim["band"], rec["band"]
    cost = 0.0
    for key, scale in (("within_db_pct", 10.0), ("under_pct", 3.0), ("over_pct", 10.0)):
        if sb.get(key) is not None and rb.get(key) is not None:
            cost += abs(sb[key] - rb[key]) / scale
    if sim["bursts"]["per_day"] is not None and rec["bursts"]["per_day"] is not None:
        cost += abs(sim["bursts"]["per_day"] - rec["bursts"]["per_day"]) / 5.0
    sr, rr = sim["rise"].get("min_burst_rise_median"), rec["rise"].get("min_burst_rise_median")
    if sr is not None and rr is not None:
        cost += abs(sr - rr) / 0.05
    return cost


def feasibility(
    model: ZoneModel, observed_rise: float | None, sim_rise: float | None, deadband: float
) -> dict[str, Any]:
    """Can target +/- deadband be held in reading space?  (see docs/TIGHT_BAND.md)"""
    rise_model = round(model_burst_rise(model, 120.0), 3)
    rise_60 = round(model_burst_rise(model, 60.0), 3)
    reading_span = 2 * deadband + 0.1  # 22.85 .. 23.15 for 23 +/- 0.1
    margin = 0.1  # estimate uncertainty at both ends (+/- 0.05)
    rise = max(v for v in (rise_model, observed_rise, sim_rise) if v is not None)
    feasible = rise + margin <= reading_span + 1e-9
    half = max(deadband, math.ceil((rise + margin - 0.1) / 2 / 0.05 - 1e-9) * 0.05)
    return {
        "model_rise_120s": rise_model,
        "model_rise_60s": rise_60,
        "observed_rise_min_burst": observed_rise,
        "simulated_rise_min_burst": sim_rise,
        "rise_used": round(rise, 3),
        "reading_span": round(reading_span, 2),
        "feasible": feasible,
        "realistic_half_band": round(half, 2),
    }


def _episode(export: Any, zone_name: str, start: float, end: float) -> dict[str, Any] | None:
    from history import _local
    from replay import heat_input_timeline

    zone = next((z for z in export.zones.values() if z.name == zone_name), None)
    if zone is None:
        return None
    temps = [(_local(t), v) for t, v in zone.temps if start <= t <= end]
    heat = [(_local(t), u) for t, u in heat_input_timeline(zone) if start <= t <= end]
    rows = [
        (_local(r["t"]), r.get("hvac_action"), r.get("temperature"), r.get("zone_heater_on"))
        for r in zone.climate_rows
        if start <= r["t"] <= end
    ]
    return {"readings": temps, "heat_input": heat, "climate_rows": rows[:60]}


def held_reading_episode(old_ref: str) -> list[dict[str, Any]]:
    """living_2 field case: 22.9 shown, no report for 30 min; when is heat requested?

    The zone runs the live living_2 model (09.11); the room cools at 0.05-0.2
    K/h; the learned peak correction is 0 or 0.2 K.  Old = ``old_ref``.
    """
    from legacy import load_engine

    import engine.estimator as new_est
    import engine.zone as new_zone

    old = load_engine(old_ref)
    old_est = sys.modules[f"{old.module.__name__.rsplit('.', 1)[0]}.estimator"]
    live = (2.416, 5.73 / 60, 15.7 / 60, 24.1, 1.21, 4.65)
    out: list[dict[str, Any]] = []
    for pc in (0.0, 0.2):
        for label, zmod, emod in (("old", old.module, old_est), ("new", new_zone, new_est)):
            for cooling in (0.05, 0.1, 0.2):
                est = emod.ZoneEstimator()
                for idx, value in zip(range(6, 12), live):
                    est.x[idx] = math.log(value)
                    est.p[idx][idx] = 0.15**2
                est.x[4] = (23.0 - 8.0) / 24.1 - cooling
                est.x[5] = 0.0
                data = est.to_dict()
                data["t"] = -3600.0
                data["stats"].update(bursts_learned=200, heat_hours_learned=20.0)
                core = zmod.ZoneCore(
                    zmod.ZoneConfig(), stored={"estimator": data, "validation_errors": [-pc] * 3}, now=-7200.0
                )
                core.mode_block_until = 0.0
                core.set_hvac(-7200.0, True)
                core.set_target(23.0)
                core.on_outdoor(-3600.0, 8.0)
                core.on_actuators(-3600.0, 0.0, False)
                core.on_temperature(-600.0, 23.0)
                core.update(-600.0)
                core.on_temperature(0.0, 22.9)
                fired = None
                t = 0.0
                while t <= 3600.0 and fired is None:
                    if t == 1800.0:
                        core.on_temperature(t, 22.8)  # next periodic report
                    core.update(t)
                    if core.burst is not None:
                        dec = core._last_decision
                        fired = {
                            "minutes_after_22_9": round(t / 60.0),
                            "after_22_8_shown": t >= 1800.0,
                            "estimate": round(core.estimator.temperature, 3),
                            "predicted_peak": round(dec.predicted_peak, 3)
                            if dec and dec.predicted_peak
                            else None,
                        }
                    t += 60.0
                out.append(
                    {"controller": label, "peak_correction": pc, "cooling_K_per_h": cooling, "fired": fired}
                )
    return out


# ------------------------------------------------------------------ report
def _fmt(v: Any, digits: int = 1) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _zone_table(title: str, cols: list[tuple[str, dict[str, Any]]], names: list[str]) -> list[str]:
    lines = [f"### {title}", ""]
    lines.append(
        "| zone | source | in ±db % | in ±2db % | over % (max / mean K) | under % (max / mean K) | bursts/day | late fires |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for name in names:
        for label, data in cols:
            z = data.get(name)
            if z is None:
                continue
            b = z["band"]
            late = z.get("late") or {}
            lines.append(
                f"| {name} | {label} | {_fmt(b.get('within_db_pct'))} | {_fmt(b.get('within_2db_pct'))} "
                f"| {_fmt(b.get('over_pct'))} ({_fmt(b.get('max_over'), 2)} / {_fmt(b.get('mean_over'), 2)}) "
                f"| {_fmt(b.get('under_pct'))} ({_fmt(b.get('max_under'), 2)} / {_fmt(b.get('mean_under'), 2)}) "
                f"| {_fmt(z['bursts'].get('per_day'))} | {_fmt(late.get('late_fires'), 0)} / {_fmt(late.get('fires'), 0)} |"
            )
    lines.append("")
    return lines


def _boiler_table(cols: list[tuple[str, dict[str, Any]]]) -> list[str]:
    lines = ["| boiler | source | starts/day | runs < 2 min | median run (min) |", "|---|---|---|---|---|"]
    for label, data in cols:
        for b, m in data.items():
            lines.append(
                f"| {b} | {label} | {_fmt(m.get('per_day'))} | {_fmt(m.get('under_2min'), 0)} | {_fmt(m.get('median_min'), 2)} |"
            )
    lines.append("")
    return lines


def _before_view(before: dict[str, Any], period: str) -> tuple[dict[str, Any], dict[str, Any]]:
    zones = {n: z[period] for n, z in before["zones"].items()}
    boilers = {b: v[period] for b, v in before["boilers"].items()}
    return zones, boilers


def build_markdown(rep: dict[str, Any]) -> str:
    lines = ["# Adaptive Thermostat 2.2: tight band in reading space", ""]
    lines += [f"Generated {rep['generated']} by `tools/tightband/report.py` ({rep['mode']}).", ""]
    lines += rep.get("notes", [])
    names = rep["zones"]
    if "before" in rep:
        for period, title in (("v21", "v2.1 period (07.11 15:57 UTC →)"), ("all", "whole export")):
            if period not in rep["sim"]:
                continue
            bz, bb = _before_view(rep["before"], period)
            cols = [
                ("BEFORE (recorded)", bz),
                ("SIM old", rep["sim"][period]["old"]["zones"]),
                ("SIM new", rep["sim"][period]["new"]["zones"]),
            ]
            lines += [f"## Results: {title}", ""]
            lines += _zone_table("Per zone (reading space, heat-mode time)", cols, names)
            lines += ["### Boilers", ""]
            lines += _boiler_table(
                [
                    ("BEFORE (recorded)", bb),
                    ("SIM old", rep["sim"][period]["old"]["boilers"]),
                    ("SIM new", rep["sim"][period]["new"]["boilers"]),
                ]
            )
        lines += ["## Calibration (whole export): recorded vs simulated old controller", ""]
        lines += [
            "| zone | radiator scale | unmodelled gains K/h | cost | in ±db rec / sim | under rec / sim | over rec / sim | bursts/day rec / sim | min-burst rise rec / sim |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name in names:
            c = rep["calibration"].get(name)
            if not c:
                continue
            r, s = c["recorded"], c["simulated"]
            lines.append(
                f"| {name} | {c['scale']} | {c['noise']} | {c['cost']:.2f} | {_fmt(r['band']['within_db_pct'])} / {_fmt(s['band']['within_db_pct'])} "
                f"| {_fmt(r['band']['under_pct'])} / {_fmt(s['band']['under_pct'])} | {_fmt(r['band']['over_pct'])} / {_fmt(s['band']['over_pct'])} "
                f"| {_fmt(r['bursts']['per_day'])} / {_fmt(s['bursts']['per_day'])} "
                f"| {_fmt(r['rise'].get('min_burst_rise_median'), 2)} / {_fmt(s['rise'].get('min_burst_rise_median'), 2)} |"
            )
        lines.append("")
    else:
        for scen, data in rep["sim"].items():
            lines += [f"## Scenario `{scen}` ({json.dumps(SCENARIOS.get(scen, {}))})", ""]
            lines += _zone_table(
                "Per zone (reading space, heat-mode time)",
                [("SIM old", data["old"]["zones"]), ("SIM new", data["new"]["zones"])],
                names,
            )
            lines += ["### Boilers", ""]
            lines += _boiler_table([("SIM old", data["old"]["boilers"]), ("SIM new", data["new"]["boilers"])])
    lines += ["## Physical feasibility of ±deadband per zone", ""]
    lines += [
        "Reading-space band for 23 ± 0.1: the reading stays 22.9–23.1 while the room is 22.85–23.15 (0.3 K).  "
        "The smallest burst must fit into that span together with the estimate's uncertainty (±0.05 K).  "
        "`rise used` is the largest of the model's, the recorded and the simulated rise of a minimum burst.",
        "",
        "| zone | model rise 120 s | model rise 60 s | recorded rise (min bursts) | simulated rise | rise used | ±0.1 feasible | realistic band |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in names:
        f = rep["feasibility"].get(name)
        if not f:
            continue
        lines.append(
            f"| {name} | {f['model_rise_120s']:.2f} | {f['model_rise_60s']:.2f} | {_fmt(f['observed_rise_min_burst'], 2)} "
            f"| {_fmt(f['simulated_rise_min_burst'], 2)} | {f['rise_used']:.2f} | {'yes' if f['feasible'] else 'no'} | ±{f['realistic_half_band']:.2f} |"
        )
    lines.append("")
    if rep.get("held_reading"):
        lines += [
            "## Field case reproduced: 22.9 shown, next report 30 min later",
            "",
            "Live living_2 model; a 22.9 report arrives at minute 0 (23.0 before), the next periodic report "
            "(minute 30) shows 22.8.  When does each controller request heat?",
            "",
            "| peak correction | cooling K/h | old: fires at | old predicted peak | new: fires at | new predicted peak |",
            "|---|---|---|---|---|---|",
        ]
        rows = rep["held_reading"]
        for pc in (0.0, 0.2):
            for cooling in (0.05, 0.1, 0.2):
                o = next(
                    r["fired"]
                    for r in rows
                    if r["controller"] == "old"
                    and r["peak_correction"] == pc
                    and r["cooling_K_per_h"] == cooling
                )
                n = next(
                    r["fired"]
                    for r in rows
                    if r["controller"] == "new"
                    and r["peak_correction"] == pc
                    and r["cooling_K_per_h"] == cooling
                )

                def when(f: dict[str, Any] | None) -> str:
                    if f is None:
                        return "not within 60 min"
                    return f"{f['minutes_after_22_9']} min" + (
                        " (after 22.8 shown)" if f["after_22_8_shown"] else ""
                    )

                lines.append(
                    f"| {pc} | {cooling} | {when(o)} | {_fmt(o and o['predicted_peak'], 2)} | {when(n)} | {_fmt(n and n['predicted_peak'], 2)} |"
                )
        lines.append("")
    if rep.get("episode"):
        ep = rep["episode"]
        lines += [
            "## living_2 on 09.11 (recorded, local time)",
            "",
            "Readings: " + ", ".join(f"{t} {v}" for t, v in ep["readings"]),
            "",
        ]
        lines += ["Heat input (valve × boiler): " + ", ".join(f"{t} {u:g}" for t, u in ep["heat_input"]), ""]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- main
def _model_dict(m: ZoneModel) -> dict[str, Any]:
    return dataclasses.asdict(m)


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    from export_loader import load_export

    path = Path(args.export)
    export = load_export(path)
    raw = json.loads((path / "raw_noattr.json").read_text())
    periods = {"all": (export.start, export.end)}
    if export.end > V21_LIVE:
        periods["v21"] = (max(export.start, V21_LIVE), export.end)
    before = analyse(export, raw, deadband=args.deadband, periods_of_interest=periods)
    rep: dict[str, Any] = {"mode": f"export {path}", "before": before}
    if args.old_export:
        old = load_export(Path(args.old_export))
        old_raw = json.loads((Path(args.old_export) / "raw_noattr.json").read_text())
        rep["before_old_export"] = analyse(old, old_raw, deadband=args.deadband)
    models, source = _models_from_export(export, replay=args.replay)
    rep["model_source"] = source
    boilers = _boilers_from_export(export)
    names = sorted(models)
    rec = recorded_inputs(export, raw)
    windows: dict[str, list[tuple[float, float]]] = {}
    from history import window_intervals

    for zone in export.zones.values():
        windows[zone.name] = window_intervals(zone, raw, export)
    recorded = {
        "outdoor": rec["outdoor"],
        "schedules": rec["schedules"],
        "initial": rec["initial"],
        "windows": windows,
    }
    days = (export.end - export.start) / 86400.0
    sim_periods = {k: (max(a, export.start + args.warmup_days * 86400.0), b) for k, (a, b) in periods.items()}
    base_job = {
        "days": days,
        "warmup_days": args.warmup_days,
        "models": {k: _model_dict(v) for k, v in models.items()},
        "boilers": boilers,
        "t0": export.start,
        "periods": sim_periods,
        "recorded": recorded,
    }
    # Calibration grid: every zone sees each (scale, noise) once.
    t_start = time.time()
    grid_jobs = [
        {
            **base_job,
            "label": f"cal {s} {n}",
            "engine": args.old_ref,
            "seed": 1,
            "scales": dict.fromkeys(names, s),
            "noises": dict.fromkeys(names, n),
        }
        for s in (float(x) for x in args.scales.split(","))
        for n in (float(x) for x in args.noises.split(","))
    ]
    grid = _run_all(grid_jobs, args.workers)
    calibration: dict[str, Any] = {}
    for name in names:
        rec_zone = before["zones"][name]["all"]
        best = None
        for job, res in zip(grid_jobs, grid):
            sim_zone = res["zones"][name]["periods"]["all"]
            cost = _distance(sim_zone, rec_zone)
            if best is None or cost < best[0]:
                best = (cost, job["scales"][name], job["noises"][name], sim_zone)
        assert best is not None
        calibration[name] = {
            "cost": best[0],
            "scale": best[1],
            "noise": best[2],
            "recorded": rec_zone,
            "simulated": best[3],
        }
    rep["calibration"] = calibration
    scales = {n: calibration[n]["scale"] for n in names}
    noises = {n: calibration[n]["noise"] for n in names}
    final_jobs = [
        {
            **base_job,
            "label": f"{eng} {seed}",
            "engine": ref,
            "seed": seed,
            "scales": scales,
            "noises": noises,
        }
        for eng, ref in (("old", args.old_ref), ("new", "current"))
        for seed in range(1, args.seeds + 1)
    ]
    final = _run_all(final_jobs, args.workers)
    rep["sim"] = {}
    for period in sim_periods:
        rep["sim"][period] = {
            eng: _aggregate([r for j, r in zip(final_jobs, final) if j["label"].startswith(eng)], period)
            for eng in ("old", "new")
        }
    rep["feasibility"] = {
        n: feasibility(
            models[n],
            before["zones"][n]["all"]["rise"].get("min_burst_rise_median"),
            rep["sim"]["all"]["old"]["zones"][n]["rise"].get("min_burst_rise_median"),
            args.deadband,
        )
        for n in names
    }
    rep["held_reading"] = held_reading_episode(args.old_ref)
    living_day = (max(export.start, export.end - 86400.0), export.end)
    rep["episode"] = _episode(export, "living_2", living_day[1] - 3 * 3600.0, living_day[1])
    rep["zones"] = names
    rep["runtime_s"] = round(time.time() - t_start)
    return rep


def run_synthetic(args: argparse.Namespace) -> dict[str, Any]:
    names = sorted(DEFAULT_MODELS)
    rep: dict[str, Any] = {"mode": "synthetic (no export available)", "zones": names, "sim": {}}
    t_start = time.time()
    jobs = []
    for scen, p in SCENARIOS.items():
        for eng, ref in (("old", args.old_ref), ("new", "current")):
            for seed in range(1, args.seeds + 1):
                jobs.append(
                    {
                        "label": f"{scen}|{eng}|{seed}",
                        "engine": ref,
                        "seed": seed,
                        "days": args.days,
                        "warmup_days": args.warmup_days,
                        "t0": 1_793_000_000.0,
                        "periods": None,
                        "tout": p["tout"],
                        "models": {k: _model_dict(v) for k, v in DEFAULT_MODELS.items()},
                        "boilers": DEFAULT_BOILERS,
                        "scales": dict.fromkeys(names, p["scale"]),
                        "noises": dict.fromkeys(names, p["noise"]),
                        "freeze": p.get("freeze", 0.0),
                    }
                )
    results = _run_all(jobs, args.workers)
    for scen in SCENARIOS:
        rep["sim"][scen] = {
            eng: _aggregate(
                [r for j, r in zip(jobs, results) if j["label"].startswith(f"{scen}|{eng}|")], "all"
            )
            for eng in ("old", "new")
        }
    rep["held_reading"] = held_reading_episode(args.old_ref)
    rep["feasibility"] = {
        n: feasibility(
            DEFAULT_MODELS[n],
            None,
            rep["sim"]["field_like"]["old"]["zones"][n]["rise"].get("min_burst_rise_median"),
            args.deadband,
        )
        for n in names
    }
    rep["runtime_s"] = round(time.time() - t_start)
    return rep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--export", help="new export directory (recorded history of the live system)")
    parser.add_argument(
        "--old-export", help="optional older export (reported as BEFORE of the old controller)"
    )
    parser.add_argument("--synthetic", action="store_true", help="no export: scenario ensemble")
    parser.add_argument("--out", required=True, help="output directory for the markdown and JSON report")
    parser.add_argument("--old-ref", default=OLD_REF, help="git ref of the old controller")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--days", type=float, default=7.0, help="synthetic: simulated days")
    parser.add_argument("--warmup-days", type=float, default=1.0)
    parser.add_argument("--deadband", type=float, default=0.1)
    parser.add_argument("--replay", action="store_true", help="learn missing models by replaying the export")
    parser.add_argument(
        "--scales", default=",".join(map(str, SCALES)), help="calibration grid: radiator scales"
    )
    parser.add_argument(
        "--noises", default=",".join(map(str, NOISES)), help="calibration grid: unmodelled gains K/h"
    )
    args = parser.parse_args(argv)
    if not args.export and not args.synthetic:
        parser.error("pass --export or --synthetic")
    rep = run_export(args) if args.export else run_synthetic(args)
    rep["generated"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tight_band_report.json").write_text(json.dumps(rep, indent=1, default=str))
    (out / "tight_band_report.md").write_text(build_markdown(rep))
    print(f"wrote {out / 'tight_band_report.md'} and .json ({rep.get('runtime_s')} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
