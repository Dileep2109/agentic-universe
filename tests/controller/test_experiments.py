"""Synthetic experiment designs and confirmatory decisions."""

import sqlite3

import pytest

from controller.experiments import (
    assign_leads,
    choose_prompt_candidate,
    evaluate_design,
    evaluate_persisted,
    planned_arm,
    record_observation,
    review_experiments,
    review_prompt_suggestions,
)
from controller.settings import SIM_DAY_ZERO, DecisionThresholds, ExperimentPolicy, RunSettings
from controller.stats import required_n_per_arm, required_samples
from controller.store import Store


def _settings(**experiment) -> RunSettings:
    policy = ExperimentPolicy().overridden(**experiment) if experiment else ExperimentPolicy()
    return RunSettings(
        thresholds=DecisionThresholds(alpha=0.05, pickup_min_dials=800, engagement_min_connected=400, min_abs_change=0.02),
        experiments=policy,
    )


def _workflow(primary: str = "pickup_rate", *, welcome: tuple[str, ...] = ("W1", "W2"), voice: tuple[str, ...] = ("V1", "V2", "V4")) -> dict:
    guard = {"engagement_rate_max_drop": 0.02} if primary == "pickup_rate" else {"pickup_rate_max_drop": 0.02}
    return {
        "workflow_id": "nw_renewal",
        "tenant_id": "northwind_life",
        "status": "active",
        "current_version": 1,
        "objective": {"primary": primary, "guardrail": guard},
        "options": {
            "welcome_message": list(welcome),
            "voice": list(voice),
            "caller_id": ["D1", "D2"],
            "prompt_version": ["P1", "P3"],
        },
        "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"], "max_shift": 0.5}},
    }


def _config() -> dict:
    return {
        "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1}, "caller_id": {"D1": 1}}},
        "prompt_version": "P1",
        "cohorts": [],
        "schedule": {},
    }


def _seed(store: Store, *, dials: int, connected: int, engaged: int, primary: str = "pickup_rate", actor: str = "business_default", welcome: tuple[str, ...] = ("W1", "W2"), voice: tuple[str, ...] = ("V1", "V2", "V4")) -> None:
    store.save_workflow(_workflow(primary, welcome=welcome, voice=voice))
    store.save_config_snapshot("nw_renewal", 1, actor, None, _config(), "fetch")
    store.conn.execute(
        """INSERT INTO call_agg (
               workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
               campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
               voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
               concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
           ) VALUES (
               'nw_renewal', 'history', 'c1', 1, 'default', '2026-12-20', 9, 0,
               'hi', '1', '18-30', 'mid', 'W1', 'V1', 'D1', 'P1',
               ?, ?, ?, 0, 0, 0, 0, 0, 0
           )""",
        (dials, connected, engaged),
    )
    store._rebuild_group_totals()


def _design(**overrides) -> dict:
    design = {
        "primary_metric": "engagement_rate",
        "guardrail_metric": "pickup_rate",
        "guardrail_max_drop": 0.02,
        "minimum_detectable_effect": 0.03,
        "alpha": 0.05,
        "required_control": 800,
        "required_treatment": 800,
        "sample_unit": "connected",
        "base_version": 1,
        "control_value": "W1",
        "treatment_value": "W2",
        "dimension": "welcome_message",
        "executable": False,
    }
    design.update(overrides)
    return design


def _ledger(design: dict, control: dict, treatment: dict, *, variant_for: dict | None = None, version_for: dict | None = None) -> dict:
    """One call per lead. Counts match the reported dials, connected calls, and engaged calls."""
    calls = []
    for arm, counts, value in (
        ("control", control, design["control_value"]),
        ("treatment", treatment, design["treatment_value"]),
    ):
        dials = int(counts["dials"])
        connected = int(counts["connected"])
        engaged = int(counts["engaged"])
        for index in range(dials):
            calls.append({
                "lead_id": f"{arm}-{index}",
                "assigned_arm": arm,
                "config_version": (version_for or {}).get(arm, design["base_version"]),
                "variant": (variant_for or {}).get(arm, value),
                "connected": 1 if index < connected else 0,
                "engaged": 1 if index < engaged else 0,
            })
    return {"calls": calls}


