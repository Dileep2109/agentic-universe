"""Compare periods, cohorts, and hours without promoting a configuration.

A finding is ``confirmed`` only when the sample floor, the adjusted p-value,
and the minimum absolute change all hold. A large swing on a small sample
stays an ``observation`` or ``insufficient``. A confirmed finding is a
statistical difference. It is not a causal effect, not an incident, and it
does not submit a change.

Multiple-testing families, each with its own Bonferroni split. A comparison
belongs to exactly one family. Guardrail families do not share alpha with
primary-KPI families.

- ``cohort_selection``: every eligible pairwise level comparison on the
  primary call KPI. Size is the number of eligible pairs. Defined in
  ``cohorts.py``. Guardrails are not members.
- ``live_vs_history_kpis``: pickup, engagement, and business outcome, live
  versus history. Size is always 3, including a metric that is later marked
  insufficient. Observational only.
- ``live_vs_history_guardrail``: the single excess-drop test named by the
  workflow objective. Alpha is ``1 - guardrail_confidence``, not ``alpha``.
- ``cohort_contrast_primary``: each complete-feature cohort versus the
  complement of other complete-feature calls, on the primary KPI. Size is the
  number of slices that meet the sample floor.
- ``cohort_contrast_guardrail``: the same slices on the guardrail metric, with
  guardrail alpha. Omitted when that metric is already the primary, so the
  metric is not tested twice.
- ``hour_contrast_pickup``: each hour versus its complement on pickup. Not
  mixed with cohort or period tests.
"""

from __future__ import annotations

from .settings import DecisionThresholds
from .stats import bonferroni, compare_rates, excess_drop_p_value


_NUMERATOR = {
    "pickup_rate": "connected",
    "engagement_rate": "engaged",
    "business_outcome": "leads_concluded",
}
_DENOMINATOR = {
    "pickup_rate": "dials",
    "engagement_rate": "connected",
    "business_outcome": "leads_uploaded",
}

LIVE_VS_HISTORY_KPI_FAMILY = "live_vs_history_kpis"
LIVE_VS_HISTORY_GUARDRAIL_FAMILY = "live_vs_history_guardrail"
COHORT_CONTRAST_PRIMARY_FAMILY = "cohort_contrast_primary"
COHORT_CONTRAST_GUARDRAIL_FAMILY = "cohort_contrast_guardrail"
HOUR_CONTRAST_FAMILY = "hour_contrast_pickup"

OBSERVATIONAL_WARNING = (
    "Live versus history is observational. Calendar time, lead mix, campaign, and "
    "configuration can all change together, so this statistical difference is not a "
    "causal effect and it is not an incident."
)

TESTING_FAMILIES = (
    "cohort_selection",
    LIVE_VS_HISTORY_KPI_FAMILY,
    LIVE_VS_HISTORY_GUARDRAIL_FAMILY,
    COHORT_CONTRAST_PRIMARY_FAMILY,
    COHORT_CONTRAST_GUARDRAIL_FAMILY,
    HOUR_CONTRAST_FAMILY,
)


def minimum_n(metric: str, thresholds: DecisionThresholds) -> int:
    if metric == "pickup_rate":
        return thresholds.pickup_min_dials
    if metric == "engagement_rate":
        return thresholds.engagement_min_connected
    if metric == "business_outcome":
        return thresholds.business_min_leads
    raise ValueError(f"unknown metric {metric}")


def assess_change(
    baseline: dict,
    current: dict,
    metric: str,
    thresholds: DecisionThresholds,
    *,
    adjusted_alpha: float | None = None,
    subject: str = "",
    family: str | None = None,
    family_size: int | None = None,
    observational_warning: str | None = None,
) -> dict:
    """Compare ``current`` with ``baseline``. Difference is current minus baseline.

    The result is a statistical comparison. ``causal`` is always false.
    """
    alpha = thresholds.alpha if adjusted_alpha is None else adjusted_alpha
    numerator = _NUMERATOR[metric]
    denominator = _DENOMINATOR[metric]
    base_n = int(baseline.get(denominator) or 0)
    cur_n = int(current.get(denominator) or 0)
    base_s = int(baseline.get(numerator) or 0)
    cur_s = int(current.get(numerator) or 0)
    comparison = compare_rates(
        cur_s,
        cur_n,
        base_s,
        base_n,
        alpha=alpha,
        minimum_n=minimum_n(metric, thresholds),
        min_abs_change=0.0,
        confidence=thresholds.confidence,
    )
    difference = comparison.difference
    if comparison.verdict == "insufficient" or difference is None:
        status = "insufficient"
        direction = "unknown"
    elif comparison.verdict == "not_significant" and abs(difference) >= thresholds.min_abs_change:
        status = "observation"
        direction = "up" if difference > 0 else "down"
    elif comparison.verdict == "not_significant" or abs(difference) < thresholds.min_abs_change:
        status = "within_variation"
        direction = "flat" if difference == 0 else ("up" if difference > 0 else "down")
    else:
        status = "confirmed"
        direction = "up" if difference > 0 else "down"
    finding = {
        "metric": metric,
        "subject": subject,
        "status": status,
        "direction": direction,
        "role": "comparison",
        "causal": False,
        "family": family,
        "family_size": family_size,
        "evidence": comparison.as_dict(),
        "note": _note(status),
    }
    if observational_warning:
        finding["observational_warning"] = observational_warning
    return finding


