"""Proportion estimates and comparisons.

Pickup, engagement, and business outcome are never combined into one score.
A difference is reportable only when both sides meet the configured sample
floor, the p-value clears the (possibly Bonferroni-adjusted) alpha, and the
absolute gap clears ``min_abs_change``. Otherwise there is no winner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def normsinv(p: float) -> float:
    """Inverse standard-normal CDF. Peter Acklam's rational approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411063e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def normal_sf(z: float) -> float:
    """Upper tail P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def bonferroni(alpha: float, comparisons: int) -> float:
    if comparisons < 1:
        raise ValueError("comparisons must be >= 1")
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    return alpha / comparisons


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError("successes must be between 0 and n")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    z = normsinv(0.5 + confidence / 2)
    phat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def required_n_per_arm(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Equal sample size per arm for a two-sided two-proportion z-test.

    Returns None when the rates are equal: no finite sample separates them.
    """
    if not 0 < p1 < 1 or not 0 < p2 < 1:
        raise ValueError("rates must be in (0, 1)")
    diff = p1 - p2
    if diff == 0:
        return None
    z_alpha = normsinv(1 - alpha / 2)
    z_power = normsinv(power)
    variance = p1 * (1 - p1) + p2 * (1 - p2)
    return math.ceil((z_alpha + z_power) ** 2 * variance / diff**2)


def required_samples(
    baseline_rate: float,
    minimum_detectable_effect: float,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    treatment_share: float = 0.5,
) -> dict[str, int] | None:
    """Samples for a two-sided two-proportion test at a fixed allocation.

    ``treatment_share`` is the fraction of the sample in the treatment arm.
    The control arm receives the rest. Returns None when the baseline is 0 or
    1, or when adding the effect leaves the treatment rate outside (0, 1):
    no finite sample separates those cases. The result is not reduced to fit
    a small population.
    """
    if not 0.0 < treatment_share < 1.0:
        raise ValueError("treatment_share must be in (0, 1)")
    if not 0.0 < baseline_rate < 1.0:
        return None
    treatment_rate = baseline_rate + minimum_detectable_effect
    if not 0.0 < treatment_rate < 1.0:
        return None
    if minimum_detectable_effect == 0:
        return None
    control_share = 1.0 - treatment_share
    treatment_per_control = treatment_share / control_share
    z_alpha = normsinv(1 - alpha / 2)
    z_power = normsinv(power)
    variance = (
        baseline_rate * (1 - baseline_rate)
        + treatment_rate * (1 - treatment_rate) / treatment_per_control
    )
    control_n = math.ceil((z_alpha + z_power) ** 2 * variance / minimum_detectable_effect**2)
    treatment_n = math.ceil(treatment_per_control * control_n)
    return {"control": control_n, "treatment": treatment_n}


@dataclass(frozen=True)
class RateEstimate:
    successes: int
    n: int
    rate: float | None
    low: float | None
    high: float | None

    def as_dict(self) -> dict:
        return {
            "successes": self.successes,
            "n": self.n,
            "rate": self.rate,
            "low": self.low,
            "high": self.high,
        }


@dataclass(frozen=True)
class RateComparison:
    """``difference`` is left minus right. ``verdict`` is never a winner by itself."""

    left: RateEstimate
    right: RateEstimate
    difference: float | None
    z: float | None
    p_value: float | None
    adjusted_alpha: float
    minimum_n: int
    min_abs_change: float
    verdict: str

    def supported_side(self) -> str | None:
        """The side with the higher rate, or None when the evidence is not enough."""
        if self.verdict == "left_higher":
            return "left"
        if self.verdict == "left_lower":
            return "right"
        return None

    def as_dict(self) -> dict:
        return {
            "left": self.left.as_dict(),
            "right": self.right.as_dict(),
            "difference": _finite(self.difference),
            "z": _finite(self.z),
            "p_value": _finite(self.p_value),
            "adjusted_alpha": self.adjusted_alpha,
            "minimum_n": self.minimum_n,
            "min_abs_change": self.min_abs_change,
            "verdict": self.verdict,
            "supported_side": self.supported_side(),
        }


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def estimate_rate(successes: int, n: int, confidence: float = 0.95) -> RateEstimate:
    if n < 0 or successes < 0:
        raise ValueError("counts must be non-negative")
    if n == 0:
        return RateEstimate(successes, 0, None, None, None)
    interval = wilson_interval(successes, n, confidence)
    assert interval is not None
    return RateEstimate(successes, n, successes / n, interval[0], interval[1])


