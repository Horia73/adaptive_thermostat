"""Window detector, sensor health, filters."""

from __future__ import annotations

from engine.filters import OutdoorFilter, ReportHistory
from engine.safety import SafetyConfig, SensorHealth
from engine.window import WindowConfig, WindowDetector


def _history(points):
    h = ReportHistory()
    for t, v in points:
        h.add(t, v)
    return h


def test_quantisation_step_is_not_a_window():
    d = WindowDetector(WindowConfig())
    h = _history([(0, 22.0), (1800, 22.1), (1860, 21.9)])
    assert d.evaluate(1860, h) is None


def test_sustained_drop_is_a_window():
    d = WindowDetector(WindowConfig())
    pts = [(0, 22.0), (1800, 22.0), (2000, 21.8), (2200, 21.6), (2400, 21.4)]
    h = ReportHistory()
    event = None
    for t, v in pts:
        h.add(t, v)
        event = d.evaluate(t, h) or event
    assert event is not None and event.opened
    assert event.slope_per_hour <= -2.0
    assert event.drop >= 0.4 - 1e-6


def test_spike_decay_is_suppressed():
    d = WindowDetector(WindowConfig())
    pts = [(0, 22.0), (600, 22.3), (1200, 22.7), (1500, 22.5), (1700, 22.3), (1900, 22.1)]
    h = ReportHistory()
    for t, v in pts:
        h.add(t, v)
        assert d.evaluate(t, h) is None


def test_window_clears_when_flat_and_dismiss_keeps_quiet():
    cfg = WindowConfig()
    d = WindowDetector(cfg)
    assert d.force_open(0, "contact") is not None
    h = _history([(0, 20.0)])
    assert d.evaluate(300, h) is None  # too early
    event = d.evaluate(1200, h)
    assert event is not None and not event.opened
    d.force_open(2000, "contact")
    d.dismiss(2100)
    assert not d.is_open
    pts = [(2200, 22.0), (2300, 21.8), (2400, 21.6), (2500, 21.4)]
    h2 = ReportHistory()
    for t, v in pts:
        h2.add(t, v)
        assert d.evaluate(t, h2) is None  # quiet period after dismissal


def test_stale_by_time_idle_vs_heating():
    h = SensorHealth(SafetyConfig(stale_timeout_s=3 * 3600))
    h.on_report(0, 21.0)
    assert h.fault(4 * 3600) is None  # idle and silent: tolerated up to 2x
    assert h.fault(6 * 3600 + 1)[0] == "sensor_stale"
    h.on_report(10, 21.0, reported_t=7 * 3600)
    h.on_heat(0.1)
    assert h.fault(7 * 3600 + 3 * 3600 + 1)[0] == "sensor_stale"


def test_stale_by_physics_and_recovery():
    h = SensorHealth(SafetyConfig())
    h.on_report(0, 22.9)
    h.on_episode(0.1, reading_changed=False)  # micro-burst: too small to judge
    assert not h.suspect
    h.on_episode(0.5, reading_changed=False)
    assert h.suspect and h.fault(1) is None
    h.on_report(1, 22.9)  # frozen value keeps being reported
    h.on_episode(0.6, reading_changed=False)
    fault = h.fault(2)
    assert fault is not None and fault[0] == "sensor_stale" and "stuck" in fault[1]
    h.on_report(3, 23.1)
    assert h.fault(4) is None and not h.suspect
    h.on_episode(0.6, reading_changed=False)
    h.on_episode(0.6, reading_changed=True)
    assert h.unanswered == 0


def test_unavailable_grace():
    h = SensorHealth(SafetyConfig(unavailable_grace_s=300))
    h.on_report(0, 21.0)
    h.on_report(10, None)
    assert h.fault(200) is None
    assert h.fault(400)[0] == "sensor_unavailable"


def test_outdoor_filter_is_time_based():
    a = OutdoorFilter(tau_s=1200)
    b = OutdoorFilter(tau_s=1200)
    a.update(0, 0.0)
    b.update(0, 0.0)
    a.update(10, 10.0)
    b.update(10, 10.0)
    for t in range(11, 1211):  # called every second
        a.advance(t)
    b.advance(1210)  # called once
    assert abs(a.value - b.value) < 1e-9
    assert 6.0 < b.value < 6.5  # 1 - e^-1


def test_slope_uses_regression_and_held_value():
    h = _history([(0, 22.0), (900, 22.1), (1800, 22.2)])
    assert 0.35 < h.slope_per_hour(1800, 1800) < 0.45  # 0.2 K in 30 min
    # Long silence flattens the slope (the reading is held).
    assert abs(h.slope_per_hour(1800 + 6 * 3600, 1800)) < 1e-9