def assess_guardrail(
    objective: dict | None,
    baseline: dict,
    current: dict,
    thresholds: DecisionThresholds,
    *,
    subject: str = "",
) -> dict | None:
    """Test the guardrail metric on its own. It does not look at the primary KPI."""
    guard = (objective or {}).get("guardrail") or {}
    if "pickup_rate_max_drop" in guard:
        metric = "pickup_rate"
        max_drop = float(guard["pickup_rate_max_drop"])
    elif "engagement_rate_max_drop" in guard:
        metric = "engagement_rate"
        max_drop = float(guard["engagement_rate_max_drop"])
    else:
        return None
    numerator = _NUMERATOR[metric]
    denominator = _DENOMINATOR[metric]
    base_n = int(baseline.get(denominator) or 0)
    cur_n = int(current.get(denominator) or 0)
    base_s = int(baseline.get(numerator) or 0)
    cur_s = int(current.get(numerator) or 0)
    floor = minimum_n(metric, thresholds)
    if base_n < floor or cur_n < floor or base_n == 0 or cur_n == 0:
        return {
            "metric": metric,
            "subject": subject,
            "status": "insufficient",
            "direction": "unknown",
            "role": "guardrail",
            "causal": False,
            "max_drop": max_drop,
            "point_drop": None,
            "p_value": None,
            "evidence": {
                "baseline_n": base_n,
                "current_n": cur_n,
                "minimum_n": floor,
                "alpha": thresholds.guardrail_alpha(),
                "p_value": None,
            },
            "note": _note("insufficient"),
        }
    point_drop = base_s / base_n - cur_s / cur_n
    p_value = excess_drop_p_value(base_s, base_n, cur_s, cur_n, max_drop)
    guard_alpha = thresholds.guardrail_alpha()
    if point_drop > max_drop and p_value is not None and p_value < guard_alpha:
        status = "confirmed"
        direction = "down"
    elif point_drop > max_drop:
        status = "observation"
        direction = "down"
    else:
        status = "within_variation"
        direction = "flat" if point_drop == 0 else "down"
    return {
        "metric": metric,
        "subject": subject,
        "status": status,
        "direction": direction,
        "role": "guardrail",
        "causal": False,
        "max_drop": max_drop,
        "point_drop": point_drop,
        "p_value": p_value,
        "evidence": {
            "baseline_rate": base_s / base_n,
            "current_rate": cur_s / cur_n,
            "baseline_n": base_n,
            "current_n": cur_n,
            "minimum_n": floor,
            "alpha": guard_alpha,
            "p_value": p_value,
        },
        "note": _note(status),
    }


def assess_slices(
    slices: list[dict],
    metric: str,
    thresholds: DecisionThresholds,
    *,
    subject_key: str,
    family: str,
    family_alpha: float | None = None,
) -> list[dict]:
    """Compare each slice with its complement.

    Bonferroni covers every slice in this family that meets the sample floor
    on both the slice and the complement. Slices below the floor are reported
    and are not counted as tests. ``family_alpha`` defaults to the primary
    ``alpha``; guardrail callers pass ``guardrail_alpha()`` instead.
    """
    floor = minimum_n(metric, thresholds)
    numerator = _NUMERATOR[metric]
    denominator = _DENOMINATOR[metric]
    base_alpha = thresholds.alpha if family_alpha is None else family_alpha
    testable = []
    for sl in slices:
        n = int(sl.get(denominator) or 0)
        total_n = int(sl.get("_total_" + denominator) or 0)
        complement_n = total_n - n
        if n >= floor and complement_n >= floor:
            testable.append(sl)
    alpha = bonferroni(base_alpha, len(testable)) if testable else base_alpha
    findings = []
    for sl in slices:
        total = {
            denominator: sl.get("_total_" + denominator) or 0,
            numerator: sl.get("_total_" + numerator) or 0,
        }
        current = {denominator: sl.get(denominator) or 0, numerator: sl.get(numerator) or 0}
        complement = {
            denominator: int(total[denominator]) - int(current[denominator]),
            numerator: int(total[numerator]) - int(current[numerator]),
        }
        n = int(current[denominator])
        complement_n = int(complement[denominator])
        finding = assess_change(
            complement,
            current,
            metric,
            thresholds,
            adjusted_alpha=alpha,
            subject=f"{subject_key}={sl.get(subject_key)}",
            family=family,
            family_size=len(testable),
        )
        finding["slice"] = sl.get(subject_key)
        finding["in_family"] = n >= floor and complement_n >= floor
        findings.append(finding)
    return findings


def _note(status: str) -> str:
    if status == "confirmed":
        return (
            "confirmed statistical difference; not a causal effect, not an incident, "
            "and not a configuration change"
        )
    if status == "observation":
        return "point estimate moved, but the sample does not separate it from ordinary variation"
    if status == "insufficient":
        return "sample is below the configured floor"
    return "inside ordinary variation"
