"""Small time-aware signal helpers (outdoor filter, robust slopes)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math


@dataclass
class OutdoorFilter:
    """Time-based exponential filter for a piecewise-constant reported signal.

    Between reports the sensor value is held, so the filter state at time ``t``
    is the exact first-order response to that held value.  The smoothing is a
    function of elapsed time only, never of how often it is called.
    """

    tau_s: float = 1200.0
    value: float | None = None
    _held: float | None = None
    _t: float | None = None

    def update(self, t: float, sample: float | None) -> float | None:
        """Advance to ``t`` and, if given, start holding ``sample``."""
        self.advance(t)
        if sample is not None:
            if self.value is None:
                self.value = sample
            self._held = sample
            self._t = t
        return self.value

    def advance(self, t: float) -> float | None:
        if self.value is None or self._held is None or self._t is None:
            return self.value
        dt = t - self._t
        if dt > 0:
            alpha = 1.0 - math.exp(-dt / self.tau_s)
            self.value += alpha * (self._held - self.value)
            self._t = t
        return self.value


def regression_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (value per second) of ``(t, value)`` points."""
    n = len(points)
    if n < 2:
        return None
    mean_t = sum(p[0] for p in points) / n
    mean_v = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_t) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mean_t) * (p[1] - mean_v) for p in points)
    return sxy / sxx


@dataclass
class ReportHistory:
    """Recent sensor reports with step-function aware slope estimation."""

    keep_s: float = 3.0 * 3600.0
    reports: deque[tuple[float, float]] = field(default_factory=deque)

    def add(self, t: float, value: float) -> None:
        if self.reports and t <= self.reports[-1][0]:
            self.reports[-1] = (t, value)
        else:
            self.reports.append((t, value))
        while self.reports and t - self.reports[0][0] > self.keep_s:
            self.reports.popleft()

    def clear(self) -> None:
        self.reports.clear()

    @property
    def last(self) -> tuple[float, float] | None:
        return self.reports[-1] if self.reports else None

    def window_points(self, now: float, window_s: float) -> list[tuple[float, float]]:
        """Points describing the held signal over ``[now - window_s, now]``.

        Includes the value held at the window start (the last report before
        it) and the currently held value at ``now``: a silent report-on-change
        sensor means the temperature stayed put, which flattens the slope.
        """
        start = now - window_s
        pts: list[tuple[float, float]] = []
        before: tuple[float, float] | None = None
        for t, v in self.reports:
            if t < start:
                before = (t, v)
            elif t <= now:
                pts.append((t, v))
        if before is not None:
            pts.insert(0, (start, before[1]))
        if pts and now > pts[-1][0]:
            pts.append((now, pts[-1][1]))
        return pts

    def slope_per_hour(self, now: float, window_s: float) -> float:
        pts = self.window_points(now, window_s)
        slope = regression_slope(pts)
        return 0.0 if slope is None else slope * 3600.0
