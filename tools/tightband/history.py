"""Reading-space analysis of a recorded Home Assistant export.

For every zone: band statistics of the displayed reading during heat-mode
time (window-open time excluded when contacts are configured), bursts per day,
the observed rise of each burst in reading space (the empirical answer to
"how much does the smallest burst lift the reading?"), and "late fires":
bursts that only started after the reading had already left the band at the
bottom.  Boilers: starts per day and runs shorter than two minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import statistics
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (HERE, REPO / "tools" / "replay", REPO / "custom_components" / "adaptive_thermostat"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from export_loader import Export, ZoneHistory, value_at  # noqa: E402
from metrics import band_metrics, boiler_periods, cycling_metrics, heat_periods  # noqa: E402
from replay import heat_input_timeline  # noqa: E402

V21_LIVE = datetime(2026, 11, 7, 15, 57, 41, tzinfo=UTC).timestamp()
MIN_BURST_MAX_S = 210.0  # bursts up to 3.5 min count as "minimum-length"
RISE_WINDOW_S = 45 * 60.0  # long enough for the tails, short enough to limit passive drift
CONTACT_KEYS = ("door_window_sensors", "door_window_sensor")


def setpoints(zone: ZoneHistory) -> list[tuple[float, float | None, bool]]:
    out: list[tuple[float, float | None, bool]] = []
    for row in zone.climate_rows:
        target = row.get("temperature")
        item = (
            row["t"],
            float(target) if isinstance(target, (int, float)) else None,
            row.get("state") == "heat",
        )
        if not out or out[-1][1:] != item[1:]:
            out.append(item)
    return out


def contact_ids(zone: ZoneHistory) -> list[str]:
    ids: list[str] = []
    for key in CONTACT_KEYS:
        value = zone.config.get(key)
        if isinstance(value, str) and value:
            ids.append(value)
        elif isinstance(value, list):
            ids.extend(v for v in value if isinstance(v, str) and v)
    return sorted(set(ids))


def window_intervals(
    zone: ZoneHistory, raw: dict[str, list[list[str]]], export: Export
) -> list[tuple[float, float]]:
    """Intervals with any own (or linked zone's) contact open."""
    ids = list(contact_ids(zone))
    linked = zone.config.get("linked_window_zones") or []
    for cid in linked if isinstance(linked, list) else []:
        other = export.zones.get(cid)
        if other is not None:
            ids.extend(contact_ids(other))
    events: list[tuple[float, str, bool]] = []
    for eid in set(ids):
        for ts, state in raw.get(eid, []):
            events.append((datetime.fromisoformat(ts).timestamp(), eid, state == "on"))
    events.sort()
    open_now: set[str] = set()
    out: list[tuple[float, float]] = []
    since: float | None = None
    for t, eid, is_open in events:
        was = bool(open_now)
        if is_open:
            open_now.add(eid)
        else:
            open_now.discard(eid)
        if open_now and not was:
            since = t
        elif was and not open_now and since is not None:
            out.append((since, t))
            since = None
    if since is not None:
        out.append((since, export.end))
    return out


def burst_rises(
    readings: list[tuple[float, float | None]], periods: list[tuple[float, float]], start: float, end: float
) -> list[dict[str, float]]:
    """Observed reading-space rise of each burst (peak reading minus reading at start)."""
    out = []
    temps = [(t, v) for t, v in readings if v is not None]
    for i, (b0, b1) in enumerate(periods):
        if not start <= b0 < end:
            continue
        stop = b1 + RISE_WINDOW_S
        if i + 1 < len(periods):
            stop = min(stop, periods[i + 1][0])
        r0 = value_at(temps, b0)
        seen = [v for t, v in temps if b0 < t <= stop]
        if r0 is None or not seen:
            continue
        out.append({"t": b0, "len_s": b1 - b0, "start": r0, "rise": round(max(seen) - r0, 2)})
    return out


def rise_stats(rises: list[dict[str, float]]) -> dict[str, Any]:
    short = sorted(r["rise"] for r in rises if r["len_s"] <= MIN_BURST_MAX_S)
    every = sorted(r["rise"] for r in rises)

    def q(values: list[float], f: float) -> float | None:
        return values[min(len(values) - 1, int(f * len(values)))] if values else None

    return {
        "n_min_bursts": len(short),
        "min_burst_rise_median": q(short, 0.5),
        "min_burst_rise_p90": q(short, 0.9),
        "n_bursts": len(every),
        "rise_median": q(every, 0.5),
    }


def late_fires(
    readings: list[tuple[float, float | None]],
    periods: list[tuple[float, float]],
    sp: list[tuple[float, float | None, bool]],
    deadband: float,
    start: float,
    end: float,
) -> dict[str, Any]:
    """Bursts that started only after the reading dropped below the band."""
    temps = [(t, v) for t, v in readings if v is not None]
    sp_series = [(t, (target, heat)) for t, target, heat in sp]
    events = []
    for b0, _b1 in periods:
        if not start <= b0 < end:
            continue
        target, heat = value_at(sp_series, b0, (None, False))
        r0 = value_at(temps, b0)
        if target is None or not heat or r0 is None:
            continue
        if r0 < target - deadband - 1e-6:
            # When did the reading drop, and how long had it shown the lowest
            # in-band value before?
            idx = max(i for i, (t, _v) in enumerate(temps) if t <= b0)
            drop_t = temps[idx][0]
            hold = None
            if idx > 0 and abs(temps[idx - 1][1] - (target - deadband)) < 1e-6:
                hold = drop_t - temps[idx - 1][0]
            events.append(
                {
                    "burst_at": _local(b0),
                    "reading": r0,
                    "target": target,
                    "after_drop_s": round(b0 - drop_t),
                    "held_lowest_in_band_min": round(hold / 60.0, 1) if hold is not None else None,
                }
            )
    n_fires = sum(1 for b0, _ in periods if start <= b0 < end)
    return {
        "late_fires": len(events),
        "fires": n_fires,
        "late_share_pct": round(100.0 * len(events) / n_fires, 1) if n_fires else None,
        "within_5min_of_drop": sum(1 for e in events if e["after_drop_s"] <= 300),
        "examples": events[:8],
    }


def _local(ts: float) -> str:
    # Bucharest: UTC+3 until 25.10.2026, UTC+2 afterwards.
    offset = 3 if ts < datetime(2026, 10, 25, 1, tzinfo=UTC).timestamp() else 2
    return datetime.fromtimestamp(ts + offset * 3600, UTC).strftime("%d.%m %H:%M")


def analyse(
    export: Export,
    raw: dict[str, list[list[str]]],
    *,
    deadband: float = 0.1,
    periods_of_interest: dict[str, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """BEFORE metrics of a recorded export (per zone and boiler, per period)."""
    poi = periods_of_interest or {"all": (export.start, export.end)}
    out: dict[str, Any] = {
        "zones": {},
        "boilers": {},
        "periods": {k: [_local(a), _local(b)] for k, (a, b) in poi.items()},
    }
    for zone in export.zones.values():
        sp = setpoints(zone)
        windows = window_intervals(zone, raw, export)
        heat = heat_periods(heat_input_timeline(zone))
        zres: dict[str, Any] = {"window_open_hours": round(sum(b - a for a, b in windows) / 3600.0, 1)}
        for label, (a, b) in poi.items():
            zres[label] = {
                "band": band_metrics(zone.temps, sp, a, b, deadband, excluded=windows),
                "bursts": cycling_metrics(heat, a, b),
                "rise": rise_stats(burst_rises(zone.temps, heat, a, b)),
                "late": late_fires(zone.temps, heat, sp, deadband, a, b),
            }
        out["zones"][zone.name] = zres
    seen: set[str] = set()
    for zone in export.zones.values():
        if not zone.boiler or zone.boiler in seen or not zone.boiler_states:
            continue
        seen.add(zone.boiler)
        switches = [(t, s == "on") for t, s in zone.boiler_states if s in ("on", "off")]
        periods = boiler_periods(switches)
        out["boilers"][zone.boiler] = {label: cycling_metrics(periods, a, b) for label, (a, b) in poi.items()}
    return out


def recorded_inputs(export: Export, raw: dict[str, list[list[str]]]) -> dict[str, Any]:
    """Outdoor series and per-zone schedules (target, mode, windows) for the simulator."""
    outdoor: list[tuple[float, float]] = []
    for zone in export.zones.values():
        series = [(t, v) for t, v in zone.outdoor if v is not None]
        if len(series) > len(outdoor):
            outdoor = series
    schedules: dict[str, list[tuple[float, str, Any]]] = {}
    initial: dict[str, tuple[float, bool]] = {}
    for zone in export.zones.values():
        events: list[tuple[float, str, Any]] = []
        sp = setpoints(zone)
        prev_target: float | None = None
        prev_heat: bool | None = None
        for t, target, heat in sp:
            if target is not None and target != prev_target:
                events.append((t, "target", target))
                prev_target = target
            if heat != prev_heat:
                events.append((t, "hvac", heat))
                prev_heat = heat
        for a, b in window_intervals(zone, raw, export):
            events.append((a, "window", True))
            events.append((b, "window", False))
        events.sort(key=lambda e: e[0])
        first_target = next((e[2] for e in events if e[1] == "target"), 23.0)
        first_heat = next((e[2] for e in events if e[1] == "hvac"), True)
        initial[zone.name] = (float(first_target), bool(first_heat))
        schedules[zone.name] = events
    return {"outdoor": outdoor, "schedules": schedules, "initial": initial}


def mean_or_none(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None
