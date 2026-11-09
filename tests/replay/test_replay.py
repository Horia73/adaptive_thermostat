"""Replay harness tests.

* The committed mini fixture (three short windows around the field
  incidents) always runs.
* The full private exports run when available; otherwise the tests skip.
  ``test_full_export_legacy`` holds the thresholds calibrated on the 15-25 Oct
  export of the old controller (``ADAPTIVE_THERMOSTAT_LEGACY_EXPORT``, or
  ``ADAPTIVE_THERMOSTAT_EXPORT`` / ``../ha-export`` when that is the old
  export).  ``test_full_export`` checks what must hold for any export,
  including ones recorded by 2.x, whose window detections already come from
  the new detector (so "old vs new" is not a meaningful comparison there).
"""

from __future__ import annotations

from pathlib import Path

from export_loader import find_export, find_legacy_export, load_export
import pytest
from replay import ReplaySettings, replay_zone, run, summarize

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "export_mini"


@pytest.fixture(scope="module")
def mini():
    return load_export(FIXTURE)


def test_frozen_sensor_is_caught(mini) -> None:
    """birou: reading frozen at 22.9 °C from 18.10 16:18 while heating continued."""
    zone = mini.zones["climate.birou"]
    # Worst case for the time check: the sensor keeps re-reporting the frozen
    # value every 30 min, so only the physics check can see it.
    res = replay_zone(zone, ReplaySettings(heartbeat_s=1800.0, warmup_s=0.0))
    physics = [e for e in res.stale_events if e["kind"] == "physics"]
    assert physics, res.stale_events
    frozen_since = physics[0]["last_change"]
    assert physics[0]["start"] - frozen_since < 6 * 3600
    # Without heartbeats the time check fires first.
    res = replay_zone(zone, ReplaySettings(warmup_s=0.0))
    assert any(e["kind"] == "time" for e in res.stale_events)


def test_valve_drift_is_found(mini) -> None:
    """baie_2: valve physically open ~18 h while the climate said idle."""
    zone = mini.zones["climate.baie_2"]
    res = replay_zone(zone, ReplaySettings(warmup_s=0.0))
    summary = summarize(zone, res, start_after=0.0)
    assert summary["drift"]["incidents"] >= 1
    assert summary["drift"]["longest_h"] > 10


def test_window_detector_on_airing_day(mini) -> None:
    """dormitor 16.10: real airing (23 -> 21.4 °C); fewer, real detections."""
    zone = mini.zones["climate.dormitor"]
    res = replay_zone(zone, ReplaySettings(warmup_s=0.0))
    window = summarize(zone, res, start_after=0.0)["window"]
    assert window["new_detections"] >= 3
    assert window["new_detections"] <= window["old_detections"]
    assert window["sharp_drops_caught"] >= 1


def test_estimator_runs_on_recorded_data(mini) -> None:
    report = run(mini, settings=ReplaySettings(warmup_s=0.0))
    for name, zone in report["zones"].items():
        assert zone["one_step"]["n"] > 0, name
        assert zone["one_step"]["model_mae"] < 0.5, name
        assert zone["params"]["q_s"] > 0


@pytest.mark.skipif(find_legacy_export() is None, reason="old-controller export not available")
def test_full_export_legacy() -> None:
    path = find_legacy_export()
    assert path is not None
    export = load_export(path)
    report = run(export)
    zones = report["zones"]
    old = sum(z["window"]["old_detections"] for z in zones.values())
    new = sum(z["window"]["new_detections"] for z in zones.values())
    assert new < old / 2
    birou = zones["birou"]["stale"]["longest_frozen"]
    assert birou["new_detection_after_h"] is not None and birou["new_detection_after_h"] < 6
    assert zones["baie_2"]["drift"]["incidents"] >= 1
    for name, zone in zones.items():
        # The model is never much worse than persistence at multi-hour
        # horizons, and better in the zones where heating dominates.
        assert zone["4h"]["all"]["model_mae"] <= 1.15 * zone["4h"]["all"]["naive_mae"], name


@pytest.mark.skipif(find_export() is None, reason="private export not available")
def test_full_export() -> None:
    export = load_export(find_export())
    report = run(export)
    zones = report["zones"]
    judged = 0
    for name, zone in zones.items():
        assert zone["params"]["q_s"] > 0, name
        one = zone["one_step"]
        if one["n"] < 20:
            continue  # too little data after the warm-up to judge
        judged += 1
        # One-step prediction at each report stays within half a degree.
        assert one["model_mae"] is not None and one["model_mae"] < 0.5, name
    assert judged > 0
