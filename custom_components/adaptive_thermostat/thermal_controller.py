"""Thermal controller logic for Adaptive Thermostat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
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
        self._apply_adaptive_timings()

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
        self._apply_adaptive_timings()

    def get_params(self) -> Params:
        """Return current controller parameters."""
        return self.params

    def residual_peak_delay(self) -> float:
        """Return time between heater off and predicted residual peak."""
        return float(max(0.0, self._t_peak()))

    def predict_peak(self, temp_start: float, tau_on: float) -> float:
        """Predict the peak temperature for a heating burst."""
        return float(self._predicted_peak(temp_start, tau_on))

    def get_runtime_state(self) -> Dict[str, Optional[float] | Dict[str, float]]:
        """Return a JSON-serializable snapshot."""
        return {
            "params": {
                "tau_r": float(self.params.tau_r),
                "tau_th": float(self.params.tau_th),
                "K": float(self.params.K),
                "p": float(self.params.p),
            },
            "ema_outdoor": None if self._ema_outdoor is None else float(self._ema_outdoor),
            "ref_outdoor": None if self._ref_outdoor is None else float(self._ref_outdoor),
            "last_good_on": None if self._last_good_on is None else float(self._last_good_on),
        }

    def restore_runtime_state(self, payload: Optional[Dict[str, object]]) -> None:
        """Restore controller state from persistence."""
        if not payload:
            return

        params_payload = payload.get("params")
        if isinstance(params_payload, dict):
            tau_r = float(params_payload.get("tau_r", self.params.tau_r))
            tau_th = float(params_payload.get("tau_th", self.params.tau_th))
            K = float(params_payload.get("K", self.params.K))
            p = float(params_payload.get("p", self.params.p))
            restored = Params(tau_r=tau_r, tau_th=tau_th, K=K, p=p)
            self.set_params(restored)

        ema_outdoor = payload.get("ema_outdoor")
        self._ema_outdoor = float(ema_outdoor) if isinstance(ema_outdoor, (int, float)) else None

        ref_outdoor = payload.get("ref_outdoor")
        self._ref_outdoor = float(ref_outdoor) if isinstance(ref_outdoor, (int, float)) else None

        last_good_on = payload.get("last_good_on")
        self._last_good_on = float(last_good_on) if isinstance(last_good_on, (int, float)) else None

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
        self._apply_adaptive_timings()
        return new_params

    def register_cycle_result(
        self,
        temp_start: float,
        temp_peak: float,
        tau_on_s: float,
        *,
        temp_target: Optional[float] = None,
        temp_cut: Optional[float] = None,
        tail_peak_delay_s: Optional[float] = None,
    ) -> Optional[Dict[str, float]]:
        """Update model after observing a full heating cycle."""
        target = self.target if temp_target is None else temp_target
        tau_on = max(0.0, float(tau_on_s))
        if tau_on <= 0:
            return None

        predicted_peak = self._predicted_peak(temp_start, tau_on)
        predicted_delta = max(0.0, predicted_peak - temp_start)
        predicted_overshoot = max(0.0, predicted_peak - target)
        actual_delta = max(0.0, temp_peak - temp_start)
        if predicted_delta <= 1e-6:
            return None

        ratio = _clip(actual_delta / predicted_delta, 0.25, 3.5)
        params = self.params
        lr = _clip(self.learn_rate, 0.0, 1.0)
        desired_K = _clip(params.K * ratio, 0.2, 20.0)
        actual_overshoot = max(0.0, temp_peak - target)
        predicted_tail_delta = max(0.0, self._delta_tail_peak(tau_on))
        predicted_tail_delay = max(1e-3, self._t_peak())
        actual_on_delta: Optional[float] = None
        if isinstance(temp_cut, (int, float)):
            actual_on_delta = max(0.0, float(temp_cut) - temp_start)

        tau_lr = min(0.25, 0.5 * lr)
        new_tau_r = params.tau_r
        new_tau_th = params.tau_th

        if tail_peak_delay_s is not None and tail_peak_delay_s > 0.0:
            delay_ratio = _clip(float(tail_peak_delay_s) / predicted_tail_delay, 0.25, 3.0)
            tau_th_target = _clip(params.tau_th * delay_ratio, params.tau_r + 2.0, 6 * 3600.0)
            tau_r_target = _clip(params.tau_r * delay_ratio, 60.0, tau_th_target - 1.0)
            new_tau_r = (1.0 - tau_lr) * new_tau_r + tau_lr * tau_r_target
            new_tau_th = (1.0 - tau_lr) * new_tau_th + tau_lr * tau_th_target

        if predicted_tail_delta > 1e-6 and actual_overshoot > 0.0:
            overshoot_ratio = _clip(actual_overshoot / max(predicted_tail_delta, 1e-6), 0.25, 3.0)
            blend = min(0.2, 0.4 * lr + 0.05)
            tau_th_target = _clip(new_tau_th * overshoot_ratio, new_tau_r + 2.0, 6 * 3600.0)
            new_tau_th = (1.0 - blend) * new_tau_th + blend * tau_th_target
            new_tau_r = min(new_tau_r, new_tau_th - 1.0)

        new_params = Params(
            tau_r=new_tau_r,
            tau_th=new_tau_th,
            K=(1.0 - lr) * params.K + lr * desired_K,
            p=params.p,
        )
        changed = (
            abs(new_params.K - params.K) > 1e-6
            or abs(new_params.tau_r - params.tau_r) > 1e-3
            or abs(new_params.tau_th - params.tau_th) > 1e-3
        )
        if changed:
            self.set_params(new_params)

        if ratio > 1.05:
            shrink = _clip(1.0 / ratio, 0.3, 1.0)
            self._last_good_on = max(float(self.min_on_s), tau_on * shrink)
        elif ratio < 0.95:
            boost = _clip(1.0 / max(ratio, 1e-3), 1.0, 3.0)
            self._last_good_on = _clip(tau_on * boost, float(self.min_on_s), 45 * 60.0)
        else:
            self._last_good_on = tau_on

        undershoot = max(0.0, target - temp_peak)
        diagnostics = {
            "predicted_peak": predicted_peak,
            "actual_peak": temp_peak,
            "start_temp": temp_start,
            "target_temp": target,
            "tau_on": tau_on,
            "ratio_actual_to_pred": ratio,
            "overshoot": actual_overshoot,
            "undershoot": undershoot,
            "predicted_overshoot": predicted_overshoot,
            "predicted_tail_delta": predicted_tail_delta,
            "predicted_tail_delay": predicted_tail_delay,
            "observed_tail_delay": tail_peak_delay_s,
        }
        if actual_on_delta is not None:
            diagnostics["actual_on_delta"] = actual_on_delta
        return diagnostics

    def _apply_adaptive_timings(self) -> None:
        """Derive min on/off times and PWM window from the current parameters."""
        params = self.params
        min_on = _clip(params.tau_r * 0.25, 60.0, params.tau_r * 0.85)
        min_off = _clip(params.tau_th * 0.2, min_on, params.tau_th)
        window = _clip(params.tau_th * 1.1, 480.0, 5400.0)
        window = max(window, min_on + min_off + 30.0)

        self.min_on_s = int(min_on)
        self.min_off_s = int(min_off)
        self.window_s = int(window)
