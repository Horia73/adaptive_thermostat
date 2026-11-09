"""Reading-space comfort and cycling metrics.

Everything is evaluated on what the owner sees: the displayed (0.1 °C
quantised) sensor value, held until the next change, during heat-mode time
(mode heat, reading available).  The same functions serve the recorded
history and the simulations, so before/after numbers are comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

EPS = 1e-6


def _step_value(series: Sequence[tuple[float, Any]], t: float, default: Any = None) -> Any:
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else default


def band_metrics(
    readings: Sequence[tuple[float, float | None]],
    setpoints: Sequence[tuple[float, float | None, bool]],
    start: float,
    end: float,
    deadband: float = 0.1,
    excluded: Sequence[tuple[float, float]] = (),
    heated: Sequence[tuple[float, float]] = (),
) -> dict[str, Any]:
    """Time-weighted band statistics of the displayed reading.

    ``readings``: ``[(t, value or None)]`` change points.  ``setpoints``:
    ``[(t, target, heat_mode)]`` change points.  ``excluded``: intervals left
    out (e.g. window open).  ``heated``: windows in which time above the band
    counts as heating-caused (a burst and its tail); the rest is passive.
    """
    marks = {start, end}
    marks.update(t for t, _ in readings if start < t < end)
    marks.update(t for t, *_ in setpoints if start < t < end)
    for a, b in (*excluded, *heated):
        if a < end and b > start:
            marks.update(x for x in (a, b) if start < x < end)
    times = sorted(marks)
    heated_sorted = sorted(heated)
    sp = [(t, (target, heat)) for t, target, heat in setpoints]
    total = within = within2 = over = under = 0.0
    over_area = under_area = 0.0
    over_heated = 0.0
    max_over = max_under = 0.0
    for a, b in zip(times, times[1:]):
        mid = 0.5 * (a + b)
        if any(x0 <= mid < x1 for x0, x1 in excluded):
            continue
        value = _step_value(readings, mid)
        target, heat = _step_value(sp, mid, (None, False))
        if value is None or target is None or not heat:
            continue
        dt = b - a
        err = value - target
        total += dt
        if abs(err) <= deadband + EPS:
            within += dt
        if abs(err) <= 2 * deadband + EPS:
            within2 += dt
        if err > deadband + EPS:
            over += dt
            if _inside(heated_sorted, mid):
                over_heated += dt
            over_area += (err - deadband) * dt
            max_over = max(max_over, err - deadband)
        elif err < -deadband - EPS:
            under += dt
            under_area += (-deadband - err) * dt
            max_under = max(max_under, -deadband - err)
    hours = total / 3600.0
    return {
        "heat_hours": round(hours, 2),
        "within_db_pct": _pct(within, total),
        "within_2db_pct": _pct(within2, total),
        "over_pct": _pct(over, total),
        "under_pct": _pct(under, total),
        "over_heating_pct": _pct(over_heated, total),
        # Excess beyond the band edge (K): maximum, and mean while outside.
        "max_over": round(max_over, 2),
        "mean_over": round(over_area / over, 3) if over else 0.0,
        "max_under": round(max_under, 2),
        "mean_under": round(under_area / under, 3) if under else 0.0,
    }


def _inside(intervals: Sequence[tuple[float, float]], t: float) -> bool:
    lo, hi = 0, len(intervals)
    while lo < hi:
        mid = (lo + hi) // 2
        if intervals[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return any(a <= t < b for a, b in intervals[max(0, lo - 3) : lo])


def _pct(part: float, total: float) -> float | None:
    return round(100.0 * part / total, 1) if total > 0 else None


def heat_periods(u_changes: Sequence[tuple[float, float]], merge_s: float = 0.0) -> list[tuple[float, float]]:
    """``[(start, end)]`` of heat-on periods from ``[(t, u)]`` change points."""
    out: list[tuple[float, float]] = []
    on: float | None = None
    for t, u in u_changes:
        if u > 0 and on is None:
            on = t
        elif u <= 0 and on is not None:
            if out and t - out[-1][1] < merge_s:
                out[-1] = (out[-1][0], t)
            else:
                out.append((on, t))
            on = None
    return out


def cycling_metrics(periods: Sequence[tuple[float, float]], start: float, end: float) -> dict[str, Any]:
    """Count/length statistics of on-periods starting within ``[start, end)``."""
    runs = sorted(b - a for a, b in periods if start <= a < end)
    days = max(1e-9, (end - start) / 86400.0)
    return {
        "per_day": round(len(runs) / days, 1),
        "count": len(runs),
        "under_2min": sum(1 for r in runs if r < 120.0 - 1.0),
        "under_1min": sum(1 for r in runs if r < 60.0),
        "median_min": round(runs[len(runs) // 2] / 60.0, 2) if runs else None,
        "on_hours_per_day": round(sum(runs) / 3600.0 / days, 2),
    }


def boiler_periods(switches: Sequence[tuple[float, bool]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    on: float | None = None
    for t, state in switches:
        if state and on is None:
            on = t
        elif not state and on is not None:
            out.append((on, t))
            on = None
    return out
