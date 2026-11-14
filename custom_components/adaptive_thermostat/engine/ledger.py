"""Cycle ledger: one bounded diagnostic record per heating burst.

The recorder keeps ``hvac_action``, ``zone_heater_on`` and the target, but
the prediction behind a stop (``predicted_peak``, ``control_reason``,
``control_mode``) is deliberately unrecorded, so the predicted peak cannot
be compared with the peak observed afterwards.  The ledger keeps exactly that
comparison per burst ("cycle"):

* at the start: mode, reason, target, reading and the plan (planned heat
  time, predicted peak, band);
* at the stop: mode, reason, forced or not, commanded time (request to
  stop) against heat time (heat actually flowing), planned time, the model
  prediction at the stop and the model's peak for the burst as actually run;
* after the stop, for an observation window sized from the predicted time
  to peak: the highest / lowest reading and when it occurred, number of
  reports and the longest gap between them;
* every perturbation seen from the request to the end of the observation
  (target change, own / linked window, manual / auto off, faults such as
  ``sensor_stale``, unavailable readings, report gaps, a new burst before
  the observation ended, restart, model reset) and the highest reading
  before the first perturbation.

A new burst ends the observation of the previous one (``end`` =
``next_cycle``, ``observed.truncated_after_stop_s``): readings before its
request cannot contain its heat.  It is a perturbation (``reheat``) only if
it was requested before the predicted peak time (the stop decision's, or in
fallback mode the model's peak for the burst as run) or before the maximum
was confirmed.  A maximum is *confirmed* by any later report (change or
heartbeat) that is not higher: the sensors report single 0.1 °C steps only
every 30 minutes, so the age of a maximum alone proves nothing.

``clean`` is true only for a burst that heated, stopped on its own, kept
mode and target, saw no window, fault, unavailable reading, long report gap
or early new burst, and whose observation ended (window elapsed or next
burst) with a confirmed maximum.  ``clean`` does not prove the absence of passive
gains (sun, cooking, people, neighbouring rooms): a reading cannot tell
those apart from the tail of the burst, and a temperature trace alone never
proves a door or window position.

The ledger only *reads* the zone core (no method it calls changes state),
so it cannot change a heating decision.  It is pure Python with injectable
time; the Home Assistant layer persists :meth:`CycleLedger.to_dict` and
exports it through the integration's diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .zone import Burst, ZoneCore, ZoneStatus

LEDGER_FORMAT = 1
MAX_CYCLES = 96  # records kept per zone (open record included)
MAX_AGE_S = 14 * 86400.0  # closed records older than this are dropped
OBSERVE_AFTER_PEAK_S = 1800.0  # observe this long past the predicted peak
OBSERVE_DEFAULT_S = 5400.0  # observation when no peak time is predicted
OBSERVE_MIN_S = 1800.0
OBSERVE_MAX_S = 3 * 3600.0
REPORT_GAP_FLAG_S = 2400.0  # longer silence (not even a heartbeat) is flagged
MAX_FLAGS = 16
VALIDATION_AFTER_PEAK_S = 1800.0  # ZoneCore observes its model check this long past the peak

STATE_HEATING = "heating"
STATE_OBSERVING = "observing"
STATE_CLOSED = "closed"

END_OBSERVED = "observation_complete"
END_NEXT_CYCLE = "next_cycle"
END_NO_HEAT = "no_heat"
END_RESTART = "restart"

FLAG_FORCED_STOP = "forced_stop"
FLAG_NO_HEAT = "no_heat"
FLAG_MODE_CHANGED = "mode_changed"
FLAG_TARGET_CHANGED = "target_changed"
FLAG_WINDOW = "window_open"
FLAG_LINKED_WINDOW = "linked_window_open"
FLAG_MANUAL_OFF = "manual_off"
FLAG_AUTO_OFF = "auto_off"
FLAG_HVAC_OFF = "hvac_off"
FLAG_READING_UNAVAILABLE = "reading_unavailable"
FLAG_SENSOR_GAP = "sensor_gap"
FLAG_REHEAT = "reheat"
FLAG_RESTART = "restart"
FLAG_MODEL_RESET = "model_reset"
FLAG_PEAK_UNCONFIRMED = "peak_unconfirmed"
FLAG_FAULT_PREFIX = "fault:"


def iso(t: float | None) -> str | None:
    """UTC ISO 8601 timestamp (matches the recorder's ``last_changed``)."""
    if t is None:
        return None
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _r(value: float | None, digits: int = 2) -> float | None:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _s(value: float | None) -> int | None:
    rounded = _r(value, 0)
    return None if rounded is None else int(rounded)


@dataclass
class _Open:
    """Live (in-memory) state of the record that is not closed yet."""

    record: dict[str, Any]
    burst: Burst
    requested_t: float
    target: float
    mode: str
    stop_t: float | None = None
    until: float | None = None
    peak_t: float | None = None  # predicted peak time at the stop
    last_report_t: float | None = None
    max_gap_s: float = 0.0
    max_t: float | None = None
    reports_since_max: int = 0
    flags: list[str] = field(default_factory=list)


class CycleLedger:
    """Bounded per-zone list of cycle records (oldest first)."""

    def __init__(
        self,
        stored: dict[str, Any] | None = None,
        *,
        now: float,
        max_cycles: int = MAX_CYCLES,
        max_age_s: float = MAX_AGE_S,
    ) -> None:
        self.max_cycles = max(1, int(max_cycles))
        self.max_age_s = max_age_s
        data = stored if isinstance(stored, dict) else {}
        records = data.get("cycles")
        self.cycles: list[dict[str, Any]] = [
            dict(r) for r in (records if isinstance(records, list) else []) if isinstance(r, dict)
        ]
        next_id = data.get("next_id")
        known = [r["id"] for r in self.cycles if isinstance(r.get("id"), int)]
        self.next_id = max([next_id if isinstance(next_id, int) else 1, *(i + 1 for i in known)])
        self._open: _Open | None = None
        self.dirty = False
        for record in self.cycles:
            if record.get("state") != STATE_CLOSED:
                # Interrupted by a restart: what was saved stays, the rest of
                # the observation is lost.
                flags = list(record.get("flags") or [])
                if FLAG_RESTART not in flags:
                    flags.append(FLAG_RESTART)
                record.update(
                    state=STATE_CLOSED,
                    end=END_RESTART,
                    closed_at=iso(now),
                    flags=flags[:MAX_FLAGS],
                    clean=False,
                )
                self.dirty = True
        self._prune(now)

    # ------------------------------------------------------------ export
    def to_dict(self) -> dict[str, Any]:
        return {"format": LEDGER_FORMAT, "next_id": self.next_id, "cycles": [dict(r) for r in self.cycles]}

    @property
    def open_record(self) -> dict[str, Any] | None:
        return self._open.record if self._open is not None else None

    def take_dirty(self) -> bool:
        """True once after a structural change (start, stop, close, first flag)."""
        dirty, self.dirty = self.dirty, False
        return dirty

    # ------------------------------------------------------------ inputs
    def note(self, t: float, flag: str) -> None:
        """Perturbation reported by the Home Assistant layer (e.g. manual off)."""
        if self._open is not None:
            self._flag(t, flag)

    def on_reading(self, t: float, value: float | None) -> None:
        """Every temperature report (also identical heartbeats); None = unavailable."""
        op = self._open
        if op is None:
            return
        if value is None:
            self._flag(t, FLAG_READING_UNAVAILABLE)
            return
        if op.stop_t is None or op.until is None or t > op.until:
            return
        if op.last_report_t is not None:
            op.max_gap_s = max(op.max_gap_s, t - op.last_report_t)
        op.last_report_t = t
        obs = op.record["observed"]
        obs["readings"] += 1
        obs["last"] = _r(value)
        if obs["max"] is None or value > obs["max"] + 1e-9:
            obs["max"] = _r(value)
            obs["max_after_stop_s"] = _s(t - op.stop_t)
            op.max_t = t
            op.reports_since_max = 0
        else:
            op.reports_since_max += 1
        obs["reports_after_max"] = op.reports_since_max
        if obs["min"] is None or value < obs["min"] - 1e-9:
            obs["min"] = _r(value)
        if op.record.get("first_perturbation") is None:
            obs["max_before_perturbation"] = obs["max"]
        obs["max_report_gap_s"] = _s(op.max_gap_s)

    def observe(self, t: float, core: ZoneCore, status: ZoneStatus) -> None:
        """Call after every :meth:`ZoneCore.update` (reads, never writes, the core)."""
        burst = core.burst
        op = self._open
        if op is not None and op.stop_t is None and burst is not op.burst:
            self._stopped(t, core, status)
        op = self._open
        if burst is not None and (op is None or op.burst is not burst):
            if op is not None:
                self._close(t, END_NEXT_CYCLE)
            self._start(t, core, status, burst)
        op = self._open
        if op is None:
            return
        rec = op.record
        if op.stop_t is None:
            rec["heat_started_at"] = iso(burst.heat_started_at) if burst is not None else None
            rec["stop"]["planned_s"] = _s(burst.planned_s) if burst is not None else None
        # Perturbations, from the request to the end of the observation.
        if abs(core.target - op.target) > 1e-6:
            self._flag(t, FLAG_TARGET_CHANGED)
        rec["target_min"] = _r(min(rec["target_min"], core.target))
        rec["target_max"] = _r(max(rec["target_max"], core.target))
        if status.window_open:
            self._flag(t, FLAG_WINDOW)
        if status.linked_window_open:
            self._flag(t, FLAG_LINKED_WINDOW)
        if status.fault:
            self._flag(t, FLAG_FAULT_PREFIX + status.fault)
        if not core.hvac_on and FLAG_MANUAL_OFF not in op.flags and FLAG_AUTO_OFF not in op.flags:
            self._flag(t, FLAG_HVAC_OFF)
        if op.stop_t is None and status.control_mode != op.mode:
            self._flag(t, FLAG_MODE_CHANGED)
        if op.until is not None and t >= op.until:
            self._close(op.until, END_OBSERVED)

    def flush(self, t: float, core: ZoneCore) -> None:
        """Bring the open record up to date before it is saved at shutdown."""
        op = self._open
        if op is None:
            return
        if op.stop_t is None:
            burst = op.burst
            op.record["heat_started_at"] = iso(burst.heat_started_at)
            op.record["stop"]["heat_s"] = _s(burst.heat_elapsed_s)
            op.record["stop"]["commanded_s"] = _s(t - op.requested_t)
        elif op.last_report_t is not None:
            op.record["observed"]["max_report_gap_s"] = _s(max(op.max_gap_s, t - op.last_report_t))
        self.dirty = True

    # ----------------------------------------------------------- internals
    def _start(self, t: float, core: ZoneCore, status: ZoneStatus, burst: Burst) -> None:
        reading = core.observer.displayed
        record: dict[str, Any] = {
            "id": self.next_id,
            "state": STATE_HEATING,
            "requested_at": iso(burst.requested_at),
            "heat_started_at": iso(burst.heat_started_at),
            "stopped_at": None,
            "closed_at": None,
            "end": None,
            "clean": False,
            "flags": [],
            "first_perturbation": None,
            "first_perturbation_after_request_s": None,
            "target_min": _r(core.target),
            "target_max": _r(core.target),
            "start": {
                "mode": burst.mode,
                "reason": status.reason,
                "target": _r(core.target),
                "reading": _r(reading),
                "planned_s": _s(burst.planned_s),
                "predicted_peak": _r(burst.predicted_peak, 3),
                "time_to_peak_s": _s(status.time_to_peak_s),
                "tail_s": _s(status.tail_s),
                "aim_peak": _r(status.aim_peak, 3),
                "heat_trigger_below": _r(status.heat_trigger_below, 3),
                "band_feasible": status.band_feasible,
                "confidence": _r(status.confidence, 3),
                "outdoor": _r(core.observer.outdoor.value, 1),
            },
            "stop": {
                "mode": None,
                "reason": None,
                "forced": None,
                "target": None,
                "reading": None,
                "planned_s": _s(burst.planned_s),
                "commanded_s": None,
                "heat_s": None,
                "predicted_peak": None,
                "time_to_peak_s": None,
                "tail_s": None,
                "model_peak_as_run": None,
                "confidence": None,
                "peak_correction": None,
                "trigger_correction": None,
                "outdoor": None,
                "slope_per_hour": None,
            },
            "observed": {
                "window_s": None,
                "readings": 0,
                "max": None,
                "max_after_stop_s": None,
                "reports_after_max": 0,
                "min": None,
                "last": None,
                "max_report_gap_s": None,
                "max_before_perturbation": None,
                "truncated_after_stop_s": None,
                "peak_error": None,
            },
        }
        self.next_id += 1
        self._open = _Open(
            record=record,
            burst=burst,
            requested_t=burst.requested_at,
            target=core.target,
            mode=burst.mode,
        )
        self.cycles.append(record)
        self.dirty = True
        self._prune(t)

    def _stopped(self, t: float, core: ZoneCore, status: ZoneStatus) -> None:
        op = self._open
        assert op is not None
        burst = op.burst
        rec = op.record
        stop = rec["stop"]
        reading = core.observer.displayed
        stop.update(
            mode=status.control_mode,
            reason=status.reason,
            forced=bool(core.last_forced_stop),
            target=_r(core.target),
            reading=_r(reading),
            planned_s=_s(burst.planned_s),
            commanded_s=_s(t - op.requested_t),
            heat_s=_s(burst.heat_elapsed_s),
            # The decision of the stopping step: the peak if heat stops now
            # (model mode; None for the fallback and for forced stops).
            predicted_peak=_r(status.predicted_peak, 3),
            time_to_peak_s=_s(status.time_to_peak_s),
            tail_s=_s(status.tail_s),
            confidence=_r(status.confidence, 3),
            peak_correction=_r(core.peak_correction(), 3),
            trigger_correction=_r(core.trigger_correction, 3),
            outdoor=_r(core.observer.outdoor.value, 1),
            slope_per_hour=_r(status.slope_instant, 3),
        )
        rec["stopped_at"] = iso(t)
        rec["heat_started_at"] = iso(burst.heat_started_at)
        if status.control_mode != op.mode:
            self._flag(t, FLAG_MODE_CHANGED)
        if core.last_forced_stop:
            self._flag(t, FLAG_FORCED_STOP)
        if burst.heat_started_at is None:
            self._flag(t, FLAG_NO_HEAT)
            op.stop_t = t
            self._close(t, END_NO_HEAT)
            return
        validation = core.validation_prediction(burst.heat_started_at)
        if validation is not None:
            stop["model_peak_as_run"] = _r(validation[0], 3)
        if status.time_to_peak_s:
            window = status.time_to_peak_s + OBSERVE_AFTER_PEAK_S
        elif validation is not None:
            window = validation[1] - t
        else:
            window = OBSERVE_DEFAULT_S
        window = min(OBSERVE_MAX_S, max(OBSERVE_MIN_S, window))
        op.stop_t = t
        op.until = t + window
        if status.time_to_peak_s:
            op.peak_t = t + status.time_to_peak_s
        elif validation is not None:
            # Fallback: the model's peak time for the burst as actually run.
            op.peak_t = validation[1] - VALIDATION_AFTER_PEAK_S
        op.last_report_t = t
        rec["state"] = STATE_OBSERVING
        obs = rec["observed"]
        obs["window_s"] = _s(window)
        if reading is not None:
            obs["max"] = obs["min"] = obs["last"] = _r(reading)
            obs["max_after_stop_s"] = 0
            op.max_t = t
            if rec.get("first_perturbation") is None:
                obs["max_before_perturbation"] = obs["max"]
        self.dirty = True

    def _flag(self, t: float, flag: str) -> None:
        op = self._open
        if op is None or flag in op.flags or len(op.flags) >= MAX_FLAGS:
            return
        op.flags.append(flag)
        rec = op.record
        rec["flags"] = list(op.flags)
        if rec.get("first_perturbation") is None and flag != FLAG_PEAK_UNCONFIRMED:
            rec["first_perturbation"] = flag
            rec["first_perturbation_after_request_s"] = _s(t - op.requested_t)
        self.dirty = True

    def _close(self, t: float, end: str) -> None:
        op = self._open
        assert op is not None
        rec = op.record
        obs = rec["observed"]
        if op.stop_t is not None and op.until is not None:
            end_t = min(t, op.until)
            if op.last_report_t is not None:
                gap = max(op.max_gap_s, end_t - op.last_report_t)
                obs["max_report_gap_s"] = _s(gap)
                if gap > REPORT_GAP_FLAG_S:
                    self._flag(t, FLAG_SENSOR_GAP)
            rising = op.reports_since_max == 0  # no report after the maximum
            if end == END_NEXT_CYCLE:
                obs["truncated_after_stop_s"] = _s(end_t - op.stop_t)
                if rising or (op.peak_t is not None and end_t < op.peak_t):
                    self._flag(t, FLAG_REHEAT)
            elif end == END_OBSERVED and rising:
                self._flag(t, FLAG_PEAK_UNCONFIRMED)
            predicted = rec["stop"]["predicted_peak"]
            if obs["max"] is not None and predicted is not None:
                obs["peak_error"] = _r(obs["max"] - predicted, 3)
        rec.update(
            state=STATE_CLOSED,
            end=end,
            closed_at=iso(t),
            flags=list(op.flags),
            clean=end in (END_OBSERVED, END_NEXT_CYCLE) and not op.flags,
        )
        self._open = None
        self.dirty = True
        self._prune(t)

    def _prune(self, now: float) -> None:
        open_rec = self.open_record
        kept = [
            r
            for r in self.cycles
            if r is open_rec
            or r.get("state") != STATE_CLOSED
            or (closed := _ts(r.get("closed_at"))) is None
            or now - closed <= self.max_age_s
        ]
        if len(kept) > self.max_cycles:
            kept = kept[-self.max_cycles :]
        if len(kept) != len(self.cycles):
            self.dirty = True
        self.cycles = kept