def _look(design, control, treatment, **kwargs):
    exposure = kwargs.pop("exposure", _ledger(design, control, treatment))
    return evaluate_design(design, control, treatment, exposure=exposure, thresholds=kwargs.pop("thresholds", _settings()), **kwargs)


def test_equal_allocation_matches_the_existing_sample_size_formula():
    sized = required_samples(0.54, 0.03, alpha=0.05, power=0.80, treatment_share=0.5)
    classic = required_n_per_arm(0.54, 0.57, alpha=0.05, power=0.80)
    assert sized is not None and classic is not None
    assert sized["control"] == classic
    assert sized["treatment"] == classic
    assert sized["control"] > 1000


def test_unequal_allocation_asks_for_more_control_samples():
    sized = required_samples(0.50, 0.03, alpha=0.05, power=0.80, treatment_share=0.2)
    assert sized is not None
    assert sized["control"] > sized["treatment"]
    assert required_samples(0.0, 0.03) is None
    assert required_samples(0.99, 0.03) is None


def _stored_experiment(store: Store, **overrides) -> dict:
    experiment = {
        "experiment_id": "exp_plan",
        "workflow_id": "nw_renewal",
        "campaign_id": None,
        "cohort_id": "default",
        "dimension": "welcome_message",
        "hypothesis": "Changing welcome_message from W1 to W2 may raise engagement_rate.",
        "primary_metric": "engagement_rate",
        "guardrail_metric": "pickup_rate",
        "guardrail_max_drop": 0.02,
        "control_value": "W1",
        "treatment_value": "W2",
        "baseline_rate": 0.5,
        "minimum_detectable_effect": 0.03,
        "alpha": 0.05,
        "power": 0.8,
        "allocation_control": 0.5,
        "allocation_treatment": 0.5,
        "required_control": 800,
        "required_treatment": 800,
        "sample_unit": "connected",
        "status": "designed",
        "executable": False,
        "exposure_verified": False,
        "causal": False,
        "base_version": 1,
        "proposed_config": {"where": {}, "applied": False, "dimension": "welcome_message"},
        "execution_limit": "The simulator cannot enforce lead-level assignment.",
        "latest_decision": None,
    }
    experiment.update(overrides)
    assert store.save_experiment(experiment) is True
    stored = store.get_experiment(experiment["experiment_id"])
    assert stored is not None
    return stored


def test_historical_calls_do_not_verify_unique_lead_population(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=40000, connected=20000, engaged=10000, primary="engagement_rate")
    plan = review_experiments(store, _settings(), credits_remaining=40)
    assert plan["created"] == []
    decision = plan["decisions"][0]
    assert decision["eligible"] is False
    population = decision["evidence"]["welcome_message"]["population"]
    assert population["available_historical_calls"] == 20000
    assert population["baseline_rate"] == pytest.approx(0.5)
    assert population["estimated_unique_leads"] is None
    assert population["verified_unique_leads"] is None
    assert population["population_sufficiency"] == "unverified"
    assert population["required_experiment_sample"]["control"] > 1000
    assert population["rejection"] == "unique-lead population is unverified"
    assert decision["evidence"]["welcome_message"]["selected_candidate"] == "W2"
    assert "only workflow option" in decision["evidence"]["welcome_message"]["selection_reason"]
    assert "powered" not in decision["reason"].lower()
    assert store.proposal_count() == 0
    store.close()


def test_small_population_is_rejected_with_the_real_floor(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=320, connected=100, engaged=40, primary="pickup_rate")
    plan = review_experiments(store, _settings(), credits_remaining=40)
    decision = plan["decisions"][0]
    assert decision["eligible"] is False
    population = decision["evidence"]["welcome_message"]["population"]
    assert population["available"] == 320
    assert population["rejection"] == "below the configured sample floor"
    assert "Minimum" not in decision["reason"] or population["available"] == 320
    assert store.experiments_for("nw_renewal") == []
    store.close()


