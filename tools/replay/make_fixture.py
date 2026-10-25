"""Cut a small, anonymised-by-omission fixture out of a full export.

Only three short windows around the field incidents are kept (the full
export stays private):

* birou    18.10 06:00 - 19.10 20:00  frozen sensor onset + heavy heating
* dormitor 16.10 05:00 - 16.10 20:00  airing day (sharp temperature drops)
* baie_2   24.10 12:00 - 25.10 10:00  valve re-opened by itself for ~18 h

Usage: python tools/replay/make_fixture.py [--export DIR] [--out tests/fixtures/export_mini]
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from export_loader import find_export  # noqa: E402

WINDOWS = {
    "climate.birou": ("2026-10-18T06:00:00+03:00", "2026-10-19T20:00:00+03:00"),
    "climate.dormitor": ("2026-10-16T05:00:00+03:00", "2026-10-16T20:00:00+03:00"),
    "climate.baie_2": ("2026-10-24T12:00:00+03:00", "2026-10-25T10:00:00+03:00"),
}
ATTR_KEYS = (
    "ts",
    "state",
    "temperature",
    "window_open_detected",
    "zone_heater_on",
    "planned_zone_on_duration",
    "last_cycle_peak_predicted",
    "last_cycle_peak_actual",
)
CONFIG_KEYS = (
    "heater_entity_ids",
    "central_heater_entity_id",
    "temp_sensor_entity_id",
    "outdoor_sensor",
    "backup_outdoor_sensor",
    "window_detection_enabled",
    "window_slope_threshold",
)


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export")
    parser.add_argument("--out", default=str(HERE.parents[1] / "tests" / "fixtures" / "export_mini"))
    args = parser.parse_args(argv)
    src = find_export(args.export)
    if src is None:
        print("No export found")
        return 1
    config = json.loads((src / "config.json").read_text())
    raw = json.loads((src / "raw_noattr.json").read_text())
    attrs = json.loads((src / "climate_attrs.json").read_text())
    out_config: dict[str, dict] = {}
    out_raw: dict[str, list] = {}
    keep = [attrs["keys"].index(k) for k in ATTR_KEYS]
    out_attrs: dict[str, object] = {"keys": list(ATTR_KEYS), "data": {}}
    for climate_id, (start, end) in WINDOWS.items():
        t0, t1 = _ts(start), _ts(end)
        cfg = {k: config[climate_id].get(k) for k in CONFIG_KEYS}
        out_config[climate_id] = cfg
        entities = [
            cfg["temp_sensor_entity_id"],
            cfg["outdoor_sensor"],
            cfg["backup_outdoor_sensor"],
            cfg["central_heater_entity_id"],
            *cfg["heater_entity_ids"],
        ]
        for entity in entities:
            if not entity:
                continue
            rows = raw.get(entity, [])
            # Keep the last state before the window so the history starts known.
            before = [r for r in rows if _ts(r[0]) < t0][-1:]
            inside = [r for r in rows if t0 <= _ts(r[0]) <= t1]
            merged = {r[0]: r for r in out_raw.get(entity, [])}
            for r in before + inside:
                merged[r[0]] = r
            out_raw[entity] = sorted(merged.values(), key=lambda r: _ts(r[0]))
        trimmed: list[list[object]] = []
        for row in attrs["data"].get(climate_id, []):
            if not t0 <= _ts(row[0]) <= t1:
                continue
            values = [row[i] for i in keep]
            if trimmed and trimmed[-1][1:] == values[1:]:
                continue  # only the columns the replay uses, deduplicated
            trimmed.append(values)
        out_attrs["data"][climate_id] = trimmed  # type: ignore[index]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(out_config, indent=1))
    (out / "raw_noattr.json").write_text(json.dumps(out_raw, separators=(",", ":")))
    (out / "climate_attrs.json").write_text(json.dumps(out_attrs, separators=(",", ":")))
    size = sum(p.stat().st_size for p in out.iterdir())
    print(f"wrote {out} ({size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
