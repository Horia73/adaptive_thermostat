"""Temperature-sensor health: unavailable, stale by time, stale by physics.

Two independent stale checks:

* **time**: no report (``last_reported``, which Home Assistant updates even when
  the value is unchanged) for longer than the configured timeout once heat
  has been delivered since the last report; a silent sensor in an idle,
  stable room (report-on-change sensors routinely stay quiet for 4-6 h) is
  only flagged after twice the timeout;
* **physics**: consecutive heat episodes that the zone model says must each
  have warmed the air noticeably (the heating *effect*: with vs without that
  heat, which is well identified even by the prior), yet the reading did not
  change at all.  This catches sensors that keep reporting a frozen value,
  which the time check cannot see.  It does not depend on knowing the room's
  losses, so it works on a fresh install and is not fooled by heat soaking
  into a cold structure during warm-up.
"""

from __future__ import annotations

from dataclasses import dataclass

FAULT_SENSOR_UNAVAILABLE = "sensor_unavailable"
FAULT_SENSOR_STALE = "sensor_stale"


@dataclass
class SafetyConfig:
    """Thresholds."""

    stale_timeout_s: float = 3 * 3600.0
    unavailable_grace_s: float = 300.0
    # A heat episode "must" move the reading if the model predicts at least
    # this much air warming from it (micro-bursts of ~1 min stay below).
    episode_effect: float = 0.3
    unanswered_episodes: int = 2


@dataclass
class SensorHealth:
    """Tracks the temperature sensor of one zone."""

    config: SafetyConfig
    last_value: float | None = None
    last_change_t: float | None = None
    last_report_t: float | None = None
    unavailable_since: float | None = None
    heat_since_report: float = 0.0  # effective heat hours since the last report
    unanswered: int = 0  # heat episodes in a row without any change of reading
    last_effect: float = 0.0  # predicted effect of the last evaluated episode
    physics_stale: bool = False

    def on_report(self, t: float, value: float | None, reported_t: float | None = None) -> None:
        """A state change or a same-value report from the sensor."""
        if value is None:
            if self.unavailable_since is None:
                self.unavailable_since = t
            return
        self.unavailable_since = None
        self.last_report_t = max(self.last_report_t or 0.0, reported_t or t)
        self.heat_since_report = 0.0
        if self.last_value is None or abs(value - self.last_value) > 1e-6:
            self.last_value = value
            self.last_change_t = t
            self.unanswered = 0
            self.physics_stale = False

    def on_heat(self, hours_effective: float) -> None:
        """Heat was delivered (counts towards the time-based limit)."""
        if hours_effective > 0:
            self.heat_since_report += hours_effective

    def on_episode(self, predicted_effect: float, reading_changed: bool) -> None:
        """A heat episode was evaluated against the model."""
        self.last_effect = predicted_effect
        if reading_changed:
            self.unanswered = 0
            return
        if predicted_effect >= self.config.episode_effect:
            self.unanswered += 1
            if self.unanswered >= self.config.unanswered_episodes:
                self.physics_stale = True

    @property
    def suspect(self) -> bool:
        """Heat has gone in without response: stop learning from this sensor."""
        return self.unanswered >= 1 or self.physics_stale

    def fault(self, t: float) -> tuple[str, str] | None:
        """Return ``(fault, message)`` or None."""
        cfg = self.config
        if self.unavailable_since is not None:
            if t - self.unavailable_since >= cfg.unavailable_grace_s or self.last_value is None:
                return FAULT_SENSOR_UNAVAILABLE, "Temperature sensor unavailable"
            return None
        if self.last_value is None:
            return FAULT_SENSOR_UNAVAILABLE, "No temperature reading yet"
        if self.last_report_t is not None:
            silent = t - self.last_report_t
            limit = cfg.stale_timeout_s if self.heat_since_report > 0 else 2 * cfg.stale_timeout_s
            if silent >= limit:
                hours = silent / 3600.0
                return FAULT_SENSOR_STALE, f"No temperature update for {hours:.1f} h"
        if self.physics_stale:
            return (
                FAULT_SENSOR_STALE,
                f"Temperature stuck at {self.last_value:.1f} °C while heating",
            )
        return None