def test_powered_sample_is_not_shrunk_to_fit(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=2000, connected=1000, engaged=500, primary="pickup_rate")
    plan = review_experiments(store, _settings(), credits_remaining=10)
    decision = plan["decisions"][0]
    assert decision["eligible"] is False
    population = decision["evidence"]["welcome_message"]["population"]
    assert population["available"] == 2000
    assert population["required_control"] + population["required_treatment"] > 2000
    assert population["available_historical_calls"] == 2000
    assert population["verified_unique_leads"] is None
    assert population["population_sufficiency"] == "unverified"
    assert population["rejection"] == "historical calls are fewer than the required independent sample"
    store.close()


def test_invalid_option_and_prompt_response_are_rejected():
    from controller.experiments import _variant_candidates

    rejected = _variant_candidates(
        {"allocations": {"default": {"welcome_message": {"W9": 1}}}},
        "welcome_message",
        ["W1", "W2"],
    )
    assert rejected[0]["accepted"] is False
    assert "not in the workflow options" in rejected[0]["reason"]

    reviewed = review_prompt_suggestions(
        "P1",
        ["P1", "P3"],
        {"suggestions": [{"prompt_version": "P9", "summary": "persuasive"}, {"prompt_version": "P3", "summary": "strict"}]},
        credits_remaining=10,
    )
    assert [item["accepted"] for item in reviewed] == [False, True]
    chosen = choose_prompt_candidate(reviewed)
    assert chosen["proposed_value"] == "P3"
    assert chosen["selection_policy"] == "single_alternative_contrast"
    assert "not a winner" in chosen["selection_reason"]
    several = review_prompt_suggestions(
        "P1",
        ["P1", "P3", "P4"],
        {"suggestions": [{"prompt_version": "P4", "summary": "first"}, {"prompt_version": "P3", "summary": "second"}]},
        credits_remaining=10,
    )
    assert choose_prompt_candidate(several) is None

    empty = review_prompt_suggestions("P1", ["P1"], {"suggestions": []}, credits_remaining=10)
    assert empty[0]["accepted"] is False
    broken = review_prompt_suggestions("P1", ["P1"], {"suggestions": [{"summary": "no version"}]}, credits_remaining=10)
    assert broken[0]["accepted"] is False
    poor = review_prompt_suggestions("P1", ["P1"], None, credits_remaining=2)
    assert "credits_remaining is below" in poor[0]["reason"]
    withheld = review_prompt_suggestions("P1", ["P1"], {"suggestions": [{"prompt_version": "P3"}]}, credits_remaining=None)
    assert withheld[0]["accepted"] is False


def test_assignment_is_stable_across_restarts_and_excludes_missing_features(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=40000, connected=20000, engaged=10000, primary="engagement_rate")
    experiment_id = _stored_experiment(store)["experiment_id"]
    leads = [
        {"lead_id": f"lead-{i}", "features": {"lang": "hi", "city_tier": "1", "age_band": "18-30", "value_band": "mid"}}
        for i in range(30)
    ]
    leads.append({"lead_id": "lead-missing", "features": {"lang": "", "city_tier": "1", "age_band": "18-30", "value_band": "mid"}})
    first = assign_leads(store, experiment_id, leads)
    second = assign_leads(store, experiment_id, leads)
    assert [(row["lead_id"], row["arm"]) for row in first] == [(row["lead_id"], row["arm"]) for row in second]
    assert len(first) == len(leads)
    missing = next(row for row in first if row["lead_id"] == "lead-missing")
    assert missing["arm"] == "excluded"
    assert "missing feature" in missing["reason"]
    arms = {row["lead_id"]: row["arm"] for row in first if row["arm"] in ("control", "treatment")}
    assert arms["lead-0"] == planned_arm(experiment_id, "lead-0", 0.5)
    store.close()
    reopened = Store.open(tmp_path, "exp")
    restored = {row["lead_id"]: row["arm"] for row in reopened.assignment_rows(experiment_id)}
    assert restored == {row["lead_id"]: row["arm"] for row in first}
    reopened.close()


