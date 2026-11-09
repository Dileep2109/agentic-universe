"""Directional impact records.

An observed gap is not a predicted uplift. This module never fills in a
precise expected effect from a directional finding, and the score it returns
cannot authorize a configuration write.
"""

from __future__ import annotations


def assess_impact(
    *,
    primary_kpi: str | None,
    guardrail_kpi: str | None,
    baseline_rate: float | None,
    observed_rate: float | None,
    sample_n: int | None,
    ci_low: float | None,
    ci_high: float | None,
    affected_population: int | None,
) -> dict:
    """Record what was observed. Leave expected, assumed, and unverified effects empty."""
    if (
        primary_kpi is None
        or baseline_rate is None
        or observed_rate is None
        or sample_n is None
        or sample_n < 1
        or not 0.0 <= baseline_rate <= 1.0
        or not 0.0 <= observed_rate <= 1.0
    ):
        return {
            "primary_kpi": primary_kpi,
            "guardrail_kpi": guardrail_kpi,
            "observed_effect": None,
            "expected_effect": None,
            "assumed_effect": None,
            "unverified_effect": None,
            "effect_kind": "unverified",
            "impact_score": 0.0,
            "sample_n": sample_n,
            "baseline_rate": baseline_rate,
            "observed_rate": observed_rate,
            "confidence_interval": None,
            "affected_population": affected_population,
            "downside": "unknown",
            "note": "Impact is unverified. No uplift is claimed.",
        }
    observed = observed_rate - baseline_rate
    interval = None
    if ci_low is not None and ci_high is not None:
        interval = {"low": ci_low, "high": ci_high}
    downside = "possible"
    if observed < 0:
        downside = "the compared rate is lower than the reference rate"
    elif observed == 0:
        downside = "no observed difference"
    return {
        "primary_kpi": primary_kpi,
        "guardrail_kpi": guardrail_kpi,
        "observed_effect": observed,
        "expected_effect": None,
        "assumed_effect": None,
        "unverified_effect": None,
        "effect_kind": "observed",
        "impact_score": min(1.0, abs(observed)),
        "sample_n": sample_n,
        "baseline_rate": baseline_rate,
        "observed_rate": observed_rate,
        "confidence_interval": interval,
        "affected_population": affected_population,
        "downside": downside,
        "note": (
            "The score is the absolute observed gap, clipped to [0, 1]. "
            "It is not a predicted uplift and it does not bypass governance."
        ),
    }
