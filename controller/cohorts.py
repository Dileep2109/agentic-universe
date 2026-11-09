"""One shared cohort definition per workflow.

Matching follows the simulator: cohorts are an ordered list, the first match
wins, a ``where`` clause is AND across features and OR within a feature, and
everyone else is ``default``. A lead with any missing feature is ``missing``.
That bucket is reported on its own and is not part of a cohort comparison.

The builder splits on at most one feature. One pairwise comparison on the
primary call KPI is what selects that feature. Every eligible level of the
feature is then emitted. Emitting a level does not mean that level was the
significant pair.
"""

from __future__ import annotations

from dataclasses import dataclass

from .settings import FEATURE_VALUES, SIMULATOR_COHORT_LIMIT, CohortPolicy, DecisionThresholds
from .stats import bonferroni, compare_rates


class CohortError(ValueError):
    pass


# Family: every eligible pair of feature levels on the primary call KPI.
# Bonferroni uses the number of those pairs. Guardrail metrics are not in it.
COHORT_SELECTION_FAMILY = "cohort_selection"

COMPLETE_FEATURE_DENOMINATOR = (
    "Cohort comparisons use only calls whose lang, city_tier, age_band, and value_band "
    "are all present. A call with any missing feature is assigned to 'missing', reported "
    "on its own, and excluded from both the cohort and its complement. The complement is "
    "the other complete-feature calls, including other cohorts and default. Missing calls "
    "are not mixed into that denominator."
)