def test_a_lead_is_not_planned_onto_two_experiments(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=40000, connected=20000, engaged=10000, primary="engagement_rate")
    first = _stored_experiment(store)
    first_id = first["experiment_id"]
    second = dict(first)
    second["experiment_id"] = "exp_other"
    second["treatment_value"] = "W3"
    assert store.save_experiment(second) is True
    lead = {"lead_id": "lead-1", "features": {"lang": "hi", "city_tier": "1", "age_band": "18-30", "value_band": "mid"}}
    assign_leads(store, first_id, [lead])
    other = assign_leads(store, "exp_other", [lead])
    assert other[0]["arm"] == "excluded"
    assert first_id in other[0]["reason"]
    blocked = review_experiments(store, _settings(), credits_remaining=40)
    assert blocked["created"] == []
    assert blocked["decisions"][-1]["blocking_conditions"] == ["conflicting open experiment"]
    store.close()


def test_metric_denominators_stay_separate(tmp_path):
    store = Store.open(tmp_path, "exp")
    block = record_observation(
        store, "exp1", "control",
        {"dials": 100, "connected": 40, "engaged": 10, "leads_uploaded": 50, "leads_concluded": 8},
    )
    record_observation(
        store, "exp1", "control",
        {"dials": 100, "connected": 40, "engaged": 10, "leads_uploaded": 50, "leads_concluded": 8},
    )
    assert block["pickup_rate"] == 0.4
    assert block["engagement_rate"] == 0.25
    assert block["business_outcome"] == 0.16
    rows = store.observation_rows("exp1")
    assert len(rows) == 1
    assert rows[0]["dials"] == 100
    hour = record_observation(store, "exp1", "treatment", {"dials": 10, "connected": 4, "engaged": 1})
    assert store.observation_rows("exp1")[0]["exposure_verified"] == 0
    assert hour["business_outcome"] is None
    store.close()


def test_several_alternatives_do_not_become_a_treatment(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(
        store,
        dials=40000,
        connected=20000,
        engaged=10000,
        primary="engagement_rate",
        welcome=("W1", "W2", "W3"),
        voice=("V1", "V2", "V4"),
    )
    plan = review_experiments(store, _settings(), credits_remaining=40)
    assert plan["created"] == []
    decision = plan["decisions"][0]
    assert decision["eligible"] is False
    assert "No defensible candidate" in decision["reason"]
    assert "Name order is not evidence" in decision["reason"]
    assert decision["evidence"]["welcome_message"]["selected_candidate"] is None
    assert "Name order is not a selection rule" in decision["evidence"]["welcome_message"]["selection_reason"]
    store.close()


def test_a_boolean_is_not_exposure_and_repeated_calls_are_not_a_sample():
    design = _design()
    control = {"dials": 2000, "connected": 800, "engaged": 400}
    treatment = {"dials": 2000, "connected": 800, "engaged": 560}
    flagged = evaluate_design(design, control, treatment, exposure={"exposure_verified": True}, thresholds=_settings())
    assert flagged["decision"] == "insufficient_data"
    assert flagged["causal"] is False
    assert flagged["evidence"]["exposure_verified"] is False
    assert flagged["confirmatory"] is False

    ledger = _ledger(design, control, treatment)
    ledger["calls"].append(dict(ledger["calls"][-1]))
    repeated = evaluate_design(design, control, treatment, exposure=ledger, thresholds=_settings())
    assert repeated["decision"] == "inconclusive"
    assert repeated["causal"] is False
    assert repeated["evidence"]["calls"]["treatment"] > repeated["evidence"]["unique_leads"]["treatment"]
    assert "primary" not in repeated["evidence"]

    mismatched = _ledger(design, control, treatment, variant_for={"treatment": "W9"})
    bad_variant = evaluate_design(design, control, treatment, exposure=mismatched, thresholds=_settings())
    assert bad_variant["decision"] == "insufficient_data"
    assert bad_variant["causal"] is False
    assert bad_variant["confirmatory"] is False
    assert "W9" in bad_variant["reason"]

    wrong_version = _ledger(design, control, treatment, version_for={"control": 2})
    bad_version = evaluate_design(design, control, treatment, exposure=wrong_version, thresholds=_settings())
    assert bad_version["decision"] == "insufficient_data"
    assert "version" in bad_version["reason"]
    assert bad_version["causal"] is False
    assert bad_version["confirmatory"] is False
    assert repeated["confirmatory"] is False


def test_treatment_improvement_regression_null_and_negligible_effect():
    win = _look(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 560},
    )
    assert win["decision"] == "treatment_wins"
    assert win["causal"] is False
    assert win["confirmatory"] is True
    assert win["evidence"]["executable"] is False
    assert win["evidence"]["simulator_enforced_assignment"] is False
    assert win["evidence"]["exposure_verified"] is False
    assert win["evidence"]["ledger_consistent"] is True
    assert "do not prove randomization" in win["reason"]
    assert win["evidence"]["primary"]["absolute_lift"] == pytest.approx(0.2)
    assert win["evidence"]["guardrail"]["status"] != "confirmed"
    assert win["evidence"]["unique_leads"]["control"] == 2000

    loss = _look(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 240},
    )
    assert loss["decision"] == "control_wins"
    assert loss["causal"] is False

    same = _look(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 400},
    )
    assert same["decision"] == "no_significant_difference"

    tiny_design = _design(minimum_detectable_effect=0.03, required_control=100000, required_treatment=100000)
    tiny_control = {"dials": 200000, "connected": 100000, "engaged": 50000}
    tiny_treatment = {"dials": 200000, "connected": 100000, "engaged": 50500}
    tiny = _look(tiny_design, tiny_control, tiny_treatment)
    assert tiny["evidence"]["primary"]["p_value"] < 0.05
    assert abs(tiny["evidence"]["primary"]["absolute_lift"]) < 0.03
    assert tiny["decision"] == "no_significant_difference"


