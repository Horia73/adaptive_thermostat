"""Simulated rooms derived from learned zone models.

The integration learns a grey-box model per zone (``thermal_params``: q_s,
tau_f, tau_e, tau_r, tau_am, rho, base gain g and shared gain k).  For the
closed-loop simulator the same physics is expressed as a
:class:`simulator.RoomTruth` (radiator water side with a shared supply
temperature that ramps up while the boiler fires, room air, building mass,
pipe gains): richer than the controller's model, so the controller still sees
model mismatch.

``heat_scale`` multiplies the radiator power.  The learned ``q_s`` is an
average over real bursts, which start with a cold supply; the default is
chosen per room so that a 3-minute burst from a cold boiler gives the rise the
learned model predicts (:func:`energy_matched_scale`).  The report harness
can calibrate it further against the recorded burst rises.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (REPO / "tests" / "sim", REPO / "custom_components" / "adaptive_thermostat"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from simulator import Plant, RoomTruth, SensorTruth  # noqa: E402

from engine.model import ThermalParams, ZoneState, simulate_burst  # noqa: E402

C_AIR = 1.5e6  # J/K; only ratios matter for the temperatures
SUPPLY_EXCESS = 42.0  # K, hot supply minus room air at full fire


@dataclass(frozen=True)
class ZoneModel:
    """Learned model of one zone (units as in ``thermal_params``)."""

    q_s: float  # K/h
    tau_f_min: float
    tau_e_min: float
    tau_r_h: float
    tau_am_h: float
    rho: float
    base_gain: float = 0.2  # K/h
    shared_gain: float = 0.1  # K/h while the boiler fires for others
    valves: int = 1

    @property
    def params(self) -> ThermalParams:
        return ThermalParams(
            q_s=self.q_s,
            tau_f=self.tau_f_min / 60.0,
            tau_e=self.tau_e_min / 60.0,
            tau_r=self.tau_r_h,
            tau_am=self.tau_am_h,
            rho=self.rho,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], valves: int = 1) -> ZoneModel:
        return cls(
            q_s=float(data["q_s"]),
            tau_f_min=float(data["tau_f_min"]),
            tau_e_min=float(data["tau_e_min"]),
            tau_r_h=float(data["tau_r_h"]),
            tau_am_h=float(data.get("tau_am_h", 1.0)),
            rho=float(data.get("rho", 8.0)),
            base_gain=float(data.get("base_gain", 0.2)),
            shared_gain=float(data.get("shared_gain", 0.1)),
            valves=valves,
        )


def room_from_model(
    name: str,
    model: ZoneModel,
    *,
    heat_scale: float = 1.0,
    sun_share: float = 0.3,
    spike_rate_per_day: float = 0.3,
    spike_power_kh: float = 1.5,
    noise_kh: float = 0.0,
    noise_tau_h: float = 1.0,
    sensor_plume: float = 0.0,
) -> RoomTruth:
    """RoomTruth with the learned model's time constants and gains."""
    h_air_out = C_AIR / (model.tau_r_h * 3600.0)
    h_air_mass = C_AIR / (model.tau_am_h * 3600.0)
    h_emitter = model.q_s / 3600.0 * C_AIR / SUPPLY_EXCESS * heat_scale
    c_emitter = model.tau_e_min * 60.0 * h_emitter
    w_flow = c_emitter / (model.tau_f_min * 60.0)
    to_watt = C_AIR / 3600.0
    base = max(0.0, model.base_gain) * to_watt
    return RoomTruth(
        name=name,
        c_air=C_AIR,
        c_mass=model.rho * C_AIR,
        h_air_mass=h_air_mass,
        h_air_out=h_air_out,
        h_mass_out=0.0,
        c_emitter=c_emitter,
        w_flow=w_flow,
        h_emitter=h_emitter,
        pipe_gain=max(0.0, model.shared_gain) * to_watt * 40.0 / SUPPLY_EXCESS,
        base_gain=base * (1.0 - sun_share),
        sun_peak=base * sun_share * 2.5,
        spike_rate_per_day=spike_rate_per_day,
        spike_power=spike_power_kh * to_watt,
        spike_minutes=20.0,
        valves=model.valves,
        noise_power=noise_kh * to_watt,
        noise_tau_s=noise_tau_h * 3600.0,
        sensor_plume=sensor_plume,
    )


def truth_burst_rise(room: RoomTruth, burst_s: float, *, t_out: float = 8.0, temp: float = 23.0) -> float:
    """Peak rise of a burst from a cold boiler in the simulator physics (no gains)."""
    quiet = replace(room, base_gain=0.0, sun_peak=0.0, spike_rate_per_day=0.0, pipe_gain=0.0)
    plant = Plant(
        [(quiet, SensorTruth(dropout_rate_per_day=0.0))],
        start_temp=temp,
        outdoor_mean=t_out,
        outdoor_swing=0.0,
    )
    zone = plant.zones[0]
    zone.t_mass = temp
    plant.t_supply = 25.0
    ref = Plant(
        [(quiet, SensorTruth(dropout_rate_per_day=0.0))],
        start_temp=temp,
        outdoor_mean=t_out,
        outdoor_swing=0.0,
    )
    ref_zone = ref.zones[0]
    peak = 0.0
    steps = int(4 * 3600 / plant.dt)
    for i in range(steps):
        t = i * plant.dt
        firing = t < burst_s
        plant.boiler_on = firing
        if not firing and plant.pump_until < 0:
            plant.pump_until = plant.t + 120.0
        zone.valve_state = ["open" if (firing or plant.t < plant.pump_until) else "closed"] * room.valves
        plant._supply(plant.dt)
        plant._physics(zone, plant.dt)
        ref._physics(ref_zone, ref.dt)
        plant.t += plant.dt
        ref.t += ref.dt
        peak = max(peak, zone.t_air - ref_zone.t_air)
    return peak


def model_burst_rise(
    model: ZoneModel, burst_s: float, *, t_out: float = 8.0, temp: float = 23.0, cooling: float = 0.2
) -> float:
    """Lift of a burst as the controller sees it: predicted peak minus the dip before it.

    The room starts in equilibrium with its mass and cools at ``cooling`` K/h
    without heat; same start delay (25 s) and pump overrun as the engine.
    """
    from engine.zone import ZoneConfig, ZoneCore

    overrun = ZoneCore(ZoneConfig(), now=0.0).overrun()
    base = (temp - t_out) / model.tau_r_h - cooling
    state = ZoneState(temp=temp, tm=temp, q=0.0, d=0.0, base=base, t_out=t_out)
    pred = simulate_burst(
        state, model.params, delay_s=25.0, burst_s=burst_s, horizon_s=4 * 3600.0, overrun=overrun
    )
    return pred.peak - pred.minimum


def energy_matched_scale(model: ZoneModel, burst_s: float = 180.0) -> float:
    """Radiator scale so the simulated cold-start burst rise matches the model's."""
    target = model_burst_rise(model, burst_s)
    if target <= 1e-4:
        return 1.0
    lo, hi = 0.3, 4.0
    for _ in range(14):
        mid = 0.5 * (lo + hi)
        if truth_burst_rise(room_from_model("x", model, heat_scale=mid), burst_s) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
