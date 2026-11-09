"""Statistical gates: intervals, power, false positives, and no winner without evidence."""

import math

import pytest

from controller.stats import (
    bonferroni,
    compare_rates,
    detection_rate,
    excess_drop_p_value,
    false_positive_rate,
    normsinv,
    required_n_per_arm,
    wilson_interval,
)


def test_normsinv_matches_known_quantiles():
    assert normsinv(0.975) == pytest.approx(1.95996398454, abs=1e-6)
    assert normsinv(0.95) == pytest.approx(1.64485362695, abs=1e-6)
    assert normsinv(0.5) == pytest.approx(0.0, abs=1e-6)


def test_wilson_covers_the_point_and_shrinks_toward_the_middle():
    interval = wilson_interval(30, 100, 0.95)
    assert interval is not None
    low, high = interval
    assert low < 0.30 < high
    assert 0.0 <= low and high <= 1.0
    assert wilson_interval(0, 0) is None


def test_sample_size_is_finite_only_when_the_rates_differ():
    n = required_n_per_arm(0.30, 0.40, alpha=0.05, power=0.80)
    assert n is not None and n > 100
    assert required_n_per_arm(0.30, 0.30) is None


def test_bonferroni_family_keeps_simultaneous_false_positives_down():
    """Ten null comparisons. Uncorrected family-wise errors pile up; Bonferroni does not."""
    uncorrected = _family_wise_error(comparisons=10, alpha=0.05, corrected=False, trials=400, seed=21)
    corrected = _family_wise_error(comparisons=10, alpha=0.05, corrected=True, trials=400, seed=21)
    assert uncorrected > 0.25
    assert corrected < 0.12
    assert corrected < uncorrected


def _family_wise_error(*, comparisons: int, alpha: float, corrected: bool, trials: int, seed: int) -> float:
    import numpy as np

    rng = np.random.default_rng(seed)
    threshold = bonferroni(alpha, comparisons) if corrected else alpha
    hits = 0
    for _ in range(trials):
        any_hit = False
        for _comparison in range(comparisons):
            left = int(rng.binomial(500, 0.30))
            right = int(rng.binomial(500, 0.30))
            verdict = compare_rates(
                left, 500, right, 500, alpha=threshold, minimum_n=1, min_abs_change=0.0
            ).verdict
            if verdict in ("left_higher", "left_lower"):
                any_hit = True
                break
        if any_hit:
            hits += 1
    return hits / trials


def test_bonferroni_splits_alpha():
    assert bonferroni(0.05, 10) == pytest.approx(0.005)
    barely = compare_rates(240, 500, 200, 500, alpha=0.05, minimum_n=100, min_abs_change=0.0)
    adjusted = compare_rates(240, 500, 200, 500, alpha=bonferroni(0.05, 20), minimum_n=100, min_abs_change=0.0)
    assert barely.p_value is not None and adjusted.p_value == barely.p_value
    assert barely.supported_side() is not None
    assert adjusted.supported_side() is None


def test_null_false_positive_rate_stays_near_alpha():
    rate = false_positive_rate(n=400, p=0.30, trials=2000, alpha=0.05, seed=11)
    assert 0.025 <= rate <= 0.080


def test_power_at_the_planned_sample_size():
    n = required_n_per_arm(0.30, 0.40, alpha=0.05, power=0.80)
    assert n is not None
    power = detection_rate(
        n=n, p_left=0.40, p_right=0.30, trials=400, alpha=0.05, seed=12, min_abs_change=0.0
    )
    assert power > 0.65


def test_no_winner_for_small_samples_missing_data_or_a_tiny_gap():
    small = compare_rates(8, 10, 1, 10, alpha=0.05, minimum_n=800, min_abs_change=0.02)
    assert small.verdict == "insufficient"
    assert small.supported_side() is None

    missing = compare_rates(0, 0, 10, 20, alpha=0.05, minimum_n=1)
    assert missing.verdict == "insufficient"
    assert missing.supported_side() is None

    # A real but 1pp gap can be "significant" at huge n and still fail the practical floor.
    tiny = compare_rates(31000, 100000, 30000, 100000, alpha=0.05, minimum_n=100, min_abs_change=0.0)
    assert tiny.p_value is not None and tiny.p_value < 0.05
    assert abs(tiny.difference) < 0.02
    gated = compare_rates(31000, 100000, 30000, 100000, alpha=0.05, minimum_n=100, min_abs_change=0.02)
    # compare_rates itself treats a sub-threshold gap as not significant.
    assert gated.verdict == "not_significant"
    assert gated.supported_side() is None


def test_genuine_regression_and_improvement_are_directional():
    drop = compare_rates(250, 1000, 400, 1000, alpha=0.05, minimum_n=100, min_abs_change=0.02)
    assert drop.verdict == "left_lower"
    assert drop.supported_side() == "right"
    lift = compare_rates(400, 1000, 250, 1000, alpha=0.05, minimum_n=100, min_abs_change=0.02)
    assert lift.verdict == "left_higher"
    assert lift.supported_side() == "left"


def test_equal_rates_are_not_a_winner_even_at_large_n():
    same = compare_rates(300, 1000, 300, 1000, alpha=0.05, minimum_n=100, min_abs_change=0.0)
    assert same.verdict == "not_significant"
    assert same.p_value == pytest.approx(1.0)
    assert same.supported_side() is None
    assert excess_drop_p_value(300, 1000, 300, 1000, 0.02) > 0.5
    assert math.isfinite(same.z)