def test_reported_call_counts_cannot_inflate_the_sample():
    design = _design(required_control=800, required_treatment=800)
    control = {"dials": 2000, "connected": 800, "engaged": 400}
    treatment = {"dials": 2000, "connected": 800, "engaged": 560}
    inflated = dict(treatment)
    inflated["connected"] = 8000
    result = evaluate_design(design, control, inflated, exposure=_ledger(design, control, treatment), thresholds=_settings())
    assert result["decision"] == "insufficient_data"
    assert result["causal"] is False
    assert result["confirmatory"] is False
    assert "not used as the sample size" in result["reason"]
    assert result["evidence"]["actual_treatment_samples"] == 800


def test_unequal_arm_sizes_use_each_arm_requirement():
    short = _look(
        _design(required_control=2000, required_treatment=500),
        {"dials": 4000, "connected": 1500, "engaged": 750},
        {"dials": 1000, "connected": 500, "engaged": 350},
    )
    assert short["decision"] == "running"
    assert short["confirmatory"] is False
    assert short["evidence"]["actual_control_samples"] == 1500
    assert short["evidence"]["actual_treatment_samples"] == 500

    ready = _look(
        _design(required_control=2000, required_treatment=500),
        {"dials": 5000, "connected": 2000, "engaged": 1000},
        {"dials": 1200, "connected": 500, "engaged": 350},
    )
    assert ready["decision"] == "treatment_wins"
    assert ready["evidence"]["actual_control_samples"] == 2000
    assert ready["evidence"]["actual_treatment_samples"] == 500
    assert ready["causal"] is False


def test_guardrail_blocks_a_primary_improvement_and_exposure_is_required():
    blocked = _look(
        _design(required_control=600, required_treatment=600),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 680, "engaged": 420},
    )
    assert blocked["decision"] == "guardrail_breached"
    assert blocked["causal"] is False
    assert blocked["confirmatory"] is True
    hidden = _look(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 560},
        exposure=None,
    )
    assert hidden["decision"] == "insufficient_data"
    assert hidden["causal"] is False


