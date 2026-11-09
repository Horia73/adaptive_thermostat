"""Closed-loop simulation of the whole installation (9 zones, 2 boilers).

Each boiler group is one :class:`simulator.Plant` (zones on different boilers
do not interact).  Zones are simulated rooms derived from learned models
(:mod:`plants`), sensors behave like the recorded ones
(:func:`simulator.zigbee_sensor`), and the control side is a real engine:
the current one or a released version (:mod:`legacy`).  Every zone starts
with its estimator seeded from the learned model, as in the live system.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import math
from pathlib import Path
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from history import burst_rises, late_fires, rise_stats  # noqa: E402
from metrics import band_metrics, boiler_periods, cycling_metrics  # noqa: E402
from plants import ZoneModel, room_from_model  # noqa: E402
from simulator import CURRENT_ENGINE, Plant, SensorTruth, zigbee_sensor  # noqa: E402

from engine.boiler import BoilerConfig  # noqa: E402
from engine.estimator import PARAM_INDEXES, ZoneEstimator  # noqa: E402

DAY = 86400.0
HEATED_TAIL_S = 2700.0  # over-band time this long after a burst counts as heating-caused

# Learned models (docs/REPLAY.md, old export 15-25 Oct; living_2 from the
# live system on 09.11).  The report harness replaces them with models learned
# from the export(s) when available.
DEFAULT_MODELS: dict[str, ZoneModel] = {
    "dormitor": ZoneModel(1.827, 3.27, 10.4, 49.9, 1.01, 5.05, 0.125, 0.078, valves=2),
    "hol_intrare": ZoneModel(3.253, 3.45, 9.9, 43.1, 1.31, 4.13, 0.225, 0.323),
    "birou": ZoneModel(2.168, 10.35, 13.5, 30.0, 1.32, 8.93, 0.253, 0.064),
    "coridor": ZoneModel(1.266, 6.83, 8.2, 36.5, 0.84, 11.17, 0.296, 0.229),
    "bucatarie": ZoneModel(2.528, 4.75, 9.2, 27.0, 2.74, 5.01, 0.44, 0.432),
    "living_2": ZoneModel(2.416, 5.73, 15.7, 24.1, 1.21, 4.65, 0.364, 0.034),
    "dressing": ZoneModel(2.279, 5.42, 20.5, 32.0, 1.84, 6.16, 0.281, 0.396),
    "baie_1": ZoneModel(0.849, 2.51, 20.5, 40.3, 4.81, 1.89, 0.284, 0.309),
    "baie_2": ZoneModel(2.241, 6.39, 13.5, 51.3, 2.15, 9.91, 0.124, 0.263),
}
DEFAULT_BOILERS: dict[str, list[str]] = {
    "switch.centrala2": ["birou", "dressing", "hol_intrare", "baie_1"],
    "switch.centrala3": ["living_2", "coridor", "bucatarie", "dormitor", "baie_2"],
}
# Passive disturbances per zone: (spikes per day, spike K/h).
DISTURBANCES: dict[str, tuple[float, float]] = {
    "bucatarie": (2.0, 2.0),
    "baie_1": (1.0, 2.0),
    "baie_2": (1.0, 2.0),
}


@dataclass
class ScenarioResult:
    """Raw logs and metrics of one run."""

    engine: str
    zones: dict[str, dict[str, Any]] = field(default_factory=dict)
    boilers: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def seeded_core_storage(model: ZoneModel) -> dict[str, Any]:
    """Core storage with the estimator seeded from a learned model."""
    est = ZoneEstimator()
    for idx, value in zip(
        PARAM_INDEXES,
        (model.q_s, model.tau_f_min / 60, model.tau_e_min / 60, model.tau_r_h, model.tau_am_h, model.rho),
    ):
        est.x[idx] = math.log(value)
        est.p[idx][idx] = 0.2**2
    est.x[4] = model.base_gain
    est.x[5] = model.shared_gain
    est.p[4][4] = 0.05**2
    est.p[5][5] = 0.05**2
    est.t = None
    data = est.to_dict()
    data["stats"].update({"bursts_learned": 100, "heat_hours_learned": 10.0, "reports": 1000})
    data["t"] = 0.0
    return {"estimator": data, "validation_errors": [0.0, 0.0, 0.0]}


def run_scenario(
    engine: Any = None,
    *,
    models: dict[str, ZoneModel] | None = None,
    boilers: dict[str, list[str]] | None = None,
    heat_scales: dict[str, float] | None = None,
    noise_kh: float | dict[str, float] = 0.0,
    sensor_plume: float = 0.0,
    days: float = 7.0,
    warmup_days: float = 1.0,
    seed: int = 1,
    t0: float = 1_793_000_000.0,
    targets: dict[str, float] | None = None,
    outdoor_fn: Callable[[float], float] | None = None,
    outdoor_mean: float = 7.0,
    outdoor_swing: float = 4.0,
    schedules: dict[str, list[tuple[float, str, Any]]] | None = None,
    deadband: float = 0.1,
    sensor: Callable[[], SensorTruth] = zigbee_sensor,
    zone_config: Callable[[Any, str], Any] | None = None,
    boiler_config: BoilerConfig | None = None,
    label: str | None = None,
    keep_raw: bool = False,
    initial_hvac: dict[str, bool] | None = None,
    excluded: dict[str, list[tuple[float, float]]] | None = None,
    periods: dict[str, tuple[float, float]] | None = None,
    freeze_params: bool = False,
) -> ScenarioResult:
    """Simulate all boiler groups and compute per-zone/per-boiler metrics."""
    engine = engine or CURRENT_ENGINE
    models = models or DEFAULT_MODELS
    boilers = boilers or DEFAULT_BOILERS
    heat_scales = heat_scales or {}
    result = ScenarioResult(engine=str(label or getattr(engine, "ref", "current")))
    start = t0 + warmup_days * DAY
    end = t0 + days * DAY
    windows = periods or {"all": (start, end)}
    for b_index, (boiler, names) in enumerate(boilers.items()):
        names = [n for n in names if n in models]
        if not names:
            continue
        rooms = []
        cfgs: dict[str, Any] = {}
        stored: dict[str, dict] = {}
        for name in names:
            spikes, power = DISTURBANCES.get(name, (0.3, 1.0))
            room = room_from_model(
                name,
                models[name],
                heat_scale=heat_scales.get(name, 1.0),
                spike_rate_per_day=spikes,
                spike_power_kh=power,
                noise_kh=noise_kh.get(name, 0.0) if isinstance(noise_kh, dict) else noise_kh,
                sensor_plume=sensor_plume,
            )
            rooms.append((room, sensor()))
            cfg = engine.ZoneConfig(deadband=deadband)
            if zone_config is not None:
                cfg = zone_config(cfg, name) or cfg
            cfgs[name] = cfg
            stored[name] = seeded_core_storage(models[name])
        target = 23.0
        plant = Plant(
            rooms,
            target=target,
            start_temp=target - 0.05,
            seed=seed * 101 + b_index,
            engine=engine,
            outdoor_fn=outdoor_fn,
            outdoor_mean=outdoor_mean,
            outdoor_swing=outdoor_swing,
            schedules=schedules,
            zone_configs=cfgs,
            stored=stored,
            t0=t0,
            boiler_config=boiler_config or BoilerConfig(min_on_s=120, min_off_s=180),
        )
        if freeze_params:
            # The model keeps its learned parameters (structural mismatch the
            # estimator cannot learn away); only the offset corrections adapt.
            for z in plant.zones:
                _freeze(z.core)
        for z in plant.zones:
            tgt = (targets or {}).get(z.truth.name, target)
            hvac = (initial_hvac or {}).get(z.truth.name, True)
            z.core.set_target(tgt)
            z.core.set_hvac(t0, hvac)
            z.hvac_on = hvac
            z.setpoints[:] = [(t0, tgt, hvac)]
            z.t_air = z.t_mass = z.t_sensor = tgt - 0.05
        plant.run(days * 24.0)
        for z in plant.zones:
            name = z.truth.name
            heated = [(a, b + HEATED_TAIL_S) for a, b in z.bursts]
            excl = (excluded or {}).get(name, [])
            per: dict[str, Any] = {}
            for period, (a, b) in windows.items():
                fires = [f for f in z.fires if a <= f[0] < b]
                per[period] = {
                    "band": band_metrics(
                        z.readings, z.setpoints, a, b, deadband, excluded=excl, heated=heated
                    ),
                    "true_band": band_metrics(
                        _truth_series(z.air_trace), z.setpoints, a, b, deadband, excluded=excl
                    ),
                    "bursts": cycling_metrics(z.bursts, a, b),
                    "rise": rise_stats(burst_rises(z.readings, z.bursts, a, b)),
                    "late": late_fires(z.readings, z.bursts, z.setpoints, deadband, a, b),
                    "fire_reading": _fire_stats(fires),
                }
            first = per[next(iter(windows))]
            result.zones[name] = {
                "boiler": boiler,
                "periods": per,
                "band": first["band"],
                "true_band": first["true_band"],
                "bursts": first["bursts"],
                "fire_reading_min": first["fire_reading"],
                "final_mode": z.core.controller.mode,
                "trigger_correction": round(getattr(z.core, "trigger_correction", 0.0), 3),
                "peak_correction": round(z.core.peak_correction(), 3),
                "learned_q_s": round(z.core.estimator.params.q_s, 3),
                "confidence": z.core.confidence(),
            }
            if keep_raw:
                result.raw[name] = {
                    "readings": list(z.readings),
                    "setpoints": list(z.setpoints),
                    "bursts": list(z.bursts),
                    "fires": list(z.fires),
                    "air": list(z.air_trace),
                }
        b_periods = boiler_periods(plant.boiler_switches)
        result.boilers[boiler] = {
            period: cycling_metrics(b_periods, a, b) for period, (a, b) in windows.items()
        }
        result.boilers[boiler].update(result.boilers[boiler][next(iter(windows))])
        if keep_raw:
            result.raw[boiler] = list(plant.boiler_switches)
    return result


def _freeze(core: Any) -> None:
    gates = core._apply_gates

    def frozen(t: float) -> None:
        gates(t)
        core.observer.learn_params = False

    core._apply_gates = frozen


def _truth_series(trace: list[tuple[float, float]]) -> list[tuple[float, float | None]]:
    """True air temperature as a 0.1-rounded step series (what a perfect sensor shows)."""
    out: list[tuple[float, float | None]] = []
    for t, v in trace:
        r = round(v, 1)
        if not out or out[-1][1] != r:
            out.append((t, r))
    return out


def _fire_stats(fires: list[tuple]) -> dict[str, Any]:
    if not fires:
        return {"n": 0}
    shown = [f[1] for f in fires if f[1] is not None]
    gaps = [f[3] - f[2] for f in fires]
    return {
        "n": len(fires),
        "displayed_at_fire": {str(v): shown.count(v) for v in sorted(set(shown))},
        "true_minus_estimate_mean": round(sum(gaps) / len(gaps), 3),
    }