def compare_rates(
    left_successes: int,
    left_n: int,
    right_successes: int,
    right_n: int,
    *,
    alpha: float = 0.05,
    minimum_n: int = 1,
    min_abs_change: float = 0.0,
    confidence: float = 0.95,
) -> RateComparison:
    left = estimate_rate(left_successes, left_n, confidence)
    right = estimate_rate(right_successes, right_n, confidence)
    if left.n < minimum_n or right.n < minimum_n or left.rate is None or right.rate is None:
        return RateComparison(left, right, None, None, None, alpha, minimum_n, min_abs_change, "insufficient")
    difference = left.rate - right.rate
    p_pool = (left_successes + right_successes) / (left_n + right_n)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / left_n + 1 / right_n))
    if se == 0:
        z = 0.0 if difference == 0 else math.copysign(math.inf, difference)
        p_value = 1.0 if difference == 0 else 0.0
    else:
        z = difference / se
        p_value = min(1.0, 2 * normal_sf(abs(z)))
    if p_value >= alpha or abs(difference) < min_abs_change:
        verdict = "not_significant"
    elif difference > 0:
        verdict = "left_higher"
    else:
        verdict = "left_lower"
    return RateComparison(
        left, right, difference, z, p_value, alpha, minimum_n, min_abs_change, verdict
    )


def excess_drop_p_value(
    baseline_successes: int,
    baseline_n: int,
    current_successes: int,
    current_n: int,
    max_drop: float,
) -> float | None:
    """One-sided p-value for H1: baseline_rate - current_rate > max_drop.

    The standard error is unpooled because the null is not "the rates are equal".
    """
    if baseline_n <= 0 or current_n <= 0:
        return None
    p_b = baseline_successes / baseline_n
    p_c = current_successes / current_n
    se = math.sqrt(p_b * (1 - p_b) / baseline_n + p_c * (1 - p_c) / current_n)
    drop = p_b - p_c
    if se == 0:
        return 0.0 if drop > max_drop else 1.0
    return normal_sf((drop - max_drop) / se)


def false_positive_rate(
    *,
    n: int,
    p: float,
    trials: int,
    alpha: float,
    seed: int,
    minimum_n: int = 1,
    min_abs_change: float = 0.0,
) -> float:
    """Share of null simulations the comparison calls a confirmed difference."""
    rng = np.random.default_rng(seed)
    left = rng.binomial(n, p, size=trials)
    right = rng.binomial(n, p, size=trials)
    hits = 0
    for a, b in zip(left.tolist(), right.tolist()):
        verdict = compare_rates(
            int(a), n, int(b), n, alpha=alpha, minimum_n=minimum_n, min_abs_change=min_abs_change
        ).verdict
        if verdict in ("left_higher", "left_lower"):
            hits += 1
    return hits / trials


def detection_rate(
    *,
    n: int,
    p_left: float,
    p_right: float,
    trials: int,
    alpha: float,
    seed: int,
    minimum_n: int = 1,
    min_abs_change: float = 0.0,
) -> float:
    """Share of simulations with a real gap that the comparison confirms."""
    rng = np.random.default_rng(seed)
    left = rng.binomial(n, p_left, size=trials)
    right = rng.binomial(n, p_right, size=trials)
    hits = 0
    for a, b in zip(left.tolist(), right.tolist()):
        verdict = compare_rates(
            int(a), n, int(b), n, alpha=alpha, minimum_n=minimum_n, min_abs_change=min_abs_change
        ).supported_side()
        if verdict is not None:
            hits += 1
    return hits / trials
