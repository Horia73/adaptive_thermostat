"""Thermal controller logic for Adaptive Thermostat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple
import math


@dataclass
class Params:
    """Model parameters for the thermal controller."""

    tau_r: float  # [s] radiator time constant
    tau_th: float  # [s] room time constant
    K: float  # [°C] steady-state gain at 100% duty
    p: float = 1.0  # nonlinearity exponent for radiator -> heat flow


def _clip(value: float, low: float, high: float) -> float:
    """Clamp a value within bounds."""
    return max(low, min(high, value))


class ThermalController:
    """Grey-box radiator + room controller with peak-hitting and hold modes."""

    def __init__(
        self,
        target: float,
        *,
        deadband: float = 0.1,
        window_s: int = 600,
        min_on_s: int = 60,
        min_off_s: int = 120,
        ema_outdoor_halflife_s: int = 900,
        init_params: Optional[Params] = None,
        learn_rate: float = 0.25,
    ) -> None:
        self.target = target
        self.deadband = deadband
        self.window_s = window_s
        self.min_on_s = min_on_s
        self.min_off_s = min_off_s
        self.learn_rate = learn_rate

        self.params = init_params or Params(
            tau_r=12 * 60,
            tau_th=35 * 60,
            K=2.0,
            p=1.0,
        )

        self._ema_outdoor: Optional[float] = None
        self._ema_alpha = self._halflife_to_alpha(ema_outdoor_halflife_s)
        self._ref_outdoor: Optional[float] = None
        self._last_good_on: Optional[float] = None

    @staticmethod
    def _halflife_to_alpha(halflife_s: float, dt_s: float = 1.0) -> float:
        """Compute exponential moving average alpha."""
        if halflife_s <= 0:
            return 1.0
        return 1.0 - math.exp(-math.log(2) * dt_s / halflife_s)

    def update_outdoor(self, temp_out: float) -> None:
        """Feed a new outdoor temperature sample."""
        if self._ema_outdoor is None:
            self._ema_outdoor = float(temp_out)
            self._ref_outdoor = float(temp_out)
            return

        alpha = self._ema_alpha
        self._ema_outdoor = (1.0 - alpha) * self._ema_outdoor + alpha * float(temp_out)

    def get_outdoor(self, fallback: float = 0.0) -> float:
        """Return the filtered outdoor temperature."""
        return self._ema_outdoor if self._ema_outdoor is not None else fallback

    def set_params(self, params: Params) -> None:
        """Override controller parameters."""
        self.params = params

    def get_params(self) -> Params:
        """Return current controller parameters."""
        return self.params

    def _t_peak(self) -> float:
        """Return time from heat cut to peak of the residual tail."""
        params = self.params
        tau_r = max(1e-3, params.tau_r)
        tau_th = max(tau_r + 1e-3, params.tau_th)
        return (tau_r * tau_th / (tau_th - tau_r)) * math.log(tau_th / tau_r)

    def _delta_on(self, tau_on: float) -> float:
        """Return temperature rise during ON for tau_on seconds (scaled by K)."""
        params = self.params
        tau_r = params.tau_r
        tau_th = params.tau_th
        if tau_on <= 0:
            return 0.0

        denom = tau_th - tau_r
        if denom == 0:
            denom = 1e-6

        term = 1.0 - (tau_th * math.exp(-tau_on / tau_r) - tau_r * math.exp(-tau_on / tau_th)) / denom
        return params.K * term

    def _delta_tail_peak(self, tau_on: float) -> float:
        """Return additional rise after cut at the residual peak."""
        params = self.params
        tau_r, tau_th, p_exp = params.tau_r, params.tau_th, params.p

        E_cut = 1.0 - math.exp(-tau_on / max(1e-6, tau_r))
        t_peak = self._t_peak()
        denom = tau_th - tau_r
        if denom == 0:
            denom = 1e-6

        factor = params.K * (E_cut ** p_exp) * (tau_th / denom)
        shape = math.exp(-t_peak / tau_r) - math.exp(-t_peak / tau_th)
        return factor * shape

    def _predicted_peak(self, temp_start: float, tau_on: float) -> float:
        """Predict absolute temperature peak for a heating burst."""
        return temp_start + self._delta_on(tau_on) + self._delta_tail_peak(tau_on)

    def propose_on_time(self, temp_in: float, temp_target: Optional[float] = None) -> float:
        """Return ON time (seconds) so that residual peak hits the target."""
        target = self.target if temp_target is None else temp_target
        temp_out = self.get_outdoor()
        initial_temp = temp_in

        delta_target = max(0.0, target - temp_out)
        if self._last_good_on is not None and self._ref_outdoor is not None:
            delta_ref = max(0.0, target - self._ref_outdoor)
            scale = (delta_target / delta_ref) if delta_ref > 1e-6 else 1.0
            tau_low = 0.0
            tau_high = _clip(
                self._last_good_on * max(0.25, min(4.0, scale * 1.5)),
                60.0,
                45 * 60.0,
            )
        else:
            tau_low = 0.0
            tau_high = _clip(8 * 60.0 * (delta_target / max(0.5, 1.0)), 120.0, 45 * 60.0)

        def solve_function(tau: float) -> float:
            return self._predicted_peak(initial_temp, tau) - target

        f_low = solve_function(tau_low)
        f_high = solve_function(tau_high)
        attempts = 0

        while f_high < 0.0 and tau_high < 45 * 60.0 and attempts < 8:
            tau_high *= 1.6
            f_high = solve_function(tau_high)
            attempts += 1

        if f_low > 0:
            return 0.0

        for _ in range(40):
            mid = 0.5 * (tau_low + tau_high)
            f_mid = solve_function(mid)
            if abs(f_mid) < 1e-3:
                tau_high = mid
                break
            if f_mid > 0:
                tau_high = mid
            else:
                tau_low = mid

        tau_on = _clip(tau_high, 0.0, 45 * 60.0)
        self._last_good_on = tau_on
        if self._ema_outdoor is not None:
            self._ref_outdoor = float(self._ema_outdoor)
        return float(tau_on)

    def hold_pwm(self, temp_in: float, temp_target: Optional[float] = None) -> Tuple[int, int]:
        """Return (t_on, t_off) split for hold-mode PWM."""
        params = self.params
        target = self.target if temp_target is None else temp_target
        temp_out = self.get_outdoor()

        if temp_in >= target + self.deadband:
            return (0, max(self.min_off_s, self.window_s))

        if temp_in <= target - self.deadband:
            duty = _clip((target - temp_out) / max(1e-6, params.K), 0.0, 1.0)
            t_on = int(_clip(duty * self.window_s, self.min_on_s, self.window_s - self.min_off_s))

            def constraint(tau: float) -> float:
                return self._predicted_peak(temp_in, tau) - (target + self.deadband)

            if constraint(t_on) > 0:
                low, high = 0.0, t_on
                for _ in range(30):
                    mid = 0.5 * (low + high)
                    if constraint(mid) > 0:
                        high = mid
                    else:
                        low = mid
                t_on = int(_clip(high, 0, t_on))

            return (t_on, self.window_s - t_on)

        return (0, self.window_s)

    def cold_start_calibrate(
        self,
        temp_start: float,
        temp_cut: float,
        temp_peak: float,
        tau_on_s: float,
        t_peak_after_cut_s: float,
        *,
        outdoor_samples: Optional[List[Tuple[float, float]]] = None,
        off_decay: Optional[List[Tuple[float, float, float]]] = None,
        clamp: bool = True,
    ) -> Params:
        """Update parameters based on an overshoot cycle."""

        old = self.params
        tau_th_est: Optional[float] = None

        if off_decay and len(off_decay) >= 3:
            xs = []
            ys = []
            for dt_value, temp_in, temp_out in off_decay:
                theta = max(1e-6, temp_in - temp_out)
                xs.append(float(dt_value))
                ys.append(math.log(theta))

            n = float(len(xs))
            sx = sum(xs)
            sy = sum(ys)
            sxx = sum(x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            denom = n * sxx - sx * sx
            if abs(denom) > 1e-9:
                slope = (n * sxy - sx * sy) / denom
                if slope < 0:
                    tau_th_est = -1.0 / max(-1e-6, slope)

        tau_th = float(tau_th_est) if tau_th_est and tau_th_est > 60.0 else old.tau_th

        def solve_ratio(x_value: float) -> float:
            if x_value <= 0 or x_value >= 1:
                return 1e9
            return tau_th * (x_value / (1 - x_value)) * math.log(1.0 / x_value) - t_peak_after_cut_s

        low, high = 1e-3, 0.99
        for _ in range(60):
            mid = 0.5 * (low + high)
            if solve_ratio(mid) > 0:
                high = mid
            else:
                low = mid
        tau_r = _clip(high * tau_th, 10.0, tau_th - 1.0)

        def g_on(tau_value: float) -> float:
            tau_r_local, tau_th_local = tau_r, tau_th
            denom_local = tau_th_local - tau_r_local
            if abs(denom_local) < 1e-9:
                denom_local = 1e-6
            return 1.0 - (
                tau_th_local * math.exp(-tau_value / tau_r_local)
                - tau_r_local * math.exp(-tau_value / tau_th_local)
            ) / denom_local

        def g_tail_peak(tau_value: float) -> float:
            tau_r_local, tau_th_local, p_local = tau_r, tau_th, old.p
            E_cut_local = 1.0 - math.exp(-tau_value / max(1e-6, tau_r_local))
            tpk_local = (tau_r_local * tau_th_local / (tau_th_local - tau_r_local)) * math.log(
                tau_th_local / tau_r_local
            )
            denom_local = tau_th_local - tau_r_local
            if abs(denom_local) < 1e-9:
                denom_local = 1e-6
            factor = (E_cut_local ** p_local) * (tau_th_local / denom_local)
            shape = math.exp(-tpk_local / tau_r_local) - math.exp(-tpk_local / tau_th_local)
            return factor * shape

        denom_gain = g_on(tau_on_s) + g_tail_peak(tau_on_s)
        if denom_gain < 1e-6:
            K_est = old.K
        else:
            K_est = _clip((temp_peak - temp_start) / denom_gain, 0.1, 15.0)

        if outdoor_samples:
            avg_out = sum(value for _, value in outdoor_samples) / max(1, len(outdoor_samples))
            self._ref_outdoor = avg_out

        lr = _clip(self.learn_rate, 0.0, 1.0)
        new_params = Params(
            tau_r=(1 - lr) * old.tau_r + lr * tau_r,
            tau_th=(1 - lr) * old.tau_th + lr * tau_th,
            K=(1 - lr) * old.K + lr * K_est,
            p=old.p,
        )

        if clamp:
            new_params.tau_r = _clip(new_params.tau_r, 60.0, new_params.tau_th - 1.0)
            new_params.tau_th = _clip(new_params.tau_th, new_params.tau_r + 1.0, 6 * 3600.0)
            new_params.K = _clip(new_params.K, 0.2, 20.0)

        self.params = new_params
        return new_params