def test_interim_looks_do_not_declare_a_winner_and_family_alpha_is_applied():
    early = _look(
        _design(required_control=5000, required_treatment=5000),
        {"dials": 2000, "connected": 1000, "engaged": 400},
        {"dials": 2000, "connected": 1000, "engaged": 700},
    )
    assert early["decision"] == "running"
    assert early["confirmatory"] is False
    assert "primary" not in early["evidence"]

    modest_design = _design(required_control=500, required_treatment=500, minimum_detectable_effect=0.0)
    modest_control = {"dials": 1000, "connected": 500, "engaged": 200}
    modest_treatment = {"dials": 1000, "connected": 500, "engaged": 240}
    modest = _look(modest_design, modest_control, modest_treatment, adjusted_alpha=0.05)
    family = _look(modest_design, modest_control, modest_treatment, adjusted_alpha=0.05 / 20)
    assert modest["evidence"]["primary"]["p_value"] < 0.05
    assert family["evidence"]["adjusted_alpha"] == pytest.approx(0.0025)
    assert family["decision"] == "no_significant_difference"
    assert modest["decision"] == "treatment_wins"


def test_one_confirmatory_evaluation_is_persisted(tmp_path):
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=40000, connected=20000, engaged=10000, primary="engagement_rate")
    design = _stored_experiment(store)
    experiment_id = design["experiment_id"]
    early_control = {"dials": 1600, "connected": 400, "engaged": 200}
    early_treatment = {"dials": 1600, "connected": 400, "engaged": 300}
    interim = evaluate_persisted(
        store, experiment_id, early_control, early_treatment,
        exposure=_ledger(design, early_control, early_treatment),
        thresholds=_settings(),
    )
    assert interim["decision"] == "running"
    assert interim["confirmatory"] is False
    full_control = {"dials": 1600, "connected": 800, "engaged": 400}
    full_treatment = {"dials": 1600, "connected": 800, "engaged": 560}
    first = evaluate_persisted(
        store, experiment_id, full_control, full_treatment,
        exposure=_ledger(design, full_control, full_treatment),
        thresholds=_settings(),
    )
    assert first["confirmatory"] is True
    assert first["causal"] is False
    assert first["decision"] == "treatment_wins"
    second = evaluate_persisted(
        store, experiment_id, full_treatment, full_control,
        exposure=_ledger(design, full_treatment, full_control),
        thresholds=_settings(),
    )
    assert second["repeated_look"] is True
    assert second["decision"] == first["decision"]
    rows = store.evaluation_rows(experiment_id)
    assert sum(1 for row in rows if row["confirmatory"]) == 1
    assert sum(1 for row in rows if not row["confirmatory"]) == 1
    assert store.get_experiment(experiment_id)["executable"] is False
    store.close()


def test_null_confirmatory_looks_stay_near_the_false_positive_rate():
    import numpy as np

    rng = np.random.default_rng(7)
    hits = 0
    trials = 300
    design = _design(minimum_detectable_effect=0.0)
    for _ in range(trials):
        control_s = int(rng.binomial(800, 0.5))
        treatment_s = int(rng.binomial(800, 0.5))
        control = {"dials": 1600, "connected": 800, "engaged": control_s}
        treatment = {"dials": 1600, "connected": 800, "engaged": treatment_s}
        result = _look(design, control, treatment)
        if result["decision"] in ("treatment_wins", "control_wins"):
            hits += 1
    rate = hits / trials
    assert 0.02 <= rate <= 0.09


def test_config_change_and_supervisor_edit_cancel_the_design(tmp_path):
    changed = evaluate_design(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 560},
        config_changed=True,
        thresholds=_settings(),
    )
    assert changed["decision"] == "cancelled"
    assert changed["causal"] is False
    edited = evaluate_design(
        _design(),
        {"dials": 2000, "connected": 800, "engaged": 400},
        {"dials": 2000, "connected": 800, "engaged": 560},
        supervisor_edit=True,
        thresholds=_settings(),
    )
    assert edited["decision"] == "cancelled"
    assert "supervisor" in edited["reason"].lower()

    settings = _settings()
    store = Store.open(tmp_path, "exp")
    _seed(store, dials=40000, connected=20000, engaged=10000, primary="engagement_rate")
    experiment_id = _stored_experiment(store)["experiment_id"]
    store.save_config_snapshot("nw_renewal", 2, "supervisor", "2027-01-04T10:00:00+05:30", _config(), "fetch")
    review_experiments(store, settings, credits_remaining=40)
    assert store.get_experiment(experiment_id)["status"] == "cancelled"
    transitions = store.conn.execute(
        "SELECT from_status, to_status, reason FROM experiment_transitions WHERE experiment_id = ? ORDER BY id",
        (experiment_id,),
    ).fetchall()
    assert transitions[-1]["to_status"] == "cancelled"
    assert "Supervisor" in transitions[-1]["reason"]
    assert store.proposal_count() == 0
    store.close()