@dataclass(frozen=True)
class Cohort:
    id: str
    where: dict[str, tuple[str, ...]]
    support_leads: int = 0
    support_dials: int = 0
    reason: str = ""

    def as_dict(self, position: int) -> dict:
        return {
            "position": position,
            "cohort_id": self.id,
            "where": {key: list(values) for key, values in self.where.items()},
            "support_leads": self.support_leads,
            "support_dials": self.support_dials,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CohortSet:
    cohorts: tuple[Cohort, ...]
    selection: dict | None = None

    def assign(self, features: dict[str, str]) -> str:
        """Return the first matching cohort id, ``missing``, or ``default``.

        ``missing`` means at least one of lang, city_tier, age_band, or
        value_band is absent. Those calls stay out of ``default`` and out of
        every cohort comparison. See ``COMPLETE_FEATURE_DENOMINATOR``.
        """
        if any(not features.get(name) for name in FEATURE_VALUES):
            return "missing"
        for cohort in self.cohorts:
            if _matches(cohort.where, features):
                return cohort.id
        return "default"


@dataclass(frozen=True)
class LevelCounts:
    feature: str
    value: str
    leads: int
    dials: int
    connected: int
    engaged: int


def validate_cohorts(cohorts: list[Cohort]) -> None:
    if len(cohorts) > SIMULATOR_COHORT_LIMIT:
        raise CohortError(f"at most {SIMULATOR_COHORT_LIMIT} cohorts")
    seen: set[str] = set()
    for cohort in cohorts:
        if not cohort.id or cohort.id in ("default", "missing") or cohort.id in seen:
            raise CohortError(f"invalid cohort id {cohort.id!r}")
        seen.add(cohort.id)
        if not cohort.where:
            raise CohortError(f"cohort {cohort.id} has an empty where clause")
        for feature, values in cohort.where.items():
            if feature not in FEATURE_VALUES:
                raise CohortError(f"cohort {cohort.id} uses unknown feature {feature}")
            if not values:
                raise CohortError(f"cohort {cohort.id} has no values for {feature}")
            bad = [value for value in values if value not in FEATURE_VALUES[feature]]
            if bad:
                raise CohortError(f"cohort {cohort.id} has values the API rejects for {feature}: {bad}")


def build_cohort_set(
    levels: list[LevelCounts],
    total_leads: int,
    *,
    primary_metric: str,
    policy: CohortPolicy | None = None,
    thresholds: DecisionThresholds | None = None,
) -> tuple[CohortSet, str]:
    """Split on at most one feature, and only with a confirmed primary-KPI gap.

    Levels of that feature which clear the size floor become the ordered
    cohort list. Levels that are too small stay in ``default``.
    """
    policy = policy or CohortPolicy()
    thresholds = thresholds or DecisionThresholds()
    if primary_metric not in ("pickup_rate", "engagement_rate"):
        return CohortSet(()), f"primary KPI {primary_metric!r} is not a call rate this builder splits on"
    if total_leads <= 0:
        return CohortSet(()), "no leads in the baseline period"
    if policy.max_cohorts < 1:
        raise CohortError("max_cohorts must be >= 1")

    by_feature: dict[str, list[LevelCounts]] = {}
    for level in levels:
        if level.feature not in policy.features or level.value not in FEATURE_VALUES.get(level.feature, ()):
            continue
        share = level.leads / total_leads
        if share < policy.min_lead_share or level.dials < policy.min_dials:
            continue
        by_feature.setdefault(level.feature, []).append(level)

    candidates = {feature: rows for feature, rows in by_feature.items() if len(rows) >= 2}
    pairs: list[tuple[str, LevelCounts, LevelCounts]] = []
    for feature, rows in candidates.items():
        ordered = sorted(rows, key=lambda row: row.value)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                pairs.append((feature, left, right))
    if not pairs:
        return CohortSet(()), "no feature has two levels above the size floor"

    alpha = bonferroni(thresholds.alpha, len(pairs))
    minimum_n = (
        thresholds.pickup_min_dials if primary_metric == "pickup_rate" else thresholds.engagement_min_connected
    )
    best: tuple | None = None
    for feature, left, right in pairs:
        comparison = _compare_level(left, right, primary_metric, alpha, minimum_n, thresholds.min_abs_change)
        if comparison.supported_side() is None or comparison.difference is None:
            continue
        gap = abs(comparison.difference)
        pair_key = (feature, left.value, right.value)
        if best is None or gap > best[0] or (gap == best[0] and pair_key < best[1]):
            best = (gap, pair_key, left, right, comparison)
    if best is None:
        return CohortSet(()), (
            f"no {primary_metric} gap cleared alpha {alpha:.4f} "
            f"across {len(pairs)} pairs"
        )

    _gap, pair_key, left, right, comparison = best
    feature = pair_key[0]
    chosen = sorted(candidates[feature], key=lambda row: (-row.leads, row.value))
    truncated = len(chosen) > policy.max_cohorts
    chosen = chosen[: min(policy.max_cohorts, SIMULATOR_COHORT_LIMIT)]
    selection = _selection_evidence(
        feature, left, right, comparison, primary_metric, len(pairs), [row.value for row in chosen]
    )
    cohorts = tuple(
        Cohort(
            id=f"{feature}:{row.value}",
            where={feature: (row.value,)},
            support_leads=row.leads,
            support_dials=row.dials,
            reason=_level_reason(row, selection),
        )
        for row in chosen
    )
    validate_cohorts(list(cohorts))
    note = (
        f"shared cohorts partition {feature}; the selecting pair is "
        f"{selection['compared_levels'][0]} vs {selection['compared_levels'][1]}, "
        f"and other emitted levels only clear the size floor"
    )
    if truncated:
        note += f"; kept the {len(cohorts)} largest levels"
    return CohortSet(cohorts, selection), note


def _matches(where: dict[str, tuple[str, ...]], features: dict[str, str]) -> bool:
    return all(features.get(feature) in values for feature, values in where.items())


def _selection_evidence(feature, left, right, comparison, metric: str, n_pairs: int, emitted: list[str]) -> dict:
    """The one pair that selected the feature. Emitted levels are listed separately."""
    return {
        "family": COHORT_SELECTION_FAMILY,
        "metric": metric,
        "feature": feature,
        "compared_levels": [left.value, right.value],
        "left": _side(left, comparison.left),
        "right": _side(right, comparison.right),
        "gap": abs(comparison.difference),
        "p_value": comparison.p_value,
        "adjusted_alpha": comparison.adjusted_alpha,
        "comparisons_in_family": n_pairs,
        "emitted_levels": list(emitted),
    }


def _side(level: LevelCounts, estimate) -> dict:
    return {
        "level": level.value,
        "successes": estimate.successes,
        "n": estimate.n,
        "rate": estimate.rate,
    }


def _level_reason(level: LevelCounts, selection: dict) -> str:
    pair = selection["compared_levels"]
    pair_text = f"{pair[0]} vs {pair[1]}"
    if level.value in pair:
        role = f"{level.value} is one side of the selecting pair {pair_text}"
    else:
        role = (
            f"{level.value} was not in the selecting pair {pair_text}; "
            f"it is emitted because it clears the size floor on {selection['feature']}"
        )
    return (
        f"{role}. The pair, not this level alone, supported the {selection['metric']} "
        f"gap of {selection['gap']:.3f} (p={selection['p_value']:.4g}, "
        f"adjusted alpha={selection['adjusted_alpha']:.4g}, "
        f"n={selection['left']['n']} vs {selection['right']['n']}). "
        f"Emitting this level does not mean it independently demonstrated that gap. "
        f"This level has {level.leads} leads and {level.dials} dials."
    )


def _compare_level(left, right, metric: str, alpha: float, minimum_n: int, min_abs_change: float):
    if metric == "pickup_rate":
        return compare_rates(
            left.connected, left.dials, right.connected, right.dials,
            alpha=alpha, minimum_n=minimum_n, min_abs_change=min_abs_change,
        )
    return compare_rates(
        left.engaged, left.connected, right.engaged, right.connected,
        alpha=alpha, minimum_n=minimum_n, min_abs_change=min_abs_change,
    )
