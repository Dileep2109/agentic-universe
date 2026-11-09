"""Experiment design, planned allocation, and confirmatory evaluation.

The simulator samples welcome message, voice, and caller ID independently on
each dial. It has no API that pins a lead to an arm, and changing an
allocation requires approval or auto-apply. This module therefore stores a
design and a planned assignment. It does not call ``/approvals``,
``PUT /workflows/{id}/config``, or ``POST /workflows/{id}/apply``. A planned
arm is not treatment exposure. ``executable`` stays false.

A treatment is selected only by ``single_alternative_contrast``: the current
value is the unique modal allocation, and exactly one other workflow option
exists. Several alternatives are not ranked by name, by list order, or by an
LLM. When that contrast does not exist, no experiment is designed.

Calls are not unique leads. Pickup and engagement denominators count dials
and connected calls. The simulator can dial the same lead again, so those
calls are dependent and the call count is not the effective sample size.
Historical aggregates have no lead id. They can estimate a baseline rate.
They cannot show that the unique-lead population meets the required sample.

A descriptive comparison requires a validated ledger: one call per lead, a
lead id on every call, the dialled variant and configuration version matching
the design, and reported counts matching that ledger. Invalid ledgers,
repeated leads, and business-outcome claims without verified conclusions do
not consume the confirmatory look.

``causal`` stays false. A caller-supplied arm and variant are not proof of
randomization, and the documented API does not enforce lead-level assignment.
``executable`` and ``simulator_enforced_assignment`` stay false.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .analytics import counts_for_features, rate_block
from .cohorts import COMPLETE_FEATURE_DENOMINATOR, Cohort, CohortSet
from .monitor import assess_guardrail, minimum_n
from .settings import SIMULATOR_COHORT_LIMIT, ExperimentPolicy, RunSettings
from .stats import compare_rates, required_samples
from .store import Store

log = logging.getLogger(__name__)

OPEN_STATUSES = ("designed", "running")
CANDIDATE_POLICY = "single_alternative_contrast"
CAUSAL_LIMIT = (
    "This comparison is descriptive. A caller-supplied arm and variant do not prove "
    "randomization, and the documented simulator API does not enforce lead-level assignment."
)
SAMPLE_INDEPENDENCE = (
    "Dials and connected calls are not independent trials when the same lead is "
    "called more than once. The effective sample size is the number of unique leads "
    "with one contributing call. A larger call count is not used as n."
)
EXECUTION_LIMIT = (
    "Phase 3 does not apply configuration. The simulator also samples each dial "
    "independently, so a planned lead arm is not exposure and is not a causal result."
)
SAMPLE_UNIT = {
    "pickup_rate": "dials",
    "engagement_rate": "connected",
    "business_outcome": "leads_uploaded",
}


def review_experiments(
    store: Store,
    settings: RunSettings | None = None,
    *,
    credits_remaining: float | None = None,
) -> dict:
    """Design at most one non-executable experiment per workflow.

    Prompt suggestions are not requested here. That call spends credits and
    its order is not a ranking. Use ``review_prompt_suggestions`` with a
    response the caller already fetched.
    """
    settings = settings or RunSettings()
    cancelled = _cancel_stale_designs(store)
    decisions = list(cancelled)
    created: list[str] = []
    for workflow in store.workflow_documents():
        decision = _review_workflow(store, workflow, settings, credits_remaining)
        decisions.append(decision)
        if decision.get("experiment_id") and decision.get("created"):
            created.append(decision["experiment_id"])
    summary = {
        "designed": len(created),
        "ineligible": sum(1 for item in decisions if item.get("eligible") is False),
        "cancelled": sum(1 for item in decisions if item.get("decision") == "cancelled"),
        "credits_remaining": credits_remaining,
    }
    log.info("experiment review %s", summary)
    return {"decisions": decisions, "created": created, "summary": summary}


def evaluate_design(
    design: dict,
    control: dict,
    treatment: dict,
    *,
    exposure: dict | None = None,
    assignments: list[dict] | None = None,
    config_changed: bool = False,
    supervisor_edit: bool = False,
    adjusted_alpha: float | None = None,
    thresholds: RunSettings | None = None,
) -> dict:
    """One statistical look. Caller-supplied fields cannot make it causal.

    The confirmatory flag is set only after the ledger has passed validation
    and both arms meet the predeclared independent-sample size. An invalid
    variant, version, lead id, count mismatch, or repeated lead does not
    spend that look. ``causal`` is always false in this phase.
    """
    settings = thresholds or RunSettings()
    alpha = design["alpha"] if adjusted_alpha is None else adjusted_alpha
    required_control = int(design["required_control"])
    required_treatment = int(design["required_treatment"])
    evidence: dict[str, Any] = {
        "reported_control": _arm_metrics(control),
        "reported_treatment": _arm_metrics(treatment),
        "required_control": required_control,
        "required_treatment": required_treatment,
        "sample_unit": design["sample_unit"],
        "minimum_detectable_effect": design["minimum_detectable_effect"],
        "adjusted_alpha": alpha,
        "exposure_verified": False,
        "ledger_consistent": False,
        "executable": False,
        "simulator_enforced_assignment": False,
        "assignment_provenance": "not_independently_verified",
        "causal_reason": CAUSAL_LIMIT,
        "config_changed": config_changed,
        "supervisor_edit": supervisor_edit,
        "sample_independence": SAMPLE_INDEPENDENCE,
        "evaluation_policy": (
            "The confirmatory look is stored once, and only after the ledger is valid "
            "and both arms meet the predeclared unique-lead sample size. Invalid evidence "
            "and repeated calls do not spend it. The result is descriptive, not causal."
        ),
    }
    if supervisor_edit or config_changed:
        reason = (
            "A supervisor edit is authoritative and cancels the design."
            if supervisor_edit
            else "The configuration changed during the experiment, so the arms are no longer the design."
        )
        return _decision("cancelled", reason, evidence, causal=False, confirmatory=False)
    ledger = _exposure_ledger(design, exposure)
    evidence["exposure"] = ledger["summary"]
    if ledger["error"]:
        return _decision("insufficient_data", ledger["error"], evidence, causal=False, confirmatory=False)
    if ledger["repeated"]:
        evidence["unique_leads"] = ledger["unique_leads"]
        evidence["calls"] = ledger["calls_n"]
        evidence["sample_size_basis"] = "repeated calls are not independent observations"
        return _decision(
            "inconclusive",
            "The same lead was called more than once. Those calls are not independent, so neither the raw call count nor a silent per-lead collapse is used as the sample size.",
            evidence,
            causal=False,
            confirmatory=False,
        )
    derived_control = ledger["control"]
    derived_treatment = ledger["treatment"]
    evidence["control"] = _arm_metrics(derived_control)
    evidence["treatment"] = _arm_metrics(derived_treatment)
    evidence["unique_leads"] = ledger["unique_leads"]
    evidence["calls"] = ledger["calls_n"]
    unit = design["sample_unit"]
    evidence["actual_control_samples"] = int(derived_control.get(unit) or 0)
    evidence["actual_treatment_samples"] = int(derived_treatment.get(unit) or 0)
    if not _counts_match(control, derived_control) or not _counts_match(treatment, derived_treatment):
        evidence["reported_control_samples"] = int(control.get(unit) or 0)
        evidence["reported_treatment_samples"] = int(treatment.get(unit) or 0)
        return _decision(
            "insufficient_data",
            "Reported counts do not match the call ledger. The larger number is not used as the sample size.",
            evidence,
            causal=False,
            confirmatory=False,
        )
    anchored = _assignment_provenance(design, ledger["calls"], assignments)
    evidence["assignment_provenance"] = anchored["provenance"]
    if anchored["error"]:
        return _decision("insufficient_data", anchored["error"], evidence, causal=False, confirmatory=False)
    evidence["ledger_consistent"] = True
    if design.get("primary_metric") == "business_outcome":
        return _decision(
            "inconclusive",
            "Business-outcome evaluation is unsupported. The call ledger has no verified lead-conclusion outcomes, and missing conclusions are not treated as zeros.",
            evidence,
            causal=False,
            confirmatory=False,
        )
    if evidence["actual_control_samples"] < required_control or evidence["actual_treatment_samples"] < required_treatment:
        return _decision(
            "running" if evidence["actual_control_samples"] or evidence["actual_treatment_samples"] else "insufficient_data",
            "At least one arm is below the predeclared independent sample size, so no winner is calculated.",
            evidence,
            causal=False,
            confirmatory=False,
        )
    guard = _guardrail_result(design, derived_control, derived_treatment, settings)
    evidence["guardrail"] = guard
    if guard["status"] == "insufficient":
        return _decision(
            "inconclusive",
            "The guardrail sample is below its floor, so a primary improvement cannot be accepted.",
            evidence,
            causal=False,
            confirmatory=False,
        )
    if guard["status"] == "confirmed":
        return _decision(
            "guardrail_breached",
            "Descriptive comparison: the treatment misses the guardrail allowance. " + CAUSAL_LIMIT,
            evidence,
            causal=False,
            confirmatory=True,
        )
    primary = _primary_result(design, derived_control, derived_treatment, alpha, settings)
    evidence["primary"] = primary
    if primary["status"] == "treatment_higher":
        return _decision(
            "treatment_wins",
            "Descriptive comparison: treatment is higher on the primary KPI by at least the predeclared effect, and the guardrail is intact. " + CAUSAL_LIMIT,
            evidence,
            causal=False,
            confirmatory=True,
        )
    if primary["status"] == "control_higher":
        return _decision(
            "control_wins",
            "Descriptive comparison: control is higher on the primary KPI by at least the predeclared effect, and the guardrail is intact. " + CAUSAL_LIMIT,
            evidence,
            causal=False,
            confirmatory=True,
        )
    if primary["status"] == "below_practical_effect":
        return _decision(
            "no_significant_difference",
            "Descriptive comparison: the difference is smaller than the predeclared minimum effect. " + CAUSAL_LIMIT,
            evidence,
            causal=False,
            confirmatory=True,
        )
    return _decision(
        "no_significant_difference",
        "Descriptive comparison: the primary comparison does not clear the adjusted significance level. " + CAUSAL_LIMIT,
        evidence,
        causal=False,
        confirmatory=True,
    )


def evaluate_persisted(
    store: Store,
    experiment_id: str,
    control: dict,
    treatment: dict,
    *,
    exposure: dict | None = None,
    config_changed: bool = False,
    supervisor_edit: bool = False,
    adjusted_alpha: float | None = None,
    thresholds: RunSettings | None = None,
) -> dict:
    """Persist at most one confirmatory evaluation for this experiment.

    Interim looks are stored with ``confirmatory`` false and can be repeated.
    After the predeclared sample size has produced a confirmatory row, later
    calls return that row and do not recompute a p-value.
    """
    existing = store.confirmatory_evaluation(experiment_id)
    if existing is not None:
        existing["repeated_look"] = True
        return existing
    design = store.get_experiment(experiment_id)
    if design is None:
        raise KeyError(experiment_id)
    result = evaluate_design(
        design,
        control,
        treatment,
        exposure=exposure,
        assignments=store.assignment_rows(experiment_id),
        config_changed=config_changed,
        supervisor_edit=supervisor_edit,
        adjusted_alpha=adjusted_alpha,
        thresholds=thresholds,
    )
    store.save_evaluation(experiment_id, result)
    result["persisted"] = True
    result["repeated_look"] = False
    return result


def assign_leads(store: Store, experiment_id: str, leads: list[dict]) -> list[dict]:
    """Plan a stable arm for each lead. Missing-feature leads stay excluded.

    Existing rows are left untouched, so a restart cannot move a lead.
    """
    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        raise KeyError(experiment_id)
    planned = []
    for lead in leads:
        lead_id = str(lead["lead_id"])
        features = lead.get("features") or {}
        if _missing_features(features):
            planned.append({
                "lead_id": lead_id,
                "arm": "excluded",
                "reason": "missing feature; " + COMPLETE_FEATURE_DENOMINATOR,
            })
            continue
        conflict = store.conflicting_assignment(
            experiment["workflow_id"], experiment["dimension"], lead_id, experiment_id
        )
        if conflict:
            planned.append({
                "lead_id": lead_id,
                "arm": "excluded",
                "reason": f"lead is already planned on experiment {conflict}",
            })
            continue
        cohort_id = _lead_cohort(experiment, features)
        if cohort_id != experiment["cohort_id"]:
            planned.append({
                "lead_id": lead_id,
                "arm": "excluded",
                "reason": f"lead is in {cohort_id}, outside target cohort {experiment['cohort_id']}",
            })
            continue
        arm = planned_arm(experiment_id, lead_id, float(experiment["allocation_treatment"]))
        planned.append({
            "lead_id": lead_id,
            "arm": arm,
            "reason": "stable hash of experiment id and lead id; this is a plan, not exposure",
        })
    store.save_assignments(experiment_id, planned)
    return store.assignment_rows(experiment_id)


def planned_arm(experiment_id: str, lead_id: str, treatment_share: float) -> str:
    """Deterministic arm. The same pair of ids always returns the same arm."""
    digest = hashlib.sha256(f"{experiment_id}|{lead_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(2**64)
    return "treatment" if bucket < treatment_share else "control"


def review_prompt_suggestions(
    current_prompt: str,
    options: list[str],
    response: dict | None,
    *,
    credits_remaining: float | None,
    credit_cost: int = 3,
) -> list[dict]:
    """Validate an already-fetched suggest-fixes payload.

    List order is ignored. A suggestion is not a winner. This function does
    not call the simulator.
    """
    if credits_remaining is None:
        return [_rejected("prompt_version", current_prompt, None, "suggest-fixes was not called because credits_remaining was not supplied", response)]
    if credits_remaining < credit_cost:
        return [_rejected(
            "prompt_version",
            current_prompt,
            None,
            "suggest-fixes was not called because credits_remaining is below the documented cost of 3",
            {"credits_remaining": credits_remaining, "cost": credit_cost},
        )]
    if not isinstance(response, dict):
        return [_rejected("prompt_version", current_prompt, None, "suggest-fixes response is not an object", response)]
    suggestions = response.get("suggestions")
    if suggestions is None:
        suggestions = response.get("fixes")
    if not isinstance(suggestions, list) or not suggestions:
        return [_rejected("prompt_version", current_prompt, None, "suggest-fixes response has no suggestion list", response)]
    reviewed = []
    allowed = set(options)
    for item in suggestions:
        if not isinstance(item, dict):
            reviewed.append(_rejected("prompt_version", current_prompt, None, "suggestion is not an object", item))
            continue
        proposed = item.get("prompt_version")
        if not proposed:
            reviewed.append(_rejected("prompt_version", current_prompt, None, "suggestion has no prompt_version", item))
            continue
        if proposed not in allowed:
            reviewed.append(_rejected("prompt_version", current_prompt, str(proposed), "prompt_version is not in the workflow options", item))
            continue
        if proposed == current_prompt:
            reviewed.append(_rejected("prompt_version", current_prompt, str(proposed), "suggestion matches the current prompt", item))
            continue
        reviewed.append({
            "source": "llm_suggest_fixes",
            "dimension": "prompt_version",
            "current_value": current_prompt,
            "proposed_value": str(proposed),
            "accepted": True,
            "reason": (
                "prompt is one of the workflow options and differs from the current prompt. "
                "Acceptance is not a rank and is not a winner. The summary text was not used."
            ),
            "raw": item,
        })
    if not reviewed:
        reviewed.append(_rejected("prompt_version", current_prompt, None, "suggest-fixes response had an empty list", response))
    return reviewed


def choose_prompt_candidate(reviewed: list[dict]) -> dict | None:
    """Select a prompt only when exactly one suggestion passed validation.

    Response order and name order are not used. Several accepted prompts mean
    no experiment can be designed from this response.
    """
    accepted = [item for item in reviewed if item.get("accepted")]
    if len(accepted) != 1:
        return None
    chosen = dict(accepted[0])
    chosen["selected"] = True
    chosen["selection_policy"] = CANDIDATE_POLICY
    chosen["selection_reason"] = (
        "Selected because it is the only suggestion that is a workflow option and differs from the current prompt. "
        "Response order and prompt-summary text were not used. This selection is not a winner."
    )
    return chosen


def record_observation(store: Store, experiment_id: str, arm: str, counts: dict) -> dict:
    """Store absolute arm counts. Writing the same counts again does not add them.

    The row stays unverified. Aggregate counts do not prove which variant was dialled.
    """
    block = rate_block(
        dials=int(counts.get("dials") or 0),
        connected=int(counts.get("connected") or 0),
        engaged=int(counts.get("engaged") or 0),
        leads_uploaded=counts.get("leads_uploaded"),
        leads_concluded=counts.get("leads_concluded"),
    )
    store.save_observation(experiment_id, arm, block)
    return block


def _review_workflow(
    store: Store,
    workflow: dict,
    settings: RunSettings,
    credits_remaining: float | None,
) -> dict:
    workflow_id = workflow["workflow_id"]
    open_rows = store.experiments_for(workflow_id, OPEN_STATUSES)
    if open_rows:
        blocked = {
            "eligible": False,
            "workflow_id": workflow_id,
            "decision": "ineligible",
            "reason": "An experiment is already open for this workflow.",
            "evidence": {"open_experiment_id": open_rows[0]["experiment_id"]},
            "blocking_conditions": ["conflicting open experiment"],
            "causal": False,
        }
        store.save_experiment_decision({
            "workflow_id": workflow_id,
            "kind": "eligibility",
            "eligible": False,
            "decision": "ineligible",
            "reason": blocked["reason"],
            "evidence": blocked["evidence"],
            "causal": False,
        })
        return blocked
    eligibility = assess_eligibility(store, workflow, settings, credits_remaining)
    store.save_experiment_decision({
        "workflow_id": workflow_id,
        "kind": "eligibility",
        "eligible": eligibility["eligible"],
        "decision": "eligible" if eligibility["eligible"] else "ineligible",
        "reason": eligibility["reason"],
        "evidence": eligibility["evidence"],
        "causal": False,
    })
    if not eligibility["eligible"]:
        log.info("experiment ineligible workflow=%s reason=%s", workflow_id, eligibility["reason"])
        return eligibility
    design = eligibility["design"]
    candidates = design.pop("candidates")
    inserted = store.save_experiment(design)
    store.save_candidates(workflow_id, candidates, design["experiment_id"] if inserted else None)
    store.save_experiment_decision({
        "workflow_id": workflow_id,
        "experiment_id": design["experiment_id"],
        "kind": "design",
        "eligible": True,
        "decision": "designed",
        "reason": design["execution_limit"],
        "evidence": {
            "experiment_id": design["experiment_id"],
            "executable": False,
            "hypothesis": design["hypothesis"],
            "selection_policy": CANDIDATE_POLICY,
            "selection_reason": design["proposed_config"].get("selection_reason"),
            "required_control": design["required_control"],
            "required_treatment": design["required_treatment"],
            "sample_unit": design["sample_unit"],
        },
        "causal": False,
    })
    eligibility["experiment_id"] = design["experiment_id"]
    eligibility["created"] = inserted
    eligibility["decision"] = "designed"
    return eligibility


def assess_eligibility(
    store: Store,
    workflow: dict,
    settings: RunSettings,
    credits_remaining: float | None,
) -> dict:
    workflow_id = workflow["workflow_id"]
    blocking: list[str] = []
    evidence: dict[str, Any] = {
        "credits_remaining": credits_remaining,
        "credits_note": (
            "Welcome and voice designs do not spend credits. suggest-fixes costs 3 "
            "and is not called by this review."
        ),
    }
    status = str(workflow.get("status") or "active")
    if status not in ("active", ""):
        blocking.append(f"workflow status is {status}")
    objective = workflow.get("objective") or {}
    primary = objective.get("primary")
    guard = _guardrail_spec(objective)
    if primary not in SAMPLE_UNIT:
        blocking.append("primary KPI is missing or is not a known rate")
    if guard is None:
        blocking.append("guardrail is missing")
    pending = store.pending_approval_count(workflow_id)
    evidence["pending_approvals"] = pending
    if pending:
        blocking.append("an approval is still pending")
    config_row = store.latest_config(workflow_id)
    if config_row is None:
        blocking.append("current configuration has not been fetched")
    options = (workflow.get("options") or {})
    if not options:
        blocking.append("workflow options were not in the fetched document")
    if blocking or guard is None or primary not in SAMPLE_UNIT or config_row is None:
        return _ineligible(workflow_id, blocking or ["eligibility inputs are incomplete"], evidence)

    cohort_set = _stored_cohorts(store, workflow_id)
    populations = counts_for_features(store, workflow_id, cohort_set, kind="history")
    if not populations:
        populations = counts_for_features(store, workflow_id, cohort_set, kind=None)
        evidence["population_period"] = "all"
    else:
        evidence["population_period"] = "history"
    evidence["populations"] = {
        key: {"dials": value["dials"], "connected": value["connected"], "engaged": value["engaged"]}
        for key, value in populations.items()
    }
    policy = settings.experiments
    floor = minimum_n(primary, settings.thresholds)
    unit = SAMPLE_UNIT[primary]
    wheres = {row["cohort_id"]: row["where"] for row in store.cohort_rows(workflow_id)}
    evidence["sample_independence"] = {
        "aggregate_count": "calls",
        "independent_trial": "one call per lead",
        "independent_trials_known": False,
        "note": SAMPLE_INDEPENDENCE
        + " History stores call aggregates without lead ids, so this available count is calls, not the effective sample size.",
    }
    best: dict | None = None
    saw_defensible = False
    population_block: str | None = None
    for dimension in policy.dimensions:
        reviewed = _variant_candidates(config_row["config"], dimension, options.get(dimension) or [])
        chosen, selection_reason = _select_treatment(reviewed)
        marked = _annotate_selection(reviewed, chosen)
        population = _best_population(populations, primary, floor, policy, settings, wheres)
        evidence[dimension] = {
            "candidates": marked,
            "selection_policy": CANDIDATE_POLICY,
            "selection_reason": selection_reason,
            "selected_candidate": None if chosen is None else chosen["proposed_value"],
            "population": None if population is None else {
                "cohort_id": population["cohort_id"],
                "available_historical_calls": population["available_historical_calls"],
                "available_calls": population["available"],
                "available": population["available"],
                "estimated_unique_leads": population["estimated_unique_leads"],
                "verified_unique_leads": population["verified_unique_leads"],
                "population_sufficiency": population["population_sufficiency"],
                "count_kind": "historical_calls",
                "baseline_rate": population["baseline_rate"],
                "required_control": None if population["sample"] is None else population["sample"]["control"],
                "required_treatment": None if population["sample"] is None else population["sample"]["treatment"],
                "required_experiment_sample": None if population["sample"] is None else {
                    "control": population["sample"]["control"],
                    "treatment": population["sample"]["treatment"],
                },
                "rejection": population["rejection"],
            },
        }
        if chosen is None:
            continue
        saw_defensible = True
        verified = population is not None and population.get("population_sufficiency") == "verified" and not population.get("rejection")
        if not verified:
            if population_block is None and population is not None:
                population_block = population.get("rejection") or "unique-lead population is unverified"
            continue
        if best is None:
            best = {
                "dimension": dimension,
                "candidate": chosen,
                "reviewed": marked,
                "population": population,
            }
    if best is None:
        if not saw_defensible:
            blocking.append(
                "No defensible candidate. A treatment is selected only when it is the single "
                "alternative to the current unique modal value. Name order is not evidence."
            )
        else:
            blocking.append(population_block or "no welcome or voice candidate has a powered population")
        return _ineligible(workflow_id, blocking, evidence)

    population = best["population"]
    candidate = best["candidate"]
    sample = population["sample"]
    hypothesis = (
        f"Changing {best['dimension']} from {candidate['current_value']} to {candidate['proposed_value']} "
        f"for cohort {population['cohort_id']} on {workflow_id} may raise {primary} by at least "
        f"{policy.minimum_detectable_effect} absolute without letting {guard['metric']} fall by more than "
        f"{guard['max_drop']}."
    )
    experiment_id = _experiment_id(
        workflow_id, population["cohort_id"], best["dimension"],
        str(candidate["current_value"]), str(candidate["proposed_value"]), int(config_row["version"]),
    )
    governance = workflow.get("governance") or {}
    auto = (governance.get("auto_apply") or {})
    design = {
        "experiment_id": experiment_id,
        "workflow_id": workflow_id,
        "campaign_id": None,
        "cohort_id": population["cohort_id"],
        "dimension": best["dimension"],
        "hypothesis": hypothesis,
        "primary_metric": primary,
        "guardrail_metric": guard["metric"],
        "guardrail_max_drop": guard["max_drop"],
        "control_value": candidate["current_value"],
        "treatment_value": candidate["proposed_value"],
        "baseline_rate": population["baseline_rate"],
        "minimum_detectable_effect": policy.minimum_detectable_effect,
        "alpha": settings.thresholds.alpha,
        "power": policy.power,
        "allocation_control": 1.0 - policy.treatment_share,
        "allocation_treatment": policy.treatment_share,
        "required_control": sample["control"],
        "required_treatment": sample["treatment"],
        "sample_unit": unit,
        "status": "designed",
        "executable": False,
        "exposure_verified": False,
        "causal": False,
        "base_version": int(config_row["version"]),
        "proposed_config": {
            "dimension": best["dimension"],
            "cohort_id": population["cohort_id"],
            "where": population.get("where") or {},
            "control_value": candidate["current_value"],
            "treatment_value": candidate["proposed_value"],
            "selection_policy": CANDIDATE_POLICY,
            "selection_reason": candidate["selection_reason"],
            "allocation": {
                str(candidate["current_value"]): 1.0 - policy.treatment_share,
                str(candidate["proposed_value"]): policy.treatment_share,
            },
            "applied": False,
        },
        "execution_limit": EXECUTION_LIMIT,
        "latest_decision": None,
        "where": population.get("where") or {},
        "candidates": best["reviewed"],
    }
    evidence.update({
        "selected_dimension": best["dimension"],
        "cohort_id": population["cohort_id"],
        "available": population["available"],
        "minimum_floor": floor,
        "sample_unit": unit,
        "baseline_rate": population["baseline_rate"],
            "required_samples_per_arm": sample,
            "selection_policy": CANDIDATE_POLICY,
            "selection_reason": candidate["selection_reason"],
            "auto_apply_allowed_dims": list(auto.get("allowed_dims") or []),
        "governance_note": (
            "Whether this dimension may later auto-apply is recorded here. "
            "Phase 3 does not send the proposal."
        ),
    })
    return {
        "eligible": True,
        "workflow_id": workflow_id,
        "decision": "eligible",
        "reason": "Population, guardrail, configuration, and one candidate support a design. The design is not executable.",
        "evidence": evidence,
        "blocking_conditions": [],
        "design": design,
        "causal": False,
    }


def _best_population(
    populations: dict[str, dict],
    primary: str,
    floor: int,
    policy: ExperimentPolicy,
    settings: RunSettings,
    wheres: dict[str, dict],
) -> dict | None:
    unit = SAMPLE_UNIT[primary]
    numerator = {"pickup_rate": "connected", "engagement_rate": "engaged", "business_outcome": "leads_concluded"}[primary]
    ranked = []
    for cohort_id, counts in populations.items():
        if cohort_id == "missing":
            continue
        available = int(counts.get(unit) or 0)
        successes = int(counts.get(numerator) or 0)
        rate = None if available == 0 else successes / available
        sample = None
        rejection = None
        if available < floor:
            rejection = "below the configured sample floor"
        elif rate is None or not 0.0 < rate < 1.0:
            rejection = "baseline rate is outside (0, 1), so the sample-size formula has no finite answer"
        else:
            sample = required_samples(
                rate,
                policy.minimum_detectable_effect,
                alpha=settings.thresholds.alpha,
                power=policy.power,
                treatment_share=policy.treatment_share,
            )
            if sample is None:
                rejection = "the minimum detectable effect leaves the treatment rate outside (0, 1)"
            elif available < sample["control"] + sample["treatment"]:
                rejection = "historical calls are fewer than the required independent sample"
            else:
                rejection = "unique-lead population is unverified"
        ranked.append((available, cohort_id, rate, sample, rejection))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[4] is not None, -item[0], item[1]))
    available, cohort_id, rate, sample, rejection = ranked[0]
    return {
        "cohort_id": cohort_id,
        "available": available,
        "available_historical_calls": available,
        "estimated_unique_leads": None,
        "verified_unique_leads": None,
        "population_sufficiency": "unverified",
        "baseline_rate": rate,
        "sample": sample,
        "rejection": rejection,
        "where": wheres.get(cohort_id) or {},
    }


def _variant_candidates(config: dict, dimension: str, options: list) -> list[dict]:
    current = _modal_option(config, "default", dimension)
    reviewed = []
    if current is None:
        weights = ((config.get("allocations") or {}).get("default") or {}).get(dimension) or {}
        if weights:
            reason = "the current allocation has no unique modal value, so name order is not used to choose a control"
        else:
            reason = "the current config has no weight for this dimension"
        return [_rejected(dimension, None, None, reason, {"options": list(options)})]
    allowed = [str(option) for option in options]
    if current not in allowed:
        return [_rejected(dimension, current, None, "the current value is not in the workflow options", {"options": allowed})]
    alternatives = sorted(option for option in allowed if option != current)
    if not alternatives:
        return [_rejected(dimension, current, None, "the workflow lists no alternative value", {"options": allowed})]
    for proposed in alternatives:
        reviewed.append({
            "source": "workflow_options",
            "dimension": dimension,
            "current_value": current,
            "proposed_value": proposed,
            "accepted": True,
            "reason": (
                "Value is a documented workflow option other than the current modal value. "
                "The list order is not a performance ranking."
            ),
            "raw": {"dimension": dimension, "current": current, "proposed": proposed},
        })
    return reviewed


def _modal_option(config: dict, cohort_id: str, dimension: str) -> str | None:
    allocations = config.get("allocations") or {}
    weights = ((allocations.get(cohort_id) or {}).get(dimension) or (allocations.get("default") or {}).get(dimension) or {})
    if not weights:
        if dimension == "prompt_version" and config.get("prompt_version"):
            return str(config["prompt_version"])
        return None
    ordered = sorted((str(key), float(value)) for key, value in weights.items())
    top = max(weight for _, weight in ordered)
    tied = [name for name, weight in ordered if weight == top]
    if len(tied) != 1:
        return None
    return tied[0]


def _select_treatment(reviewed: list[dict]) -> tuple[dict | None, str]:
    """Apply the single-alternative policy. Name order is not a reason."""
    accepted = [item for item in reviewed if item.get("accepted")]
    if len(accepted) == 1:
        chosen = dict(accepted[0])
        reason = (
            "Selected because this is the only workflow option other than the current unique modal value. "
            "Outcome rates, response order, and name order were not used. This selection is not a winner."
        )
        chosen["selected"] = True
        chosen["selection_policy"] = CANDIDATE_POLICY
        chosen["selection_reason"] = reason
        return chosen, reason
    if not accepted:
        return None, "No candidate passed validation, so no experiment can be designed on this dimension."
    return None, (
        "More than one alternative is allowed, and no documented outcome evidence distinguishes them. "
        "Name order is not a selection rule, so no experiment can be designed on this dimension."
    )


def _annotate_selection(reviewed: list[dict], chosen: dict | None) -> list[dict]:
    selected_value = None if chosen is None else chosen.get("proposed_value")
    marked = []
    for item in reviewed:
        copy = dict(item)
        copy["selected"] = bool(selected_value is not None and copy.get("accepted") and copy.get("proposed_value") == selected_value)
        if copy["selected"] and chosen is not None:
            raw = dict(copy.get("raw") or {})
            raw["validation_reason"] = item.get("reason")
            copy["raw"] = raw
            copy["selection_policy"] = CANDIDATE_POLICY
            copy["selection_reason"] = chosen["selection_reason"]
            copy["reason"] = chosen["selection_reason"]
        marked.append(copy)
    return marked


def _stored_cohorts(store: Store, workflow_id: str) -> CohortSet:
    cohorts = []
    for row in store.cohort_rows(workflow_id):
        where = {key: tuple(values) for key, values in row["where"].items()}
        cohorts.append(Cohort(row["cohort_id"], where, int(row["support_leads"]), int(row["support_dials"]), row["reason"]))
        if len(cohorts) >= SIMULATOR_COHORT_LIMIT:
            break
    return CohortSet(tuple(cohorts))


def _cancel_stale_designs(store: Store) -> list[dict]:
    cancelled = []
    for workflow in store.workflow_documents():
        workflow_id = workflow["workflow_id"]
        current = store.latest_config(workflow_id)
        if current is None:
            continue
        for experiment in store.experiments_for(workflow_id, OPEN_STATUSES):
            if int(current["version"]) == int(experiment["base_version"]):
                continue
            supervisor = current.get("actor") == "supervisor"
            reason = (
                "Supervisor edit is authoritative. The open design is cancelled and is not overwritten."
                if supervisor
                else "Configuration version moved after the design was stored. The design is cancelled."
            )
            store.transition_experiment(experiment["experiment_id"], "cancelled", reason, decision="cancelled")
            store.save_experiment_decision({
                "workflow_id": workflow_id,
                "experiment_id": experiment["experiment_id"],
                "kind": "lifecycle",
                "eligible": False,
                "decision": "cancelled",
                "reason": reason,
                "evidence": {
                    "design_version": experiment["base_version"],
                    "current_version": current["version"],
                    "actor": current.get("actor"),
                },
                "causal": False,
            })
            log.info("cancelled experiment %s: %s", experiment["experiment_id"], reason)
            cancelled.append({
                "eligible": False,
                "workflow_id": workflow_id,
                "experiment_id": experiment["experiment_id"],
                "decision": "cancelled",
                "reason": reason,
                "causal": False,
            })
    return cancelled


def _primary_result(design: dict, control: dict, treatment: dict, alpha: float, settings: RunSettings) -> dict:
    metric = design["primary_metric"]
    left_s, left_n = _metric_counts(treatment, metric)
    right_s, right_n = _metric_counts(control, metric)
    comparison = compare_rates(
        left_s,
        left_n,
        right_s,
        right_n,
        alpha=alpha,
        minimum_n=1,
        min_abs_change=0.0,
        confidence=settings.thresholds.confidence,
    )
    difference = comparison.difference
    mde = float(design["minimum_detectable_effect"])
    if comparison.verdict == "insufficient" or difference is None or comparison.p_value is None:
        status = "insufficient"
    elif comparison.p_value >= alpha:
        status = "not_significant"
    elif abs(difference) < mde:
        status = "below_practical_effect"
    elif difference > 0:
        status = "treatment_higher"
    else:
        status = "control_higher"
    body = comparison.as_dict()
    body["status"] = status
    body["absolute_lift"] = difference
    return body


def _guardrail_result(design: dict, control: dict, treatment: dict, settings: RunSettings) -> dict:
    objective = {"guardrail": {f"{design['guardrail_metric']}_max_drop": design["guardrail_max_drop"]}}
    finding = assess_guardrail(objective, control, treatment, settings.thresholds, subject="treatment_vs_control")
    if finding is None:
        return {"status": "insufficient", "reason": "guardrail metric is not defined"}
    return finding


def _metric_counts(counts: dict, metric: str) -> tuple[int, int]:
    if metric == "pickup_rate":
        return int(counts.get("connected") or 0), int(counts.get("dials") or 0)
    if metric == "engagement_rate":
        return int(counts.get("engaged") or 0), int(counts.get("connected") or 0)
    return int(counts.get("leads_concluded") or 0), int(counts.get("leads_uploaded") or 0)


def _arm_metrics(counts: dict) -> dict:
    uploaded = counts.get("leads_uploaded")
    concluded = counts.get("leads_concluded")
    return rate_block(
        dials=int(counts.get("dials") or 0),
        connected=int(counts.get("connected") or 0),
        engaged=int(counts.get("engaged") or 0),
        leads_uploaded=None if uploaded is None else int(uploaded),
        leads_concluded=None if concluded is None else int(concluded),
    )


def _missing_features(features: dict) -> bool:
    from .settings import FEATURE_VALUES
    return any(not features.get(name) for name in FEATURE_VALUES)


def _lead_cohort(experiment: dict, features: dict) -> str:
    where = (experiment.get("proposed_config") or {}).get("where") or experiment.get("where") or {}
    if not where or experiment["cohort_id"] == "default":
        return "default" if not _missing_features(features) else "missing"
    cohort = Cohort(experiment["cohort_id"], {key: tuple(values) for key, values in where.items()})
    return CohortSet((cohort,)).assign(features)


def _guardrail_spec(objective: dict) -> dict | None:
    guard = (objective or {}).get("guardrail") or {}
    if "pickup_rate_max_drop" in guard:
        return {"metric": "pickup_rate", "max_drop": float(guard["pickup_rate_max_drop"])}
    if "engagement_rate_max_drop" in guard:
        return {"metric": "engagement_rate", "max_drop": float(guard["engagement_rate_max_drop"])}
    return None


def _experiment_id(workflow_id: str, cohort_id: str, dimension: str, control: str, treatment: str, version: int) -> str:
    raw = f"{workflow_id}|{cohort_id}|{dimension}|{control}|{treatment}|v{version}"
    return "exp_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _ineligible(workflow_id: str, blocking: list[str], evidence: dict) -> dict:
    return {
        "eligible": False,
        "workflow_id": workflow_id,
        "decision": "ineligible",
        "reason": blocking[0],
        "evidence": evidence,
        "blocking_conditions": blocking,
        "causal": False,
    }


def _rejected(dimension: str, current: str | None, proposed: str | None, reason: str, raw: Any) -> dict:
    return {
        "source": "llm_suggest_fixes" if dimension == "prompt_version" else "workflow_options",
        "dimension": dimension,
        "current_value": current,
        "proposed_value": proposed,
        "accepted": False,
        "reason": reason,
        "raw": raw,
    }


def _decision(decision: str, reason: str, evidence: dict, *, causal: bool, confirmatory: bool = False) -> dict:
    return {
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "causal": causal,
        "confirmatory": confirmatory,
    }


def _empty_counts() -> dict:
    return {"dials": 0, "connected": 0, "engaged": 0, "leads_uploaded": 0, "leads_concluded": None}


def _assignment_provenance(design: dict, calls: list[dict], assignments: list[dict] | None) -> dict:
    """Compare caller-supplied arms with persisted plans. A match is not causal."""
    if not assignments:
        return {"provenance": "caller_supplied", "error": None}
    planned = {
        str(row["lead_id"]): str(row["arm"])
        for row in assignments
        if row.get("arm") in ("control", "treatment")
    }
    if not planned:
        return {"provenance": "caller_supplied", "error": None}
    experiment_id = design.get("experiment_id")
    share = design.get("allocation_treatment")
    for call in calls:
        lead_id = str(call["lead_id"])
        arm = str(call["assigned_arm"])
        stored = planned.get(lead_id)
        if stored is None:
            return {
                "provenance": "not_independently_verified",
                "error": f"Lead {lead_id} has no persisted assignment.",
            }
        if stored != arm:
            return {
                "provenance": "not_independently_verified",
                "error": f"Lead {lead_id} is persisted as {stored}, not the supplied arm {arm}.",
            }
        if experiment_id is not None and share is not None:
            expected = planned_arm(str(experiment_id), lead_id, float(share))
            if expected != arm:
                return {
                    "provenance": "not_independently_verified",
                    "error": f"Lead {lead_id} does not match the stable planned arm {expected}.",
                }
    return {"provenance": "matches_persisted_plan", "error": None}


def _counts_match(reported: dict, derived: dict) -> bool:
    return all(int(reported.get(key) or 0) == int(derived.get(key) or 0) for key in ("dials", "connected", "engaged"))


def _exposure_ledger(design: dict, exposure: dict | None) -> dict:
    """Turn a call ledger into arm counts, or explain why it is not evidence."""
    blank = {
        "error": None,
        "repeated": False,
        "summary": {"calls": 0},
        "control": _empty_counts(),
        "treatment": _empty_counts(),
        "raw": {"control": _empty_counts(), "treatment": _empty_counts()},
        "unique_leads": {"control": 0, "treatment": 0},
        "calls_n": {"control": 0, "treatment": 0},
        "calls": [],
    }
    if not isinstance(exposure, dict) or not isinstance(exposure.get("calls"), list) or not exposure["calls"]:
        blank["error"] = (
            "Treatment exposure has no call-level provenance. A verification flag is not evidence "
            "that a variant was dialled, and a planned assignment is not exposure."
        )
        if isinstance(exposure, dict) and "exposure_verified" in exposure:
            blank["summary"]["verification_flag_ignored"] = True
        return blank
    calls = exposure["calls"]
    base_version = design.get("base_version")
    control_value = design.get("control_value")
    treatment_value = design.get("treatment_value")
    if base_version is None or control_value is None or treatment_value is None:
        blank["error"] = "The design has no version or variant provenance to check the calls against."
        blank["summary"] = {"calls": len(calls)}
        return blank
    required = ("lead_id", "assigned_arm", "config_version", "variant", "connected", "engaged")
    by_lead: dict[str, list[str]] = {}
    control_counts = _empty_counts()
    treatment_counts = _empty_counts()
    parsed: list[dict] = []
    error = None
    for call in calls:
        if not isinstance(call, dict) or any(key not in call for key in required):
            error = error or "A call is missing lead_id, assigned_arm, config_version, variant, connected, or engaged."
            continue
        arm = call["assigned_arm"]
        if arm not in ("control", "treatment"):
            error = error or "A call has an assigned arm other than control or treatment."
            continue
        try:
            version = int(call["config_version"])
            connected = int(call["connected"])
            engaged = int(call["engaged"])
        except (TypeError, ValueError):
            error = error or "A call has a non-numeric config version or outcome flag."
            continue
        if connected not in (0, 1) or engaged not in (0, 1):
            error = error or "connected and engaged must be 0 or 1 on each call."
            continue
        if engaged and not connected:
            error = error or "A call is marked engaged without being connected."
            continue
        lead_id = str(call["lead_id"]).strip()
        if not lead_id:
            error = error or "A call is missing a lead id."
            continue
        expected = control_value if arm == "control" else treatment_value
        bucket = control_counts if arm == "control" else treatment_counts
        bucket["dials"] += 1
        bucket["connected"] += connected
        bucket["engaged"] += engaged
        if version != int(base_version):
            error = error or f"Call config_version {version} does not match design version {int(base_version)}."
        elif str(call["variant"]) != str(expected):
            error = error or f"Dialled variant {call['variant']} does not match the {arm} value {expected}."
        by_lead.setdefault(lead_id, []).append(str(arm))
        parsed.append({"lead_id": lead_id, "assigned_arm": str(arm)})
    if any(len(set(arms)) > 1 for arms in by_lead.values()):
        error = "A lead is assigned to both control and treatment."
    repeated = any(len(arms) > 1 for arms in by_lead.values())
    unique = {"control": 0, "treatment": 0}
    for arms in by_lead.values():
        if len(set(arms)) == 1:
            unique[arms[0]] += 1
    control_counts["leads_uploaded"] = unique["control"]
    treatment_counts["leads_uploaded"] = unique["treatment"]
    summary = {
        "calls": len(calls),
        "design_version": int(base_version),
        "control_value": str(control_value),
        "treatment_value": str(treatment_value),
        "unique_leads": unique,
        "call_counts": {"control": control_counts["dials"], "treatment": treatment_counts["dials"]},
    }
    return {
        "error": error,
        "repeated": repeated and error is None,
        "summary": summary,
        "control": control_counts,
        "treatment": treatment_counts,
        "raw": {"control": control_counts, "treatment": treatment_counts},
        "unique_leads": unique,
        "calls_n": {"control": control_counts["dials"], "treatment": treatment_counts["dials"]},
        "calls": parsed,
    }