def test_invalid_evidence_does_not_consume_the_confirmatory_look(tmp_path):
    store = Store.open(tmp_path, "exp")
    design = _stored_experiment(store)
    experiment_id = design["experiment_id"]
    control = {"dials": 1600, "connected": 800, "engaged": 400}
    treatment = {"dials": 1600, "connected": 800, "engaged": 560}
    settings = _settings()

    bad_variant = _ledger(design, control, treatment, variant_for={"treatment": "W9"})
    variant_result = evaluate_persisted(store, experiment_id, control, treatment, exposure=bad_variant, thresholds=settings)
    assert variant_result["decision"] == "insufficient_data"
    assert variant_result["confirmatory"] is False
    assert store.confirmatory_evaluation(experiment_id) is None

    wrong_version = _ledger(design, control, treatment, version_for={"control": 9})
    version_result = evaluate_persisted(store, experiment_id, control, treatment, exposure=wrong_version, thresholds=settings)
    assert "version" in version_result["reason"]
    assert version_result["confirmatory"] is False
    assert store.confirmatory_evaluation(experiment_id) is None

    missing = _ledger(design, control, treatment)
    missing["calls"][0]["lead_id"] = ""
    missing_result = evaluate_persisted(store, experiment_id, control, treatment, exposure=missing, thresholds=settings)
    assert "lead id" in missing_result["reason"]
    assert missing_result["confirmatory"] is False
    assert store.confirmatory_evaluation(experiment_id) is None

    repeated_calls = []
    for index in range(1600):
        repeated_calls.append({
            "lead_id": f"control-{index}",
            "assigned_arm": "control",
            "config_version": 1,
            "variant": "W1",
            "connected": 1 if index < 800 else 0,
            "engaged": 1 if index < 400 else 0,
        })
    for index in range(400):
        for _copy in range(2):
            repeated_calls.append({
                "lead_id": f"treatment-{index}",
                "assigned_arm": "treatment",
                "config_version": 1,
                "variant": "W2",
                "connected": 1,
                "engaged": 1,
            })
    repeated = evaluate_persisted(
        store, experiment_id, control, {"dials": 800, "connected": 800, "engaged": 800},
        exposure={"calls": repeated_calls},
        thresholds=settings,
    )
    assert repeated["decision"] == "inconclusive"
    assert repeated["confirmatory"] is False
    assert repeated["evidence"]["calls"]["treatment"] == 800
    assert repeated["evidence"]["unique_leads"]["treatment"] == 400
    assert "primary" not in repeated["evidence"]
    assert store.confirmatory_evaluation(experiment_id) is None

    flagged = dict(_ledger(design, control, treatment))
    flagged["randomized"] = True
    flagged["causal"] = True
    first = evaluate_persisted(store, experiment_id, control, treatment, exposure=flagged, thresholds=settings)
    assert first["decision"] == "treatment_wins"
    assert first["confirmatory"] is True
    assert first["causal"] is False
    assert first["evidence"]["simulator_enforced_assignment"] is False
    assert first["evidence"]["exposure_verified"] is False
    assert store.get_experiment(experiment_id)["causal"] is False
    assert store.get_experiment(experiment_id)["executable"] is False

    replacement = evaluate_persisted(
        store, experiment_id, treatment, control,
        exposure=_ledger(design, treatment, control),
        thresholds=settings,
    )
    assert replacement["repeated_look"] is True
    assert replacement["decision"] == "treatment_wins"
    assert sum(1 for row in store.evaluation_rows(experiment_id) if row["confirmatory"]) == 1
    store.close()


