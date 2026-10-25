"""Realistic multi-zone hydronic heating simulator for closed-loop tests.

The *truth* physics is deliberately richer than the controller's model, so
the tests exercise robustness to model mismatch:

* radiator water side: ``C_e dT_e/dt = flow W (T_s - T_e) - H_ea (T_e - T_a)``
  with a shared supply temperature ``T_s`` that warms while the boiler fires
  and cools during pump overrun (asymmetric fill/discharge);
* room air + building mass, the mass also leaks to outdoors;
* heat from the boiler's distribution pipes while the pump runs;
* passive gains: sun (midday bell), random cooking/shower spikes, people;
* daily outdoor temperature cycle with noise;
* sensor: first-order lag, noise, 0.1 °C rounding, threshold reports (>= one
  or two steps) with a minimum interval, 30-minute heartbeats that only show
  up when the value changed, random drop-outs;
* valves: 18 s travel (opening/closing states), random unavailable blips.

The control side is the real engine: one :class:`ZoneCore` per zone, one
:class:`BoilerPolicy` per boiler, and a small reconciler equivalent to the
Home Assistant one (desired valve = want heat or overrun hold).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import random
import sys

ENGINE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "adaptive_thermostat"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from engine.boiler import BoilerConfig, BoilerPolicy, ZoneReport  # noqa: E402
from engine.zone import ZoneConfig, ZoneCore, ZoneStatus  # noqa: E402


@dataclass
class RoomTruth:
    """True physical parameters of a room (SI-ish units: J/K, W/K, W)."""

    name: str
    c_air: float = 1.5e6  # J/K, air + light furniture
    c_mass: float = 12e6  # J/K, walls/floor reachable within hours
    h_air_mass: float = 400.0  # W/K
    h_air_out: float = 25.0  # W/K
    h_mass_out: float = 15.0  # W/K
    c_emitter: float = 60e3  # J/K
    w_flow: float = 300.0  # W/K, water-side conductance when the valve is open
    h_emitter: float = 35.0  # W/K, radiator to air
    pipe_gain: float = 60.0  # W while the pump runs (distribution pipes)
    base_gain: float = 150.0  # W, neighbours and appliances
    sun_peak: float = 250.0  # W at noon
    spike_rate_per_day: float = 0.5
    spike_power: float = 1500.0  # W
    spike_minutes: float = 30.0
    window_rate_per_day: float = 0.0
    window_h: float = 120.0  # W/K extra loss while a window is open
    window_minutes: float = 10.0
    valves: int = 1


@dataclass
class SensorTruth:
    """Sensor behaviour."""

    lag_s: float = 180.0
    noise: float = 0.015
    threshold_steps: int = 2  # immediate report when the rounded value moved this many steps
    heartbeat_s: float = 1800.0
    min_interval_s: float = 30.0
    dropout_rate_per_day: float = 1.0
    dropout_minutes: float = 8.0


@dataclass
class ZoneSim:
    """One simulated zone: truth state, sensor, actuators and its ZoneCore."""

    truth: RoomTruth
    sensor: SensorTruth
    core: ZoneCore
    t_air: float
    t_mass: float
    t_emit: float
    t_sensor: float
    reported: float | None = None
    last_report_t: float = -1e9
    last_heartbeat_t: float = 0.0
    valve_state: list[str] = field(default_factory=list)
    valve_move_until: list[float] = field(default_factory=list)
    valve_unavailable_until: list[float] = field(default_factory=list)
    dropout_until: float = -1.0
    spike_until: float = -1.0
    window_until: float = -1.0
    status: ZoneStatus | None = None
    log: list[tuple[float, float, float, bool, float]] = field(default_factory=list)
    bursts: list[tuple[float, float]] = field(default_factory=list)
    heat_on_since: float | None = None
    heat_seconds: float = 0.0
    passive_events: list[tuple[float, float, str]] = field(default_factory=list)
    air_trace: list[tuple[float, float]] = field(default_factory=list)


class Plant:
    """Shared boiler + zones."""

    def __init__(
        self,
        rooms: list[tuple[RoomTruth, SensorTruth]],
        *,
        target: float = 22.0,
        start_temp: float = 20.0,
        outdoor_mean: float = 6.0,
        outdoor_swing: float = 4.0,
        seed: int = 1,
        zone_config: ZoneConfig | None = None,
        boiler_config: BoilerConfig | None = None,
        supply_set: float = 65.0,
        turn_on_delay_s: float = 10.0,
        turn_off_delay_s: float = 120.0,
        stored: dict[str, dict] | None = None,
        t0: float = 1_790_000_000.0,
    ) -> None:
        self.rng = random.Random(seed)
        self.t = t0
        self.t0 = t0
        self.dt = 5.0
        self.outdoor_mean = outdoor_mean
        self.outdoor_swing = outdoor_swing
        self.supply_set = supply_set
        self.t_supply = start_temp
        self.boiler_on = False
        self.pump_until = -1.0
        self.boiler_cmd_t = -1.0
        self.boiler_switches: list[tuple[float, bool]] = []
        self.turn_on_delay_s = turn_on_delay_s
        self.turn_off_delay_s = turn_off_delay_s
        self.policy = BoilerPolicy(config=boiler_config or BoilerConfig())
        self.policy.sync_actual(False, t0 - 3600.0)
        self.zones: list[ZoneSim] = []
        for room, sensor in rooms:
            cfg = zone_config or ZoneConfig()
            core = ZoneCore(cfg, stored=(stored or {}).get(room.name), now=t0)
            core.set_hvac(t0, True)
            core.set_target(target)
            zone = ZoneSim(
                truth=room,
                sensor=sensor,
                core=core,
                t_air=start_temp,
                t_mass=start_temp,
                t_emit=start_temp,
                t_sensor=start_temp,
                valve_state=["closed"] * room.valves,
                valve_move_until=[0.0] * room.valves,
                valve_unavailable_until=[-1.0] * room.valves,
            )
            self.zones.append(zone)
        self._next_control = t0
        self._outdoor_reported: float | None = None

    # ------------------------------------------------------------ truth
    def outdoor(self, t: float) -> float:
        hours = (t - self.t0) / 3600.0
        return self.outdoor_mean + self.outdoor_swing * math.sin(2 * math.pi * (hours - 9.0) / 24.0)

    def _sun(self, t: float) -> float:
        hour = ((t - self.t0) / 3600.0) % 24.0
        return max(0.0, math.cos((hour - 13.0) / 4.0 * math.pi / 2)) if 9 <= hour <= 17 else 0.0

    def _physics(self, z: ZoneSim, dt: float) -> None:
        r = z.truth
        t_out = self.outdoor(self.t)
        pump = self.boiler_on or self.t < self.pump_until
        open_frac = sum(1.0 for s in z.valve_state if s in ("open", "closing")) / len(z.valve_state)
        flow = r.w_flow * open_frac if pump else 0.0
        gains = r.base_gain + r.sun_peak * self._sun(self.t)
        if self.t < z.spike_until:
            gains += r.spike_power
        if pump:
            gains += r.pipe_gain * max(0.0, (self.t_supply - z.t_air) / 40.0)
        h_out = r.h_air_out + (r.window_h if self.t < z.window_until else 0.0)
        q_emit = r.h_emitter * (z.t_emit - z.t_air)
        d_emit = (flow * (self.t_supply - z.t_emit) - q_emit) / r.c_emitter
        d_air = (q_emit - r.h_air_mass * (z.t_air - z.t_mass) - h_out * (z.t_air - t_out) + gains) / r.c_air
        d_mass = (r.h_air_mass * (z.t_air - z.t_mass) - r.h_mass_out * (z.t_mass - t_out)) / r.c_mass
        z.t_emit += d_emit * dt
        z.t_air += d_air * dt
        z.t_mass += d_mass * dt
        z.t_sensor += (z.t_air - z.t_sensor) * (1.0 - math.exp(-dt / z.sensor.lag_s))
        # Random events.
        day = 86400.0
        if self.rng.random() < r.spike_rate_per_day * dt / day:
            z.spike_until = self.t + r.spike_minutes * 60.0
            z.passive_events.append((self.t, z.spike_until, "spike"))
        if self.rng.random() < r.window_rate_per_day * dt / day:
            z.window_until = self.t + r.window_minutes * 60.0
            z.passive_events.append((self.t, z.window_until, "window"))
        if self.rng.random() < z.sensor.dropout_rate_per_day * dt / day:
            z.dropout_until = self.t + z.sensor.dropout_minutes * 60.0

    def _supply(self, dt: float) -> None:
        if self.boiler_on:
            self.t_supply += (self.supply_set - self.t_supply) * (1 - math.exp(-dt / 90.0))
        else:
            self.t_supply += (25.0 - self.t_supply) * (1 - math.exp(-dt / 150.0))

    # ----------------------------------------------------------- sensor
    def _sensor(self, z: ZoneSim) -> None:
        s = z.sensor
        if self.t < z.dropout_until:
            if z.reported is not None:
                z.reported = None
                z.core.on_temperature(self.t, None)
            return
        value = round(z.t_sensor + self.rng.gauss(0.0, s.noise), 1)
        if z.reported is None:
            z.reported = value
            z.last_report_t = self.t
            z.core.on_temperature(self.t, value)
            return
        steps = round(abs(value - z.reported) * 10)
        due_heartbeat = self.t - z.last_heartbeat_t >= s.heartbeat_s
        if (steps >= s.threshold_steps and self.t - z.last_report_t >= s.min_interval_s) or (
            due_heartbeat and steps >= 1
        ):
            z.reported = value
            z.last_report_t = self.t
            z.last_heartbeat_t = self.t
            z.core.on_temperature(self.t, value)
        elif due_heartbeat:
            z.last_heartbeat_t = self.t
            # Unchanged heartbeat: Home Assistant only updates last_reported.
            z.core.on_temperature(self.t, value, reported_t=self.t)

    # --------------------------------------------------------- actuators
    def _valves(self, z: ZoneSim, desired_open: bool) -> None:
        for i, state in enumerate(z.valve_state):
            if self.t < z.valve_unavailable_until[i]:
                continue
            if state == "opening" and self.t >= z.valve_move_until[i]:
                z.valve_state[i] = "open"
            elif state == "closing" and self.t >= z.valve_move_until[i]:
                z.valve_state[i] = "closed"
            state = z.valve_state[i]
            if desired_open and state in ("closed", "closing"):
                z.valve_state[i] = "opening"
                z.valve_move_until[i] = self.t + 18.0
            elif not desired_open and state in ("open", "opening"):
                z.valve_state[i] = "closing"
                z.valve_move_until[i] = self.t + 18.0

    def _control(self) -> None:
        t = self.t
        decisions: dict[str, ZoneStatus] = {}
        for z in self.zones:
            z.core.on_outdoor(t, round(self.outdoor(t), 1))
            frac = sum(1.0 for s in z.valve_state if s in ("open", "closing")) / len(z.valve_state)
            z.core.on_actuators(t, frac, self.boiler_on)
            start_delay = 20.0 if self.boiler_on else 20.0 + self.turn_on_delay_s
            if not self.boiler_on and self.policy.off_since is not None:
                start_delay = max(start_delay, self.policy.off_since + self.policy.config.min_off_s - t)
            z.core.set_start_conditions(start_delay, 0.3)
            status = z.core.update(t)
            z.status = status
            decisions[z.truth.name] = status
            all_open = all(s == "open" for s in z.valve_state)
            all_closed = all(s == "closed" for s in z.valve_state)
            self.policy.update_zone(
                z.truth.name,
                ZoneReport(
                    demand=status.want_heat,
                    forced_stop=status.forced_stop,
                    valves_open=all_open,
                    valves_closed=all_closed,
                    turn_on_delay_s=self.turn_on_delay_s,
                    turn_off_delay_s=self.turn_off_delay_s,
                ),
            )
        decision = self.policy.evaluate(t)
        wakes = [z.status.next_wake for z in self.zones if z.status is not None]
        if decision.next_wake is not None:
            wakes.append(decision.next_wake)
        self._next_control = min([t + 60.0, *wakes])
        if decision.on != self.boiler_on:
            self.boiler_on = decision.on
            self.boiler_switches.append((t, decision.on))
            if not decision.on:
                self.pump_until = t + 180.0
        for z in self.zones:
            hold = self.policy.hold_until(z.truth.name, t)
            self._valves(z, decisions[z.truth.name].want_heat or hold is not None)
            flowing = self.boiler_on and any(s in ("open", "closing") for s in z.valve_state)
            if flowing and z.heat_on_since is None:
                z.heat_on_since = t
            elif not flowing and z.heat_on_since is not None:
                z.bursts.append((z.heat_on_since, t))
                z.heat_on_since = None

    def run(self, hours: float, *, log_every_s: float = 300.0) -> None:
        end = self.t + hours * 3600.0
        next_log = self.t
        while self.t < end:
            self._supply(self.dt)
            for z in self.zones:
                self._physics(z, self.dt)
                self._sensor(z)
            if self.t >= self._next_control - 1e-6:
                self._control()
            if int(self.t) % 60 == 0:
                for z in self.zones:
                    z.air_trace.append((self.t, z.t_air))
            if self.t >= next_log:
                for z in self.zones:
                    z.log.append(
                        (
                            self.t,
                            z.t_air,
                            z.core.target,
                            bool(z.status and z.status.want_heat),
                            z.status.confidence if z.status else 0.0,
                        )
                    )
                next_log = self.t + log_every_s
            self.t += self.dt


def comfort_metrics(zone: ZoneSim, start: float, end: float | None = None) -> dict[str, float]:
    """Comfort and cycling statistics of a zone between ``start`` and ``end``."""
    rows = [r for r in zone.log if r[0] >= start and (end is None or r[0] <= end)]
    errs = [r[1] - r[2] for r in rows]
    n = max(1, len(errs))
    bursts = [b for b in zone.bursts if b[0] >= start and (end is None or b[0] <= end)]
    hours = (rows[-1][0] - rows[0][0]) / 3600.0 if len(rows) > 1 else 1.0
    lengths = sorted(b[1] - b[0] for b in bursts)
    errs_sorted = sorted(errs)
    return {
        "mean_error": sum(errs) / n,
        "mae": sum(abs(e) for e in errs) / n,
        "within_0_2": sum(1 for e in errs if abs(e) <= 0.2) / n,
        "within_0_3": sum(1 for e in errs if abs(e) <= 0.3) / n,
        "p95_over": errs_sorted[int(0.95 * (n - 1))] if errs else 0.0,
        "p05_under": errs_sorted[int(0.05 * (n - 1))] if errs else 0.0,
        "max_over": max(errs) if errs else 0.0,
        "bursts_per_hour": len(bursts) / max(hours, 1e-6),
        "median_burst_s": lengths[len(lengths) // 2] if lengths else 0.0,
        "min_burst_s": lengths[0] if lengths else 0.0,
        "heat_fraction": sum(lengths) / max(1.0, hours * 3600.0),
    }


def boiler_metrics(plant: Plant, start: float) -> dict[str, float]:
    """Boiler cycling statistics."""
    sw = [s for s in plant.boiler_switches if s[0] >= start]
    on_runs: list[float] = []
    off_runs: list[float] = []
    for (t1, on1), (t2, _on2) in zip(sw, sw[1:]):
        (on_runs if on1 else off_runs).append(t2 - t1)
    hours = (plant.t - start) / 3600.0
    return {
        "starts_per_hour": sum(1 for s in sw if s[1]) / max(hours, 1e-6),
        "min_on_s": min(on_runs) if on_runs else 0.0,
        "min_off_s": min(off_runs) if off_runs else 0.0,
    }


# Room archetypes used by the tests (calibrated so that full radiator output
# warms the air by roughly 2-6 K/h, as learned from the recorded zones).
def fast_room(name: str = "fast") -> RoomTruth:
    """Small room, big radiator, light structure (bathroom/dressing)."""
    return RoomTruth(
        name=name,
        c_air=0.8e6,
        c_mass=4e6,
        h_air_mass=250.0,
        h_air_out=15.0,
        h_mass_out=8.0,
        c_emitter=35e3,
        w_flow=250.0,
        h_emitter=30.0,
        base_gain=80.0,
        sun_peak=80.0,
    )


def slow_room(name: str = "slow") -> RoomTruth:
    """Large heavy room, small radiator relative to its size (living room)."""
    return RoomTruth(
        name=name,
        c_air=2.5e6,
        c_mass=30e6,
        h_air_mass=700.0,
        h_air_out=30.0,
        h_mass_out=20.0,
        c_emitter=90e3,
        w_flow=350.0,
        h_emitter=45.0,
        base_gain=200.0,
        sun_peak=300.0,
    )


def leaky_room(name: str = "leaky") -> RoomTruth:
    """Corner room with poor windows: high losses, needs long bursts."""
    return RoomTruth(
        name=name,
        c_air=1.5e6,
        c_mass=10e6,
        h_air_mass=400.0,
        h_air_out=45.0,
        h_mass_out=25.0,
        c_emitter=70e3,
        w_flow=350.0,
        h_emitter=45.0,
        base_gain=60.0,
        sun_peak=150.0,
    )


def typical_room(name: str = "typical", valves: int = 1) -> RoomTruth:
    """Bedroom-like room similar to the recorded zones."""
    return RoomTruth(name=name, valves=valves)


def burst_overshoot(zone: ZoneSim, start: float, *, after_s: float = 3600.0) -> dict[str, float]:
    """Heating-caused overshoot: peak above target after each burst.

    Bursts overlapping a passive event (spike/window) or followed by another
    burst within ``after_s`` are excluded; the peak is taken over the burst
    and the following ``after_s`` (when the tail must have peaked).
    """
    excess: list[float] = []
    bursts = [b for b in zone.bursts if b[0] >= start]
    for i, (b0, b1) in enumerate(bursts):
        end = b1 + after_s
        if i + 1 < len(bursts) and bursts[i + 1][0] < end:
            end = bursts[i + 1][0]
        if any(e0 < end and e1 > b0 - 1800 for e0, e1, _ in zone.passive_events):
            continue
        peak = max((v for t, v in zone.air_trace if b0 <= t <= end), default=None)
        if peak is not None:
            excess.append(peak - zone.core.target)
    excess.sort()
    n = len(excess)
    return {
        "bursts": float(n),
        "median_excess": excess[n // 2] if n else 0.0,
        "p90_excess": excess[int(0.9 * (n - 1))] if n else 0.0,
        "max_excess": excess[-1] if n else 0.0,
    }


def warmup_lower_bound(room: RoomTruth, start_temp: float, level: float, *, seed: int = 4) -> float | None:
    """Seconds the room needs to reach ``level`` with the radiator on continuously."""
    plant = Plant([(room, SensorTruth())], target=level, start_temp=start_temp, seed=seed)
    zone = plant.zones[0]
    plant.boiler_on = True
    zone.valve_state = ["open"] * len(zone.valve_state)
    for _ in range(int(48 * 3600 / plant.dt)):
        plant._supply(plant.dt)
        plant._physics(zone, plant.dt)
        plant.t += plant.dt
        if zone.t_air >= level:
            return plant.t - plant.t0
    return None
