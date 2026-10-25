"""Open-window detection that is robust to 0.1 °C quantisation.

The previous detector used the "instantaneous" slope between two consecutive
reports, i.e. 0.1 °C divided by whatever the reporting interval happened to
be, which produced slopes of 20 °C/h out of a single quantisation step.  Here a
drop has to be *sustained*:

* least-squares slope over a time window of reports (not two points),
* a minimum number of reports and a minimum time span inside the window,
* a minimum absolute drop from the window maximum,
* heat-spike suppression: a fall back from a short-lived spike (oven, shower,
  sun patch) is not a window unless the temperature goes clearly below the
  pre-spike level.
"""

from __future__ import annotations

from dataclasses import dataclass

from .filters import ReportHistory, regression_slope


@dataclass
class WindowConfig:
    """Detector tuning."""

    slope_threshold: float = 2.5  # °C/h, the user option
    # A least-squares slope over several reports is lower than the peak
    # two-point slope the option was originally tuned for.
    slope_factor: float = 0.8
    window_s: float = 1200.0  # regression window
    min_span_s: float = 300.0  # first to last report inside the window
    min_reports: int = 3  # real reports inside the window
    min_drop: float = 0.4  # °C below the window maximum
    spike_lookback_s: float = 3600.0
    spike_rise: float = 0.4  # °C rise that marks a temporary heat spike
    clear_min_open_s: float = 600.0
    clear_window_s: float = 900.0
    clear_slope: float = -0.3  # °C/h, slower than this means the window is closed
    max_open_s: float = 2 * 3600.0
    dismiss_quiet_s: float = 1800.0


@dataclass
class WindowEvent:
    """A detection or clearance."""

    opened: bool
    t: float
    slope_per_hour: float
    drop: float
    reason: str


@dataclass
class WindowDetector:
    """Slope-based window detector (no contact sensor needed)."""

    config: WindowConfig
    is_open: bool = False
    opened_at: float | None = None
    open_baseline: float | None = None
    open_slope: float | None = None
    closed_at: float | None = None
    _quiet_until: float | None = None

    def evaluate(self, t: float, history: ReportHistory) -> WindowEvent | None:
        """Run detection or clearance at time ``t``; returns a transition."""
        if self.is_open:
            return self._evaluate_clear(t, history)
        if self._quiet_until is not None and t < self._quiet_until:
            return None
        return self._evaluate_open(t, history)

    def _evaluate_open(self, t: float, history: ReportHistory) -> WindowEvent | None:
        cfg = self.config
        last = history.last
        if last is None or t - last[0] > 60.0:
            # Only a fresh report can start a detection.
            return None
        real = [p for p in history.reports if last[0] - cfg.window_s <= p[0] <= last[0]]
        if len(real) < cfg.min_reports or real[-1][0] - real[0][0] < cfg.min_span_s:
            return None
        current = last[1]
        # The drop counts from the highest level held during the window,
        # including the value held at its start.
        top = max(v for _, v in history.window_points(last[0], cfg.window_s))
        drop = top - current
        if drop < cfg.min_drop - 1e-6:
            return None
        slope = regression_slope(real)
        if slope is None:
            return None
        slope_h = slope * 3600.0
        if slope_h > -cfg.slope_threshold * cfg.slope_factor:
            return None
        if self._is_spike_decay(last[0], history, top, current):
            return None
        self.is_open = True
        self.opened_at = t
        self.open_baseline = top
        self.open_slope = slope_h
        return WindowEvent(True, t, slope_h, drop, "slope")

    def _is_spike_decay(self, t: float, history: ReportHistory, top: float, current: float) -> bool:
        """True if the drop is a return from a temporary heat spike."""
        cfg = self.config
        before = [v for ts, v in history.reports if t - cfg.spike_lookback_s <= ts <= t]
        if len(before) < 3:
            return False
        # Level before the spike: lowest value seen before the window maximum.
        peak_idx = max(range(len(before)), key=lambda i: before[i])
        if peak_idx == 0:
            return False
        baseline = min(before[:peak_idx])
        rise = before[peak_idx] - baseline
        if rise < cfg.spike_rise - 1e-6:
            return False
        # Still above (or only just below) the pre-spike level: spike decay.
        return current > baseline - cfg.min_drop / 2.0

    def _evaluate_clear(self, t: float, history: ReportHistory) -> WindowEvent | None:
        cfg = self.config
        assert self.opened_at is not None
        elapsed = t - self.opened_at
        if elapsed >= cfg.max_open_s:
            return self._close(t, 0.0, "timeout")
        if elapsed < cfg.clear_min_open_s:
            return None
        slope_h = history.slope_per_hour(t, cfg.clear_window_s)
        if slope_h >= cfg.clear_slope:
            return self._close(t, slope_h, "recovered")
        return None

    def _close(self, t: float, slope_h: float, reason: str) -> WindowEvent:
        self.is_open = False
        self.closed_at = t
        self.opened_at = None
        self.open_baseline = None
        return WindowEvent(False, t, slope_h, 0.0, reason)

    def force_open(self, t: float, reason: str) -> WindowEvent | None:
        """Open from a contact sensor."""
        if self.is_open:
            return None
        self.is_open = True
        self.opened_at = t
        self.open_slope = None
        return WindowEvent(True, t, 0.0, 0.0, reason)

    def force_close(self, t: float, reason: str) -> WindowEvent | None:
        if not self.is_open:
            return None
        return self._close(t, 0.0, reason)

    def dismiss(self, t: float) -> WindowEvent | None:
        """User confirmed a false alarm: close and stay quiet for a while."""
        self._quiet_until = t + self.config.dismiss_quiet_s
        if not self.is_open:
            return None
        return self._close(t, 0.0, "dismissed")
