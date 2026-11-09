"""Run settings and the statistical thresholds the analytics layer reads.

These thresholds decide when a difference is large enough to report. They do
not promote a variant, and they do not read prompt-summary text.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class DecisionThresholds:
    """Population floors and error rates used by Phase 2 comparisons.

    Pickup, engagement, and business outcome stay separate. None of these
    values blends those rates, and none of them is a prompt-summary rule.

    ``alpha`` is the family-wise target for a primary-KPI family before
    Bonferroni splits it. ``guardrail_confidence`` is separate: a guardrail
    test uses ``1 - guardrail_confidence`` and is not pooled with the primary
    family. Difference-in-differences, decay, and hallucination thresholds are
    not defined here until the phase that applies them.
    """

    engagement_min_connected: int = 400
    pickup_min_dials: int = 800
    alpha: float = 0.05
    guardrail_confidence: float = 0.90
    business_min_leads: int = 400
    min_abs_change: float = 0.02
    confidence: float = 0.95

    def guardrail_alpha(self) -> float:
        if not 0.0 < self.guardrail_confidence < 1.0:
            raise ValueError("guardrail_confidence must be in (0, 1)")
        return 1.0 - self.guardrail_confidence

    def overridden(self, **updates: float | int) -> DecisionThresholds:
        known = {item.name for item in fields(self)}
        unknown = set(updates) - known
        if unknown:
            raise ValueError(f"unknown threshold: {sorted(unknown)}")
        return replace(self, **updates)


# Documented simulator clock: D0 is 2027-01-04T00:00:00+05:30.
SIM_DAY_ZERO = "2027-01-04"

# Values the simulator accepts in a cohort ``where`` clause. Copied from the
# public config validator so this package does not import ``sim``.
FEATURE_VALUES = {
    "lang": ("hi", "en", "ta", "te", "kn", "mr", "bn"),
    "city_tier": ("1", "2", "3"),
    "age_band": ("18-30", "31-45", "46-60", "60+"),
    "value_band": ("low", "mid", "high"),
}
SIMULATOR_COHORT_LIMIT = 20


@dataclass(frozen=True)
class CohortPolicy:
    """When a feature level is large enough to be its own cohort.

    The builder still requires a confirmed rate difference before it splits.
    Size alone does not create a cohort.
    """

    min_lead_share: float = 0.12
    min_dials: int = 800
    max_cohorts: int = 6
    features: tuple[str, ...] = ("age_band", "lang", "city_tier", "value_band")

    def overridden(self, **updates: float | int | tuple) -> CohortPolicy:
        known = {item.name for item in fields(self)}
        unknown = set(updates) - known
        if unknown:
            raise ValueError(f"unknown cohort policy: {sorted(unknown)}")
        return replace(self, **updates)


@dataclass(frozen=True)
class ExperimentPolicy:
    """Design constants for one confirmatory experiment.

    The detectable effect is absolute, on the primary KPI's own rate. It is
    not lowered when the population is small. Welcome message and voice are
    the dimensions this phase will design. Prompt text stays a suggestion
    until a separate safety review, and it is never a winner by itself.
    """

    minimum_detectable_effect: float = 0.03
    power: float = 0.80
    treatment_share: float = 0.5
    dimensions: tuple[str, ...] = ("welcome_message", "voice")
    suggest_fixes_credit_cost: int = 3

    def overridden(self, **updates: float | int | tuple) -> ExperimentPolicy:
        known = {item.name for item in fields(self)}
        unknown = set(updates) - known
        if unknown:
            raise ValueError(f"unknown experiment policy: {sorted(unknown)}")
        return replace(self, **updates)


@dataclass(frozen=True)
class RunSettings:
    tick_minutes: int = 15
    request_timeout_s: float = 120.0
    chunk_timeout_s: float = 300.0
    idempotent_retries: int = 3
    retry_sleep_s: float = 0.25
    max_consecutive_advance_failures: int = 5
    thresholds: DecisionThresholds = DecisionThresholds()
    cohorts: CohortPolicy = CohortPolicy()
    experiments: ExperimentPolicy = ExperimentPolicy()