def test_business_outcome_needs_verified_lead_conclusions():
    design = _design(primary_metric="business_outcome", sample_unit="leads_uploaded", required_control=400, required_treatment=400)
    control = {"dials": 1600, "connected": 800, "engaged": 400, "leads_uploaded": 800, "leads_concluded": 0}
    treatment = {"dials": 1600, "connected": 800, "engaged": 560, "leads_uploaded": 800, "leads_concluded": 200}
    result = _look(design, control, treatment)
    assert result["decision"] == "inconclusive"
    assert result["causal"] is False
    assert result["confirmatory"] is False
    assert "primary" not in result["evidence"]
    assert "verified lead-conclusion" in result["reason"]
    assert result["evidence"]["control"]["engagement_rate"] == pytest.approx(0.5)
    assert result["evidence"]["control"]["pickup_rate"] == pytest.approx(0.5)
    assert result["evidence"]["control"]["business_outcome"] is None


def test_phase1_database_gains_experiment_tables_without_losing_dials(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE call_agg (
            workflow_id TEXT, config_version INTEGER, cohort_id TEXT, call_date TEXT, hour INTEGER,
            campaign_day_index INTEGER, lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT,
            welcome_message TEXT, voice TEXT, caller_id TEXT, prompt_version TEXT,
            dials INTEGER, connected INTEGER, engaged INTEGER, halluc_flags INTEGER,
            concluded_interested INTEGER, concluded_not_interested INTEGER,
            no_answer INTEGER, voicemail INTEGER, short_hangup INTEGER,
            PRIMARY KEY (
                workflow_id, config_version, cohort_id, call_date, hour, campaign_day_index,
                lang, city_tier, age_band, value_band, welcome_message, voice, caller_id, prompt_version
            )
        )"""
    )
    conn.execute(
        """INSERT INTO call_agg VALUES
           ('nw', 1, 'default', ?, 9, 0, 'hi', '1', '18-30', 'low', 'W1', 'V1', 'D1', 'P1',
            10, 4, 1, 0, 1, 0, 6, 0, 3)""",
        (SIM_DAY_ZERO,),
    )
    conn.commit()
    conn.close()
    store = Store(path, "old")
    assert store.call_totals() == {"dials": 10, "connected": 4, "engaged": 1}
    assert "confirmatory" in [row[1] for row in store.conn.execute("PRAGMA table_info(experiment_evaluations)")]
    assert "selected" in [row[1] for row in store.conn.execute("PRAGMA table_info(experiment_candidates)")]
    assert store._table_exists("experiments")
    assert store._table_exists("experiment_assignments")
    store.close()
    again = Store(path, "old")
    assert again.call_totals() == {"dials": 10, "connected": 4, "engaged": 1}
    again.close()


def test_existing_experiment_rows_survive_the_new_columns(tmp_path):
    path = tmp_path / "mid.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE experiment_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            causal INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE experiment_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT,
            workflow_id TEXT NOT NULL,
            source TEXT NOT NULL,
            dimension TEXT NOT NULL,
            current_value TEXT,
            proposed_value TEXT,
            accepted INTEGER NOT NULL,
            reason TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """INSERT INTO experiment_evaluations
           (experiment_id, decision, reason, evidence_json, causal, created_at)
           VALUES ('exp_old', 'running', 'interim kept', '{"decision":"running"}', 0, 't')"""
    )
    conn.execute(
        """INSERT INTO experiment_candidates
           (workflow_id, source, dimension, accepted, reason, raw_json, created_at)
           VALUES ('nw', 'workflow_options', 'welcome_message', 1, 'candidate kept', '{}', 't')"""
    )
    conn.commit()
    conn.close()
    store = Store(path, "mid")
    evaluation = store.conn.execute("SELECT decision, reason, confirmatory FROM experiment_evaluations").fetchone()
    assert evaluation["decision"] == "running"
    assert evaluation["reason"] == "interim kept"
    assert evaluation["confirmatory"] == 0
    candidate = store.conn.execute("SELECT reason, selected FROM experiment_candidates").fetchone()
    assert candidate["reason"] == "candidate kept"
    assert candidate["selected"] == 0
    store.close()
