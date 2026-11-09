"""Shared cohort matching and evidence-based formation."""

import pytest

from controller.cohorts import Cohort, CohortError, CohortSet, LevelCounts, build_cohort_set, validate_cohorts
from controller.settings import CohortPolicy, DecisionThresholds


def _level(feature, value, leads, dials, connected, engaged) -> LevelCounts:
    return LevelCounts(feature, value, leads, dials, connected, engaged)


def test_first_match_and_missing_features():
    cohorts = CohortSet((
        Cohort("senior_ta", {"age_band": ("60+",), "lang": ("ta",)}),
        Cohort("senior", {"age_band": ("60+",)}),
        Cohort("young", {"age_band": ("18-30",)}),
    ))
    assert cohorts.assign({"lang": "ta", "city_tier": "1", "age_band": "60+", "value_band": "low"}) == "senior_ta"
    assert cohorts.assign({"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"}) == "senior"
    assert cohorts.assign({"lang": "hi", "city_tier": "2", "age_band": "31-45", "value_band": "mid"}) == "default"
    assert cohorts.assign({"lang": "", "city_tier": "1", "age_band": "60+", "value_band": "low"}) == "missing"


def test_validator_enforces_simulator_limits():
    with pytest.raises(CohortError):
        validate_cohorts([Cohort("default", {"lang": ("hi",)})])
    with pytest.raises(CohortError):
        validate_cohorts([Cohort("empty", {})])
    with pytest.raises(CohortError):
        validate_cohorts([Cohort("bad", {"lang": ("xx",)})])
    with pytest.raises(CohortError):
        validate_cohorts([Cohort(f"c{i}", {"lang": ("hi",)}) for i in range(21)])


def test_null_and_small_populations_do_not_create_cohorts():
    null_levels = [
        _level("age_band", "18-30", 5000, 4000, 1200, 600),
        _level("age_band", "60+", 5000, 4000, 1200, 600),
    ]
    cohorts, note = build_cohort_set(null_levels, 10000, primary_metric="pickup_rate")
    assert cohorts.cohorts == ()
    assert "no " in note

    small = [
        _level("lang", "hi", 20, 10, 1, 0),
        _level("lang", "en", 20, 10, 9, 1),
    ]
    cohorts, note = build_cohort_set(small, 40, primary_metric="pickup_rate")
    assert cohorts.cohorts == ()
    assert "size floor" in note


def test_confirmed_gap_creates_one_shared_partition():
    levels = [
        _level("age_band", "18-30", 3000, 4000, 800, 400),
        _level("age_band", "60+", 3000, 4000, 2000, 1000),
        _level("lang", "hi", 4000, 4000, 1400, 700),
        _level("lang", "en", 4000, 4000, 1500, 750),
        _level("city_tier", "1", 100, 50, 10, 5),
    ]
    cohorts, note = build_cohort_set(
        levels,
        total_leads=6000,
        primary_metric="pickup_rate",
        policy=CohortPolicy(min_lead_share=0.12, min_dials=800, max_cohorts=6),
        thresholds=DecisionThresholds(alpha=0.05, pickup_min_dials=800, min_abs_change=0.02),
    )
    features = {next(iter(cohort.where)) for cohort in cohorts.cohorts}
    assert features == {"age_band"}
    assert {cohort.id for cohort in cohorts.cohorts} == {"age_band:18-30", "age_band:60+"}
    assert "age_band" in note
    evidence = cohorts.selection
    assert evidence["family"] == "cohort_selection"
    assert evidence["compared_levels"] == ["18-30", "60+"]
    assert evidence["comparisons_in_family"] == 2
    assert evidence["adjusted_alpha"] == pytest.approx(0.025)
    assert evidence["p_value"] < evidence["adjusted_alpha"]
    assert evidence["left"]["n"] == 4000 and evidence["right"]["n"] == 4000
    assert evidence["left"]["rate"] == pytest.approx(0.20)
    assert evidence["right"]["rate"] == pytest.approx(0.50)
    assert evidence["gap"] == pytest.approx(0.30)
    for cohort in cohorts.cohorts:
        assert "one side of the selecting pair" in cohort.reason
        assert "independently demonstrated" in cohort.reason
    assigned = CohortSet(cohorts.cohorts).assign(
        {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "mid"}
    )
    assert assigned == "age_band:60+"


def test_subthreshold_gap_is_not_a_split_even_when_the_p_value_is_small():
    levels = [
        _level("value_band", "low", 5000, 20000, 6000, 3000),
        _level("value_band", "high", 5000, 20000, 6200, 3100),
    ]
    cohorts, _note = build_cohort_set(
        levels,
        total_leads=10000,
        primary_metric="pickup_rate",
        thresholds=DecisionThresholds(alpha=0.05, pickup_min_dials=800, min_abs_change=0.02),
    )
    assert cohorts.cohorts == ()
    assert cohorts.selection is None


def test_emitted_level_is_not_described_as_the_selecting_pair():
    levels = [
        _level("age_band", "18-30", 3000, 4000, 800, 400),
        _level("age_band", "31-45", 3000, 4000, 1900, 900),
        _level("age_band", "60+", 3000, 4000, 2000, 1000),
    ]
    policy = CohortPolicy(min_lead_share=0.12, min_dials=800, max_cohorts=6)
    thresholds = DecisionThresholds(alpha=0.05, pickup_min_dials=800, min_abs_change=0.02)
    cohorts, _note = build_cohort_set(
        levels, total_leads=9000, primary_metric="pickup_rate", policy=policy, thresholds=thresholds
    )
    evidence = cohorts.selection
    assert evidence["compared_levels"] == ["18-30", "60+"]
    assert evidence["comparisons_in_family"] == 3
    assert evidence["adjusted_alpha"] == pytest.approx(0.05 / 3)
    assert set(evidence["emitted_levels"]) == {"18-30", "31-45", "60+"}
    reasons = {cohort.id: cohort.reason for cohort in cohorts.cohorts}
    assert "was not in the selecting pair" in reasons["age_band:31-45"]
    assert "one side of the selecting pair" in reasons["age_band:18-30"]
    assert "independently demonstrated" in reasons["age_band:31-45"]
    again, _again_note = build_cohort_set(
        list(reversed(levels)),
        total_leads=9000,
        primary_metric="pickup_rate",
        policy=policy,
        thresholds=thresholds,
    )
    assert [cohort.id for cohort in again.cohorts] == [cohort.id for cohort in cohorts.cohorts]
    assert again.selection == evidence


def test_equal_gaps_select_the_feature_name_that_sorts_first():
    levels = [
        _level("lang", "en", 4000, 4000, 2000, 800),
        _level("lang", "hi", 4000, 4000, 800, 200),
        _level("age_band", "60+", 4000, 4000, 2000, 800),
        _level("age_band", "18-30", 4000, 4000, 800, 200),
    ]
    cohorts, _note = build_cohort_set(
        levels,
        total_leads=8000,
        primary_metric="pickup_rate",
        thresholds=DecisionThresholds(alpha=0.05, pickup_min_dials=800, min_abs_change=0.02),
    )
    assert cohorts.selection["feature"] == "age_band"
    assert {cohort.id for cohort in cohorts.cohorts} == {"age_band:18-30", "age_band:60+"}
