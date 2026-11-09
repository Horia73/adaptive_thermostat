"""Load a Home Assistant history export into per-zone event streams.

The export format (produced outside this repository, never committed in full):

* ``config.json``: ``{climate_id: {heater_entity_ids, central_heater_entity_id,
  temp_sensor_entity_id, outdoor_sensor, backup_outdoor_sensor, ...}}``
* ``raw_noattr.json``: ``{entity_id: [[iso_timestamp, state], ...]}``
* ``climate_attrs.json``: ``{"keys": [...], "data": {climate_id: [[ts, state, ...attrs]]}}``

Everything here is plain Python so the harness runs without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

EXPORT_ENV = "ADAPTIVE_THERMOSTAT_EXPORT"
# The 15-25 Oct 2026 export of the old (1.x) controller: the replay tests'
# legacy thresholds (window detections old vs new, frozen birou sensor, baie_2
# valve drift) were calibrated on it.
LEGACY_EXPORT_ENV = "ADAPTIVE_THERMOSTAT_LEGACY_EXPORT"
LEGACY_END = datetime(2026, 10, 27, tzinfo=UTC).timestamp()  # 2.0 went live after this
UNAVAILABLE = ("unavailable", "unknown", "")


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _num(state: str) -> float | None:
    if state in UNAVAILABLE:
        return None
    try:
        return float(state)
    except ValueError:
        return None


@dataclass
class ZoneHistory:
    """All recorded history relevant to one zone."""

    climate_id: str
    temp_sensor: str
    valves: list[str]
    boiler: str | None
    outdoor_sensor: str | None
    backup_outdoor_sensor: str | None
    config: dict[str, Any]
    temps: list[tuple[float, float | None]] = field(default_factory=list)
    valve_states: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    boiler_states: list[tuple[float, str]] = field(default_factory=list)
    outdoor: list[tuple[float, float | None]] = field(default_factory=list)
    outdoor_backup: list[tuple[float, float | None]] = field(default_factory=list)
    climate_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.climate_id.split(".", 1)[1]


@dataclass
class Export:
    """A loaded export."""

    zones: dict[str, ZoneHistory]
    start: float
    end: float


def find_export(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the export directory if it exists (explicit path, env var, sibling dir)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get(EXPORT_ENV):
        candidates.append(Path(os.environ[EXPORT_ENV]))
    # Default layout of the orchestrator run directory: <run>/repo and <run>/ha-export.
    candidates.append(Path(__file__).resolve().parents[3] / "ha-export")
    for path in candidates:
        if (path / "raw_noattr.json").is_file() and (path / "config.json").is_file():
            return path
    return None


def is_legacy_export(export: Export) -> bool:
    """True for an export recorded entirely with the old (pre-2.0) controller."""
    return export.end < LEGACY_END


def find_legacy_export() -> Path | None:
    """The old-controller export: ``$ADAPTIVE_THERMOSTAT_LEGACY_EXPORT``, else ``find_export()`` if legacy."""
    explicit = os.environ.get(LEGACY_EXPORT_ENV)
    if explicit:
        given = Path(explicit)
        return given if (given / "raw_noattr.json").is_file() else None
    found = find_export()
    if found is None:
        return None
    return found if is_legacy_export(load_export(found)) else None


def _dedupe(series: list[tuple[float, Any]]) -> list[tuple[float, Any]]:
    """Sort by time and drop consecutive duplicates (keeps the first occurrence)."""
    series.sort(key=lambda item: item[0])
    out: list[tuple[float, Any]] = []
    for ts, value in series:
        if out and out[-1][1] == value:
            continue
        out.append((ts, value))
    return out


def load_export(path: str | os.PathLike[str]) -> Export:
    """Load the export directory into per-zone histories."""
    root = Path(path)
    config = json.loads((root / "config.json").read_text())
    raw = json.loads((root / "raw_noattr.json").read_text())
    attrs_path = root / "climate_attrs.json"
    attrs = json.loads(attrs_path.read_text()) if attrs_path.is_file() else {"keys": [], "data": {}}
    keys = attrs.get("keys", [])

    zones: dict[str, ZoneHistory] = {}
    start = float("inf")
    end = float("-inf")
    for climate_id, cfg in config.items():
        if not isinstance(cfg, dict) or not cfg.get("temp_sensor_entity_id"):
            continue  # not a zone (e.g. extra metadata stored next to the zones)
        zone = ZoneHistory(
            climate_id=climate_id,
            temp_sensor=cfg["temp_sensor_entity_id"],
            valves=list(cfg.get("heater_entity_ids") or []),
            boiler=cfg.get("central_heater_entity_id"),
            outdoor_sensor=cfg.get("outdoor_sensor"),
            backup_outdoor_sensor=cfg.get("backup_outdoor_sensor"),
            config=cfg,
        )
        zone.temps = _dedupe([(_ts(t), _num(s)) for t, s in raw.get(zone.temp_sensor, [])])
        for valve in zone.valves:
            zone.valve_states[valve] = _dedupe([(_ts(t), s) for t, s in raw.get(valve, [])])
        if zone.boiler:
            zone.boiler_states = _dedupe([(_ts(t), s) for t, s in raw.get(zone.boiler, [])])
        if zone.outdoor_sensor:
            zone.outdoor = _dedupe([(_ts(t), _num(s)) for t, s in raw.get(zone.outdoor_sensor, [])])
        if zone.backup_outdoor_sensor:
            zone.outdoor_backup = _dedupe(
                [(_ts(t), _num(s)) for t, s in raw.get(zone.backup_outdoor_sensor, [])]
            )
        for row in attrs.get("data", {}).get(climate_id, []):
            item = dict(zip(keys, row))
            item["t"] = _ts(item["ts"])
            zone.climate_rows.append(item)
        zone.climate_rows.sort(key=lambda item: item["t"])
        for series in (zone.temps, zone.boiler_states, zone.outdoor):
            if series:
                start = min(start, series[0][0])
                end = max(end, series[-1][0])
        zones[climate_id] = zone
    climate_raw = [_ts(t) for cid in zones for t, _ in raw.get(cid, [])[-1:]]
    if climate_raw:
        end = max(end, *climate_raw)
    return Export(zones=zones, start=start, end=end)


def value_at(series: list[tuple[float, Any]], ts: float, default: Any = None) -> Any:
    """Return the last value at or before ``ts`` (binary search)."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else default
