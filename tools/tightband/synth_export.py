"""Write a synthetic export in the Home Assistant export format.

Runs the simulator (by default with the old controller) and stores what the
recorder would have: temperature/valve/boiler/outdoor/contact state changes
(``raw_noattr.json``), climate rows (``climate_attrs.json``) and the zone
configuration (``config.json``).  Used to test the report harness end to end
without the private data::

    python tools/tightband/synth_export.py --out /tmp/synthetic-export --days 4
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from legacy import load_engine  # noqa: E402
from scenario import DEFAULT_BOILERS, DEFAULT_MODELS, run_scenario  # noqa: E402


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def write_export(out: Path, *, days: float, seed: int, engine_ref: str, scale: float, noise: float) -> None:
    t0 = datetime(2026, 11, 3, tzinfo=UTC).timestamp()
    windows = {"dormitor": [(t0 + 1.5 * 86400, "window", True), (t0 + 1.5 * 86400 + 900, "window", False)]}
    targets = {"dormitor": 22.5}
    schedules: dict[str, list[tuple[float, str, Any]]] = {
        "living_2": [(t0 + 2.2 * 86400, "target", 23.5), (t0 + 2.6 * 86400, "target", 23.0)],
        **windows,
    }
    res = run_scenario(
        load_engine(engine_ref),
        days=days,
        warmup_days=0.0,
        seed=seed,
        t0=t0,
        heat_scales=dict.fromkeys(DEFAULT_MODELS, scale),
        noise_kh=noise,
        targets=targets,
        schedules=schedules,
        keep_raw=True,
    )
    config: dict[str, dict] = {}
    raw: dict[str, list[list[str]]] = {"sensor.outdoor": []}
    attrs: dict[str, Any] = {"keys": ["ts", "state", "temperature", "hvac_action"], "data": {}}
    for boiler, names in DEFAULT_BOILERS.items():
        raw[boiler] = [[_iso(t), "on" if on else "off"] for t, on in res.raw[boiler]]
        raw[boiler].insert(0, [_iso(t0), "off"])
        for name in names:
            data = res.raw[name]
            cid = f"climate.{name}"
            valve = f"valve.{name}"
            contact = f"binary_sensor.{name}_window"
            cfg = {
                "heater_entity_ids": [valve],
                "central_heater_entity_id": boiler,
                "temp_sensor_entity_id": f"sensor.{name}",
                "outdoor_sensor": "sensor.outdoor",
                "door_window_sensors": [contact],
                "thermal_params_synthetic": {
                    "q_s": DEFAULT_MODELS[name].q_s,
                    "tau_f_min": DEFAULT_MODELS[name].tau_f_min,
                    "tau_e_min": DEFAULT_MODELS[name].tau_e_min,
                    "tau_r_h": DEFAULT_MODELS[name].tau_r_h,
                    "tau_am_h": DEFAULT_MODELS[name].tau_am_h,
                    "rho": DEFAULT_MODELS[name].rho,
                    "base_gain": DEFAULT_MODELS[name].base_gain,
                    "shared_gain": DEFAULT_MODELS[name].shared_gain,
                },
            }
            config[cid] = cfg
            raw[f"sensor.{name}"] = [
                [_iso(t), "unavailable" if v is None else f"{v:.1f}"] for t, v in data["readings"]
            ]
            valve_rows = [[_iso(t0), "closed"]]
            for a, b in data["bursts"]:
                valve_rows += [
                    [_iso(a - 20), "opening"],
                    [_iso(a - 2), "open"],
                    [_iso(b), "closing"],
                    [_iso(b + 18), "closed"],
                ]
            raw[valve] = valve_rows
            raw[contact] = [[_iso(t0), "off"]] + [
                [_iso(t), "on" if v else "off"] for t, kind, v in schedules.get(name, []) if kind == "window"
            ]
            rows = [
                [_iso(t), "heat" if heat else "off", target, "idle"] for t, target, heat in data["setpoints"]
            ]
            attrs["data"][cid] = rows
    # Outdoor: hourly samples of the simulator's default outdoor curve.
    from simulator import Plant

    plant = Plant([], t0=t0, outdoor_mean=7.0, outdoor_swing=4.0)
    t = t0
    while t <= t0 + days * 86400:
        raw["sensor.outdoor"].append([_iso(t), f"{plant.outdoor(t):.1f}"])
        t += 1800
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(config, indent=1))
    (out / "raw_noattr.json").write_text(json.dumps(raw))
    (out / "climate_attrs.json").write_text(json.dumps(attrs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--days", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--engine", default="9e505dd")
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--noise", type=float, default=0.3)
    args = parser.parse_args(argv)
    write_export(
        Path(args.out),
        days=args.days,
        seed=args.seed,
        engine_ref=args.engine,
        scale=args.scale,
        noise=args.noise,
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
