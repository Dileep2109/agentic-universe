"""Governed configuration changes.

A proposal is stored locally before any write. Approval submission, auto-apply,
and apply each go through the documented client methods. An uncertain response
is reconciled with GET /approvals and configuration history before another
write is considered. This module does not import the simulator.

Caller-id relief is an observed association between options already dialled.
It is not a causal estimate, and it carries no predicted uplift. Cohort
contrasts and historical aggregates do not by themselves create a proposal.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .analytics import call_groups, caller_connection_health, recent_caller_connection
from .client import AmbiguousResponse, SimError
from .impact import assess_impact
from .policy import CHANGE_DIMS, AGENT_DIMS, changed_dimensions, distribution, evaluate_policy, total_variation
from .monitor import assess_change, assess_guardrail
from .settings import DecisionThresholds, RunSettings
from .stats import compare_rates
from .store import Store

OPEN_STATUSES = (
    "drafted",
    "submitting",
    "submitted",
    "pending",
    "approved",
    "applying",
    "uncertain",
    "applied",
)
TERMINAL = ("policy_rejected", "rejected", "expired", "verified", "failed", "lapsed", "rolled_back")


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def proposal_id_for(workflow_id: str, base_version: int, proposed: dict) -> str:
    digest = hashlib.sha256(
        f"{workflow_id}|{base_version}|{_dumps(proposed)}".encode("utf-8")
    ).hexdigest()[:24]
    return f"gov-{digest}"


def _audit(store: Store, action: str, workflow_id: str | None, sim_ts: str | None, detail: dict) -> None:
    store.audit(action, detail, workflow_id=workflow_id, sim_ts=sim_ts)


def _audit_new(
    store: Store,
    action: str,
    workflow_id: str | None,
    sim_ts: str | None,
    detail: dict,
    identity: dict,
) -> None:
    """Record an audit once per workflow and identity. A new identity still records."""
    if isinstance(workflow_id, str) and store.has_audit_detail(action, workflow_id, identity):
        return
    _audit(store, action, workflow_id, sim_ts, detail)


def _audit_supervisor(store: Store, workflow_id: str, sim_ts: str | None, proposal_id: str, version) -> None:
    """One row per supervisor config version. The next version is a new change."""
    try:
        version_n = int(version)
    except (TypeError, ValueError):
        return
    _audit_new(
        store,
        "supervisor_modification_detected",
        workflow_id,
        sim_ts,
        {"proposal_id": proposal_id, "version": version_n},
        {"version": version_n},
    )


def _recent_requests(approvals: list[dict], sim_ts: str | None) -> int | None:
    if sim_ts is None:
        return None
    try:
        now = datetime.fromisoformat(sim_ts)
    except ValueError:
        return None
    cutoff = now - timedelta(hours=24)
    count = 0
    for row in approvals:
        created = row.get("created_at")
        if not isinstance(created, str):
            return None
        try:
            moment = datetime.fromisoformat(created)
        except ValueError:
            return None
        if moment >= cutoff:
            count += 1
    return count


def _pending(approvals: list[dict]) -> int | None:
    count = 0
    for row in approvals:
        status = row.get("status")
        if status is None:
            return None
        if status == "pending":
            count += 1
    return count


def build_proposal(
    workflow: dict,
    current: dict,
    proposed: dict,
    evidence: dict,
    impact: dict,
    *,
    sim_ts: str | None,
    pending_approvals: int | None,
    recent_requests: int | None,
    rationale: str,
) -> dict:
    """Build one proposal, or a rejection record when policy or evidence says no."""
    if not evidence.get("explained"):
        policy = {
            "decision": "rejected",
            "reason_codes": ["unexplained_change"],
            "ready_to_submit": False,
            "current_version": current.get("version") if isinstance(current, dict) else None,
            "expected_version_after_apply": None,
            "policy_evidence": {},
            "route": None,
            "changed_dims": [],
        }
        return {"proposal": None, "policy": policy}
    if impact.get("effect_kind") != "observed" or impact.get("expected_effect") is not None:
        policy = {
            "decision": "rejected",
            "reason_codes": ["impact_not_explainable"],
            "ready_to_submit": False,
            "current_version": current.get("version") if isinstance(current, dict) else None,
            "expected_version_after_apply": None,
            "policy_evidence": {},
            "route": None,
            "changed_dims": [],
        }
        return {"proposal": None, "policy": policy}
    policy = evaluate_policy(
        workflow,
        current,
        proposed,
        sim_ts=sim_ts,
        pending_approvals=pending_approvals,
        recent_requests=recent_requests,
        evidence_version=evidence.get("config_version"),
        validation_run_id=evidence.get("validation_run_id"),
    )
    if policy["decision"] == "rejected":
        return {"proposal": None, "policy": policy}
    version = int(current["version"])
    pid = proposal_id_for(workflow["workflow_id"], version, proposed)
    score = float(impact["impact_score"])
    if not 0.0 <= score <= 1.0:
        policy = {**policy, "decision": "rejected", "reason_codes": ["impact_score_out_of_range"], "ready_to_submit": False}
        return {"proposal": None, "policy": policy}
    affected = evidence.get("affected_population")
    if not isinstance(affected, int) or affected < 0:
        policy = {**policy, "decision": "rejected", "reason_codes": ["affected_population_unknown"], "ready_to_submit": False}
        return {"proposal": None, "policy": policy}
    if not rationale.strip():
        policy = {**policy, "decision": "rejected", "reason_codes": ["empty_rationale"], "ready_to_submit": False}
        return {"proposal": None, "policy": policy}
    dims = policy["changed_dims"]
    diff = {
        "changed_dims": dims,
        "shift": policy["shift"],
        "max_shift": policy["max_shift"],
    }
    summary = "change " + ", ".join(dims)
    dry_run = {
        "method": "observed_rate_difference",
        "observed_effect": impact["observed_effect"],
        "expected_effect": None,
        "effect_kind": "observed",
        "note": impact["note"],
    }
    proposal = {
        "proposal_id": pid,
        "workflow_id": workflow["workflow_id"],
        "base_version": version,
        "proposed_config": proposed,
        "change_summary": summary,
        "diff": diff,
        "affected_leads": affected,
        "dry_run": dry_run,
        "impact_score": score,
        "impact_calculation": impact,
        "rationale": rationale,
        "evidence": {**evidence, "evidence_ids": evidence.get("evidence_ids") or [pid]},
        "approval_id": None,
        "status": "drafted",
        "outcome": None,
        "route": policy["route"],
        "expires_at": None,
        "policy": policy,
    }
    return {"proposal": proposal, "policy": policy}


def _approval_body(proposal: dict) -> dict:
    ttl = proposal["policy"].get("expires_in_hours")
    body = {
        "proposal_id": proposal["proposal_id"],
        "workflow_id": proposal["workflow_id"],
        "base_version": proposal["base_version"],
        "proposed_config": proposal["proposed_config"],
        "change_summary": proposal["change_summary"],
        "diff": proposal["diff"],
        "affected_leads": int(proposal["affected_leads"]),
        "dry_run": proposal["dry_run"],
        "impact_score": float(proposal["impact_score"]),
        "rationale": proposal["rationale"],
        "evidence": {
            "evidence_ids": proposal["evidence"].get("evidence_ids"),
            "effect_kind": proposal["impact_calculation"].get("effect_kind"),
            "validation_run_id": proposal["evidence"].get("validation_run_id"),
        },
    }
    if isinstance(ttl, (int, float)):
        body["expires_in_hours"] = float(ttl)
    return body


def _auto_body(proposal: dict) -> dict:
    return {
        "proposal_id": proposal["proposal_id"],
        "base_version": proposal["base_version"],
        "proposed_config": proposal["proposed_config"],
        "rationale": proposal["rationale"],
        "evidence": {
            "evidence_ids": proposal["evidence"].get("evidence_ids"),
            "effect_kind": proposal["impact_calculation"].get("effect_kind"),
        },
    }


def _save(store: Store, proposal: dict) -> None:
    existing = store.get_proposal(proposal["proposal_id"])
    if existing is not None:
        proposal = {**proposal, "created_at": existing.get("created_at")}
        if existing.get("verification") and proposal.get("verification") is None:
            proposal["verification"] = existing["verification"]
    store.save_proposal(proposal)


def _mark(store: Store, proposal: dict, status: str, outcome: str | None = None, **extra: Any) -> dict:
    updated = {**proposal, "status": status, "outcome": outcome if outcome is not None else proposal.get("outcome")}
    updated.update(extra)
    _save(store, updated)
    return updated


WEIGHT_TOLERANCE = 1e-6


def _weights_match(left: dict, right: dict) -> bool:
    options = set(left) | set(right)
    for option in options:
        if abs(float(left.get(option, 0) or 0) - float(right.get(option, 0) or 0)) > WEIGHT_TOLERANCE:
            return False
    return True


def _dimension_matches(proposed: dict, actual: dict, dimension: str) -> bool:
    if dimension == "prompt_version":
        return proposed.get("prompt_version") == actual.get("prompt_version")
    if dimension == "cohorts":
        return proposed.get("cohorts") == actual.get("cohorts")
    if dimension == "schedule":
        return proposed.get("schedule") == actual.get("schedule")
    if dimension in AGENT_DIMS:
        keys = set((proposed.get("allocations") or {})) | set((actual.get("allocations") or {}))
        return all(
            _weights_match(distribution(proposed, key, dimension), distribution(actual, key, dimension))
            for key in keys
        )
    return False


def verify_configuration(
    proposed: dict,
    actual: dict | None,
    changed_dims: list[str],
    *,
    version,
    expected_version: int,
    history: dict | None,
) -> dict:
    """Match every changed dimension, and reject a live config that differs elsewhere.

    Allocation weights use an absolute tolerance. Schedule, prompt, and cohorts
    are compared exactly. Total variation is not used, because it ignores those.
    """
    mismatches: list[str] = []
    if not isinstance(actual, dict):
        mismatches = list(CHANGE_DIMS)
    else:
        for dimension in CHANGE_DIMS:
            if not _dimension_matches(proposed, actual, dimension):
                mismatches.append(dimension)
    changed_ok = all(dimension not in mismatches for dimension in changed_dims)
    unexpected = [dimension for dimension in mismatches if dimension not in changed_dims]
    version_ok = version == expected_version
    history_ok = (
        isinstance(history, dict)
        and history.get("version") == expected_version
        and history.get("proposal_id") is not None
    )
    ok = changed_ok and not unexpected and version_ok and history_ok and not mismatches
    return {
        "ok": ok,
        "version": version,
        "expected_version": expected_version,
        "history": history,
        "values_match": not mismatches,
        "changed_ok": changed_ok,
        "unexpected_dims": unexpected,
        "mismatched_dims": mismatches,
        "version_match": version_ok,
        "history_match": history_ok,
        "causal": False,
    }


def _verify(client, proposal: dict, expected_version: int) -> dict:
    fresh = client.get_config(proposal["workflow_id"])
    history = client.find_config_by_proposal(proposal["workflow_id"], proposal["proposal_id"])
    version = fresh.get("version") if isinstance(fresh, dict) else None
    config = fresh.get("config") if isinstance(fresh, dict) else None
    changed = list((proposal.get("diff") or {}).get("changed_dims") or [])
    result = verify_configuration(
        proposal["proposed_config"],
        config,
        changed,
        version=version,
        expected_version=expected_version,
        history=history,
    )
    result["actor"] = fresh.get("actor") if isinstance(fresh, dict) else None
    if isinstance(history, dict) and history.get("proposal_id") not in (None, proposal["proposal_id"]):
        result["ok"] = False
        result["history_match"] = False
        result["unexpected_dims"] = list(result["unexpected_dims"]) + ["proposal_id"]
    elif isinstance(history, dict) and history.get("proposal_id") != proposal["proposal_id"]:
        result["ok"] = False
        result["history_match"] = False
    return result


def _submit(store: Store, client, proposal: dict, sim_ts: str | None) -> dict:
    kind = "auto_apply" if proposal["route"] == "auto_apply" else "approval_submit"
    if store.attempts_for(proposal["proposal_id"], kind):
        reconciled = _reconcile_one(store, client, proposal, sim_ts)
        if reconciled.get("outcome") != "not_on_server":
            return reconciled
        proposal = reconciled
    proposal = _mark(store, proposal, "submitting")
    store.record_attempt(proposal["proposal_id"], proposal["workflow_id"], kind, "started", {"base_version": proposal["base_version"]})
    _audit(
        store,
        "auto_apply_attempted" if kind == "auto_apply" else "approval_submitted",
        proposal["workflow_id"],
        sim_ts,
        {"proposal_id": proposal["proposal_id"], "base_version": proposal["base_version"], "outcome": "started"},
    )
    try:
        if kind == "auto_apply":
            result = client.auto_apply(proposal["workflow_id"], _auto_body(proposal))
        else:
            result = client.request_approval(_approval_body(proposal))
    except AmbiguousResponse as exc:
        proposal = _mark(store, proposal, "uncertain", outcome="response_lost")
        store.record_attempt(proposal["proposal_id"], proposal["workflow_id"], kind, "uncertain", {"error": exc.code})
        _audit(store, "write_uncertain", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "error": exc.code})
        return proposal
    except SimError as exc:
        if exc.status == 429 or exc.code in ("rate_limit", "too_many_requests"):
            proposal = _mark(store, proposal, "drafted", outcome="rate_limited")
            _audit(store, "approval_rate_limited", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "code": exc.code})
            return proposal
        if exc.code == "stale_base_version":
            proposal = _mark(store, proposal, "failed", outcome="stale_base_version")
            _audit(store, "configuration_verification_failed", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "code": exc.code})
            return proposal
        if exc.code in ("auto_apply_not_permitted", "duplicate_proposal_unconfirmed"):
            proposal = _mark(store, proposal, "failed", outcome=exc.code)
            _audit(store, "policy_rejected", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "code": exc.code})
            return proposal
        proposal = _mark(store, proposal, "uncertain", outcome=exc.code)
        store.record_attempt(proposal["proposal_id"], proposal["workflow_id"], kind, "uncertain", {"code": exc.code})
        return proposal
    if not result.ok:
        proposal = _mark(store, proposal, "failed", outcome=result.reason)
        return proposal
    if kind == "auto_apply":
        proposal = _mark(store, proposal, "applied", outcome=result.reason, approval_id=None)
        return _confirm_application(store, client, proposal, sim_ts, result.body)
    body = result.body if isinstance(result.body, dict) else {}
    status = body.get("status") or "pending"
    proposal = _mark(
        store,
        proposal,
        status if status in ("pending", "approved", "rejected", "expired") else "submitted",
        outcome=result.reason,
        approval_id=body.get("approval_id"),
        expires_at=body.get("expires_at"),
    )
    _audit(
        store,
        "approval_submitted",
        proposal["workflow_id"],
        sim_ts,
        {
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal.get("approval_id"),
            "status": proposal["status"],
            "base_version": proposal["base_version"],
            "outcome": result.reason,
        },
    )
    return proposal


def _confirm_application(store: Store, client, proposal: dict, sim_ts: str | None, body: dict) -> dict:
    expected = int(proposal["policy"]["expected_version_after_apply"])
    try:
        verification = _verify(client, proposal, expected)
    except (AmbiguousResponse, SimError) as exc:
        proposal = _mark(store, proposal, "uncertain", outcome="verification_unconfirmed", verification={"ok": False, "error": getattr(exc, "code", "error")})
        _audit(store, "configuration_verification_failed", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "outcome": "uncertain"})
        return proposal
    proposal = _mark(
        store,
        proposal,
        "verified" if verification["ok"] else "uncertain",
        outcome="verified" if verification["ok"] else "verification_mismatch",
        verification=verification,
    )
    action = "configuration_applied" if verification["ok"] else "configuration_verification_failed"
    _audit(
        store,
        action,
        proposal["workflow_id"],
        sim_ts,
        {
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal.get("approval_id"),
            "version": verification.get("version"),
            "expected_version": expected,
            "outcome": proposal["outcome"],
            "evidence_ids": proposal["evidence"].get("evidence_ids"),
        },
    )
    return proposal


def _reconcile_one(store: Store, client, proposal: dict, sim_ts: str | None) -> dict:
    """Adopt server state. Do not send the same write again when it may have landed."""
    if proposal["status"] in TERMINAL:
        return proposal
    try:
        approval = client.find_approval_by_proposal(proposal["proposal_id"], proposal["workflow_id"])
        applied = client.find_config_by_proposal(proposal["workflow_id"], proposal["proposal_id"])
    except AmbiguousResponse:
        proposal = _mark(store, proposal, "uncertain", outcome="reconcile_unconfirmed")
        return proposal
    except SimError as exc:
        if exc.ambiguous:
            proposal = _mark(store, proposal, "uncertain", outcome="reconcile_unconfirmed")
            return proposal
        raise
    if applied is not None:
        proposal = _mark(store, proposal, "applied", outcome="reconciled_from_history", approval_id=(approval or {}).get("approval_id"))
        return _confirm_application(store, client, proposal, sim_ts, applied)
    if approval is None:
        if proposal["status"] in ("submitting", "uncertain", "submitted", "drafted", "applying", "applied"):
            # A successful read found neither an approval nor a config row.
            return _mark(store, proposal, "drafted", outcome="not_on_server")
        return proposal
    status = approval.get("status")
    proposal = _mark(
        store,
        proposal,
        status if isinstance(status, str) else "uncertain",
        outcome="reconciled",
        approval_id=approval.get("approval_id"),
        expires_at=approval.get("expires_at"),
    )
    if status in ("approved", "rejected", "expired", "lapsed", "applied"):
        _audit(
            store,
            f"approval_{status}",
            proposal["workflow_id"],
            sim_ts,
            {"proposal_id": proposal["proposal_id"], "approval_id": approval.get("approval_id"), "outcome": status},
        )
    return proposal


def _apply_approved(store: Store, client, proposal: dict, sim_ts: str | None, current: dict) -> dict:
    if proposal.get("approval_id") is None:
        return _mark(store, proposal, "uncertain", outcome="missing_approval_id")
    if current.get("actor") == "supervisor":
        _audit_supervisor(store, proposal["workflow_id"], sim_ts, proposal["proposal_id"], current.get("version"))
        return _mark(store, proposal, "failed", outcome="supervisor_edit")
    if int(current.get("version", -1)) != int(proposal["base_version"]):
        return _mark(store, proposal, "failed", outcome="configuration_moved")
    if store.attempts_for(proposal["proposal_id"], "apply"):
        reconciled = _reconcile_one(store, client, proposal, sim_ts)
        if reconciled["status"] != "approved":
            return reconciled
        proposal = reconciled
    proposal = _mark(store, proposal, "applying")
    store.record_attempt(proposal["proposal_id"], proposal["workflow_id"], "apply", "started", {"approval_id": proposal["approval_id"]})
    try:
        result = client.apply_approval(proposal["workflow_id"], proposal["approval_id"])
    except AmbiguousResponse as exc:
        proposal = _mark(store, proposal, "uncertain", outcome="response_lost")
        store.record_attempt(proposal["proposal_id"], proposal["workflow_id"], "apply", "uncertain", {"error": exc.code})
        return proposal
    except SimError as exc:
        if exc.code == "approval_lapsed":
            return _mark(store, proposal, "lapsed", outcome=exc.code)
        if exc.code in ("not_approved", "stale_base_version", "not_found"):
            return _mark(store, proposal, "failed", outcome=exc.code)
        proposal = _mark(store, proposal, "uncertain", outcome=exc.code)
        return proposal
    if not result.ok:
        return _mark(store, proposal, "failed", outcome=result.reason)
    proposal = _mark(store, proposal, "applied", outcome=result.reason)
    return _confirm_application(store, client, proposal, sim_ts, result.body if isinstance(result.body, dict) else {})


def _shift_caller(config: dict, source: str, dest: str, delta: float) -> dict | None:
    proposed = copy.deepcopy(config)
    allocations = proposed.setdefault("allocations", {})
    default = allocations.get("default")
    if not isinstance(default, dict) or "caller_id" not in default:
        return None
    weights = dict(default["caller_id"])
    if source not in weights or dest not in weights:
        return None
    if weights[source] <= 0 or delta <= 0:
        return None
    move = min(float(weights[source]), float(delta))
    weights[source] = float(weights[source]) - move
    weights[dest] = float(weights[dest]) + move
    if weights[source] <= 1e-12:
        weights.pop(source)
    default["caller_id"] = weights
    allocations["default"] = default
    proposed["allocations"] = allocations
    if total_variation(config, proposed) <= 0:
        return None
    return proposed


def _caller_counts(row: dict) -> dict:
    dials = int(row["dials"])
    no_answer = int(row.get("no_answer") or 0)
    voicemail = int(row.get("voicemail") or 0)
    unanswered = no_answer + voicemail
    return {
        "dials": dials,
        "connected": int(row["connected"]),
        "engaged": int(row["engaged"]),
        "no_answer": no_answer,
        "voicemail": voicemail,
        "unanswered": unanswered,
        "unanswered_rate": None if dials == 0 else unanswered / dials,
    }


def _decision_key(opportunity: dict) -> tuple:
    evidence = opportunity.get("evidence") or {}
    return (
        bool(opportunity.get("actionable")),
        opportunity.get("reason"),
        evidence.get("pickup_verdict"),
        evidence.get("engagement_verdict"),
        evidence.get("worst_caller_id"),
        evidence.get("best_caller_id"),
    )


def _opportunity_decision_changed(previous: dict | None, opportunity: dict) -> bool:
    """True when this observation is a new decision, not a larger identical sample."""
    if previous is None:
        return True
    return _decision_key(previous) != _decision_key(opportunity)


def detect_opportunities(store: Store, workflow: dict, current: dict, settings: RunSettings) -> list[dict]:
    """Caller-id connection gaps on the current configuration version.

    The health signal is the call event: ``connected`` and the ``no_answer`` /
    ``voicemail`` dispositions. It is not ``GET /health``. History is excluded.
    Older live versions are not pooled in, because a caller can degrade after
    those versions were dialled. The degraded caller does not need the
    engagement sample; the caller that would receive weight does.
    """
    workflow_id = workflow.get("workflow_id")
    if not isinstance(workflow_id, str) or not isinstance(current, dict):
        return []
    thresholds: DecisionThresholds = settings.thresholds
    try:
        version = int(current["version"])
        groups = caller_connection_health(store, workflow_id, kind="events", config_version=version)
    except (TypeError, ValueError):
        return []
    eligible = [row for row in groups if row["key"] and row["dials"] >= thresholds.pickup_min_dials]
    if len(eligible) < 2:
        return []
    ranked = sorted(eligible, key=lambda row: (row["pickup_rate"], row["key"]))
    worst, best = ranked[0], ranked[-1]
    if worst["key"] == best["key"]:
        return []
    comparison = compare_rates(
        worst["connected"],
        worst["dials"],
        best["connected"],
        best["dials"],
        alpha=thresholds.alpha,
        minimum_n=thresholds.pickup_min_dials,
        min_abs_change=thresholds.min_abs_change,
        confidence=thresholds.confidence,
    )
    engagement = compare_rates(
        best["engaged"],
        best["connected"],
        worst["engaged"],
        worst["connected"],
        alpha=thresholds.alpha,
        minimum_n=thresholds.engagement_min_connected,
        min_abs_change=thresholds.min_abs_change,
        confidence=thresholds.confidence,
    )
    evidence = {
        "explained": False,
        "config_version": int(current["version"]),
        "config_snapshot": current.get("config"),
        "worst_caller_id": worst["key"],
        "best_caller_id": best["key"],
        "pickup_verdict": comparison.verdict,
        "engagement_verdict": engagement.verdict,
        "pickup_comparison": comparison.as_dict(),
        "engagement_comparison": engagement.as_dict(),
        "callers": {
            str(worst["key"]): _caller_counts(worst),
            str(best["key"]): _caller_counts(best),
        },
        "causal": False,
        "effect_kind": "observed",
        "evidence_ids": [f"caller:{workflow_id}:{current['version']}:{worst['key']}:{best['key']}"],
        "affected_population": int(worst["dials"] + best["dials"]),
        "sample_n": int(worst["dials"] + best["dials"]),
        "primary_comparison": "pickup_rate",
        "health_signal": {
            "source": "GET /events/chunks/{n}",
            "fields": ["connected", "disposition"],
            "dispositions": ["no_answer", "voicemail"],
            "config_version": version,
        },
    }
    opportunity_id = evidence["evidence_ids"][0]
    if comparison.verdict != "left_lower":
        return [{
            "opportunity_id": opportunity_id,
            "workflow_id": workflow_id,
            "actionable": False,
            "reason": "caller-id pickup gap is not a confirmed difference",
            "evidence": evidence,
            "proposed_config": None,
        }]
    if int(best["connected"]) < thresholds.engagement_min_connected:
        return [{
            "opportunity_id": opportunity_id,
            "workflow_id": workflow_id,
            "actionable": False,
            "reason": "the higher-pickup caller id is below the engagement sample floor",
            "evidence": evidence,
            "proposed_config": None,
        }]
    if engagement.verdict == "left_lower":
        return [{
            "opportunity_id": opportunity_id,
            "workflow_id": workflow_id,
            "actionable": False,
            "reason": "the higher-pickup caller id has lower observed engagement",
            "evidence": evidence,
            "proposed_config": None,
        }]
    auto = (workflow.get("governance") or {}).get("auto_apply") or {}
    cap = auto.get("max_shift") if auto.get("enabled") is True else 0.1
    if not isinstance(cap, (int, float)) or cap <= 0:
        cap = 0.1
    config = current.get("config")
    if not isinstance(config, dict):
        return []
    proposed = _shift_caller(config, str(worst["key"]), str(best["key"]), float(cap))
    if proposed is None:
        return [{
            "opportunity_id": opportunity_id,
            "workflow_id": workflow_id,
            "actionable": False,
            "reason": "no caller-id weight can be moved inside the current allocation",
            "evidence": evidence,
            "proposed_config": None,
        }]
    evidence["explained"] = True
    evidence["explanation"] = (
        "Observed connection rate on one caller id is lower on this configuration version. "
        "The counts are call events, including no_answer and voicemail. The caller that "
        "receives weight meets the engagement floor and is not worse on engagement. "
        "Weight moves by at most the workflow shift limit. This is an association, not a "
        "causal effect, and no future uplift is claimed."
    )
    return [{
        "opportunity_id": opportunity_id,
        "workflow_id": workflow_id,
        "actionable": True,
        "reason": evidence["explanation"],
        "evidence": evidence,
        "proposed_config": proposed,
        "comparison": comparison,
        "worst": worst,
        "best": best,
    }]


def _call_baseline(row: dict | None) -> dict:
    """Pre-change call counts. Business outcome is absent: these rows are not leads."""
    if row is None:
        return {
            "dials": 0,
            "connected": 0,
            "engaged": 0,
            "leads_uploaded": None,
            "leads_concluded": None,
            "business_outcome_available": False,
            "reason": "no calls on the pre-change configuration version",
        }
    return {
        "dials": int(row["dials"]),
        "connected": int(row["connected"]),
        "engaged": int(row["engaged"]),
        "leads_uploaded": None,
        "leads_concluded": None,
        "business_outcome_available": False,
        "reason": "call aggregates do not identify unique concluded leads",
    }


def _default_caller_weights(config: dict) -> dict[str, float] | None:
    allocations = config.get("allocations") or {}
    if not isinstance(allocations, dict) or set(allocations) - {"default"}:
        return None
    weights = (allocations.get("default") or {}).get("caller_id")
    if not isinstance(weights, dict) or not weights:
        return None
    try:
        return {str(key): float(value) for key, value in weights.items()}
    except (TypeError, ValueError):
        return None


def _pair_meets_floor(rows: dict[str, dict], left: str, right: str, floor: int) -> bool:
    return (
        left in rows
        and right in rows
        and int(rows[left]["dials"]) >= floor
        and int(rows[right]["dials"]) >= floor
    )


def _restore_reexposes_degraded_caller(
    store: Store,
    proposal: dict,
    current: dict,
    previous: dict,
    findings: list[dict],
    settings: RunSettings,
) -> dict | None:
    """Evidence that a restore would dial a worse-connecting caller more often.

    The guardrail test is unchanged. This refuses only the restore, and only
    when caller connection rates at the existing pickup floor say so. An
    engagement drop is not refused. Missing caller evidence leaves the restore
    in place.
    """
    engagement_down = any(
        item.get("metric") == "engagement_rate"
        and item.get("status") == "confirmed"
        and item.get("direction") == "down"
        for item in findings
    )
    if engagement_down:
        return None
    pickup_problem = any(
        item.get("metric") == "pickup_rate"
        and item.get("status") == "confirmed"
        and (item.get("role") == "guardrail" or item.get("direction") == "down")
        for item in findings
    )
    if not pickup_problem:
        return None
    live = current.get("config") if isinstance(current, dict) else None
    if not isinstance(live, dict) or changed_dimensions(previous, live) != ["caller_id"]:
        return None
    before = _default_caller_weights(previous)
    after = _default_caller_weights(live)
    if before is None or after is None:
        return None
    deltas = {key: before.get(key, 0.0) - after.get(key, 0.0) for key in set(before) | set(after)}
    gainers = [key for key, delta in deltas.items() if delta > 1e-9]
    losers = [key for key, delta in deltas.items() if delta < -1e-9]
    if not gainers or not losers:
        return None
    gainer = max(gainers, key=lambda key: deltas[key])
    loser = max(losers, key=lambda key: -deltas[key])
    floor = settings.thresholds.pickup_min_dials
    workflow_id = proposal["workflow_id"]
    sample = None
    source = None
    applied = (proposal.get("verification") or {}).get("version")
    if applied is not None:
        rows = {
            str(row["key"]): row
            for row in caller_connection_health(store, workflow_id, kind="events", config_version=int(applied))
        }
        if _pair_meets_floor(rows, gainer, loser, floor):
            sample = rows
            source = "applied_version"
    if sample is None:
        baseline_version = (proposal.get("evidence") or {}).get("config_version")
        if baseline_version is None:
            return None
        recent = recent_caller_connection(store, workflow_id, int(baseline_version), (gainer, loser), floor)
        if recent is None:
            return None
        sample = recent
        source = "recent_baseline_version"
    thresholds = settings.thresholds
    comparison = compare_rates(
        int(sample[gainer]["connected"]),
        int(sample[gainer]["dials"]),
        int(sample[loser]["connected"]),
        int(sample[loser]["dials"]),
        alpha=thresholds.alpha,
        minimum_n=floor,
        min_abs_change=thresholds.min_abs_change,
        confidence=thresholds.confidence,
    )
    if comparison.verdict != "left_lower":
        return None
    return {
        "gainer": gainer,
        "loser": loser,
        "sample_source": source,
        "gainer_pickup": sample[gainer]["pickup_rate"],
        "loser_pickup": sample[loser]["pickup_rate"],
        "gainer_unanswered": sample[gainer]["unanswered"],
        "loser_unanswered": sample[loser]["unanswered"],
        "comparison": comparison.as_dict(),
        "causal": False,
    }


def _monitor_applied(store: Store, client, settings: RunSettings, sim_ts: str | None) -> None:
    """Compare post-change calls with the stored baseline, using the workflow objective.

    Primary degradation uses the workflow's primary KPI. A guardrail breach uses
    the maximum drop named on that workflow. Neither result is causal. Business
    outcome is not scored from call rows.
    """
    thresholds = settings.thresholds
    for proposal in store.proposals_by_status(("verified",)):
        if proposal["evidence"].get("rollback_of"):
            continue
        workflow_id = proposal["workflow_id"]
        version = (proposal.get("verification") or {}).get("version")
        if version is None:
            continue
        try:
            current = client.get_config(workflow_id)
            groups = call_groups(store, workflow_id, "config_version", kind="events")
            documents = {item["workflow_id"]: item for item in client.workflows() if item.get("workflow_id")}
        except (AmbiguousResponse, SimError, ValueError):
            continue
        if current.get("actor") == "supervisor" or int(current.get("version", -1)) != int(version):
            if current.get("actor") == "supervisor":
                _audit_supervisor(store, workflow_id, sim_ts, proposal["proposal_id"], current.get("version"))
            continue
        post = next((row for row in groups if int(row["key"]) == int(version)), None)
        if post is None:
            continue
        baseline = proposal["evidence"].get("baselines")
        if not isinstance(baseline, dict):
            pre_n = proposal["evidence"].get("baseline_n")
            pre_success = proposal["evidence"].get("baseline_successes")
            if not isinstance(pre_n, int) or not isinstance(pre_success, int):
                continue
            baseline = {
                "dials": pre_n,
                "connected": pre_success,
                "engaged": proposal["evidence"].get("baseline_engaged"),
                "leads_uploaded": None,
                "leads_concluded": None,
                "business_outcome_available": False,
            }
        observed = {
            "dials": int(post["dials"]),
            "connected": int(post["connected"]),
            "engaged": int(post["engaged"]),
            "leads_uploaded": None,
            "leads_concluded": None,
        }
        workflow = documents.get(workflow_id) or {}
        objective = workflow.get("objective") or {}
        primary = objective.get("primary")
        findings = []
        if primary == "business_outcome" or primary not in ("pickup_rate", "engagement_rate"):
            findings.append({
                "metric": primary,
                "role": "primary",
                "status": "unavailable",
                "causal": False,
                "reason": "business outcome needs unique concluded leads, which call rows do not provide",
            })
        elif baseline.get("engaged") is None and primary == "engagement_rate":
            findings.append({"metric": primary, "role": "primary", "status": "insufficient", "causal": False})
        else:
            findings.append(assess_change(baseline, observed, primary, thresholds, subject=workflow_id))
        guard = assess_guardrail(objective, baseline, observed, thresholds, subject=workflow_id)
        if guard is not None:
            findings.append(guard)
        primary_down = any(
            item.get("role") != "guardrail" and item.get("status") == "confirmed" and item.get("direction") == "down"
            for item in findings
        )
        guard_breach = any(item.get("role") == "guardrail" and item.get("status") == "confirmed" for item in findings)
        if not primary_down and not guard_breach:
            continue
        version_n = int(version)
        _audit_new(
            store,
            "degradation_detected",
            workflow_id,
            sim_ts,
            {
                "proposal_id": proposal["proposal_id"],
                "version": version_n,
                "primary_degraded": primary_down,
                "guardrail_breached": guard_breach,
                "findings": [
                    {"metric": item.get("metric"), "status": item.get("status"), "role": item.get("role"), "causal": False}
                    for item in findings
                ],
                "outcome": "confirmed",
                "causal": False,
            },
            {"proposal_id": proposal["proposal_id"], "version": version_n, "primary_degraded": primary_down, "guardrail_breached": guard_breach},
        )
        previous = proposal["evidence"].get("config_snapshot")
        if not isinstance(previous, dict) or not workflow:
            outcome = "missing_previous_config" if not isinstance(previous, dict) else "missing_workflow"
            _audit_new(
                store,
                "rollback_recommended",
                workflow_id,
                sim_ts,
                {"proposal_id": proposal["proposal_id"], "outcome": outcome},
                {"proposal_id": proposal["proposal_id"], "outcome": outcome},
            )
            continue
        harmful = _restore_reexposes_degraded_caller(store, proposal, current, previous, findings, settings)
        if harmful is not None:
            _audit_new(
                store,
                "rollback_recommended",
                workflow_id,
                sim_ts,
                {
                    "proposal_id": proposal["proposal_id"],
                    "outcome": "restore_reexposes_degraded_caller",
                    "causal": False,
                    "caller_health": harmful,
                },
                {"proposal_id": proposal["proposal_id"], "outcome": "restore_reexposes_degraded_caller"},
            )
            continue
        approvals = client.list_approvals(workflow_id)
        reason = "primary KPI degradation" if primary_down else "guardrail breach"
        evidence = {
            "explained": True,
            "explanation": (
                f"Post-change observations on version {version} show a confirmed {reason}. "
                "The comparison is observational. The recommendation is to restore the previous configuration."
            ),
            "config_version": int(current["version"]),
            "config_snapshot": current.get("config"),
            "baselines": _call_baseline(post),
            "rollback_of": proposal["proposal_id"],
            "causal": False,
            "effect_kind": "observed",
            "evidence_ids": [f"rollback:{proposal['proposal_id']}"],
            "affected_population": int(post["dials"]),
            "validation_run_id": None,
            "monitoring": findings,
        }
        observed_rate = None
        baseline_rate = None
        sample_n = int(post["dials"])
        if (
            primary == "engagement_rate"
            and post["connected"]
            and baseline.get("connected")
            and baseline.get("engaged") is not None
        ):
            observed_rate = post["engaged"] / post["connected"]
            baseline_rate = int(baseline["engaged"]) / int(baseline["connected"])
            sample_n = int(post["connected"])
        elif post["dials"] and baseline.get("dials"):
            observed_rate = post["connected"] / post["dials"]
            baseline_rate = int(baseline["connected"]) / int(baseline["dials"])
        impact = assess_impact(
            primary_kpi=primary if primary in ("pickup_rate", "engagement_rate") else "pickup_rate",
            guardrail_kpi=(guard or {}).get("metric"),
            baseline_rate=baseline_rate,
            observed_rate=observed_rate,
            sample_n=sample_n,
            ci_low=None,
            ci_high=None,
            affected_population=int(post["dials"]),
        )
        built = build_proposal(
            workflow,
            current,
            previous,
            evidence,
            impact,
            sim_ts=sim_ts,
            pending_approvals=_pending(approvals),
            recent_requests=_recent_requests(approvals, sim_ts),
            rationale=evidence["explanation"],
        )
        if built["proposal"] is None:
            _audit_new(
                store,
                "rollback_recommended",
                workflow_id,
                sim_ts,
                {
                    "proposal_id": proposal["proposal_id"],
                    "outcome": "approval_or_policy",
                    "reason_codes": built["policy"].get("reason_codes"),
                    "causal": False,
                },
                {"proposal_id": proposal["proposal_id"], "outcome": "approval_or_policy"},
            )
            continue
        existing = store.get_proposal(built["proposal"]["proposal_id"])
        if existing is not None:
            continue
        _save(store, built["proposal"])
        _audit_new(
            store,
            "rollback_recommended",
            workflow_id,
            sim_ts,
            {"proposal_id": built["proposal"]["proposal_id"], "outcome": built["proposal"]["route"], "causal": False},
            {"proposal_id": built["proposal"]["proposal_id"], "outcome": built["proposal"]["route"]},
        )


def govern(store: Store, client, settings: RunSettings | None = None) -> dict:
    """Reconcile and, when policy and evidence allow, submit one change per workflow."""
    settings = settings or RunSettings()
    summary = {
        "mode": "live",
        "submitted": 0,
        "auto_applied": 0,
        "applied": 0,
        "verified": 0,
        "opportunities": 0,
        "rejected": 0,
    }
    if client is None or not hasattr(client, "list_approvals") or not hasattr(client, "workflows"):
        return {**summary, "mode": "unavailable"}
    try:
        server = client.state()
    except (AmbiguousResponse, SimError):
        return {**summary, "mode": "unavailable"}
    sim_ts = server.get("ts")
    for proposal in store.proposals_by_status(("submitting", "submitted", "pending", "uncertain", "applying", "applied")):
        current = _reconcile_one(store, client, proposal, sim_ts)
        if current["status"] == "approved":
            fresh = client.get_config(current["workflow_id"])
            current = _apply_approved(store, client, current, sim_ts, fresh)
        if current["status"] == "verified":
            summary["verified"] += 1
        elif current["route"] == "auto_apply" and current["status"] == "verified":
            summary["auto_applied"] += 1
    try:
        documents = client.workflows()
    except (AmbiguousResponse, SimError):
        return summary
    for workflow in documents:
        workflow_id = workflow.get("workflow_id")
        if not isinstance(workflow_id, str):
            continue
        if store.open_proposal_for(workflow_id) is not None:
            continue
        try:
            current = client.get_config(workflow_id)
            approvals = client.list_approvals(workflow_id)
        except (AmbiguousResponse, SimError):
            continue
        for opportunity in detect_opportunities(store, workflow, current, settings):
            summary["opportunities"] += 1
            evidence = opportunity.get("evidence")
            if isinstance(evidence, dict):
                evidence["observed_sim_ts"] = sim_ts
            previous = store.get_opportunity(opportunity["opportunity_id"])
            store.record_opportunity({**opportunity, "sim_ts": sim_ts})
            if _opportunity_decision_changed(previous, opportunity):
                _audit(
                    store,
                    "opportunity_detected",
                    workflow_id,
                    sim_ts,
                    {
                        "opportunity_id": opportunity["opportunity_id"],
                        "actionable": opportunity["actionable"],
                        "outcome": opportunity["reason"],
                        "evidence_ids": evidence.get("evidence_ids") if isinstance(evidence, dict) else None,
                        "pickup_verdict": evidence.get("pickup_verdict") if isinstance(evidence, dict) else None,
                        "engagement_verdict": evidence.get("engagement_verdict") if isinstance(evidence, dict) else None,
                        "worst_caller_id": evidence.get("worst_caller_id") if isinstance(evidence, dict) else None,
                        "best_caller_id": evidence.get("best_caller_id") if isinstance(evidence, dict) else None,
                    },
                )
            if not opportunity["actionable"] or opportunity.get("proposed_config") is None:
                continue
            worst = opportunity["worst"]
            best = opportunity["best"]
            comparison = opportunity["comparison"]
            pre = next(
                (
                    row
                    for row in call_groups(store, workflow_id, "config_version", kind="events")
                    if int(row["key"]) == int(current["version"])
                ),
                None,
            )
            evidence = {
                **opportunity["evidence"],
                "baselines": _call_baseline(pre),
                "baseline_n": None if pre is None else int(pre["dials"]),
                "baseline_successes": None if pre is None else int(pre["connected"]),
            }
            impact = assess_impact(
                primary_kpi="pickup_rate",
                guardrail_kpi="engagement_rate",
                baseline_rate=worst["pickup_rate"],
                observed_rate=best["pickup_rate"],
                sample_n=int(worst["dials"] + best["dials"]),
                ci_low=comparison.left.low,
                ci_high=comparison.left.high,
                affected_population=int(worst["dials"] + best["dials"]),
            )
            rationale = (
                f"Observed pickup on caller id {worst['key']} is {worst['pickup_rate']:.4f} "
                f"({worst['dials']} dials) and on {best['key']} is {best['pickup_rate']:.4f} "
                f"({best['dials']} dials). The comparison is {comparison.verdict}. "
                "No predicted uplift is claimed. The change only moves weight within policy."
            )
            built = build_proposal(
                workflow,
                current,
                opportunity["proposed_config"],
                evidence,
                impact,
                sim_ts=sim_ts,
                pending_approvals=_pending(approvals),
                recent_requests=_recent_requests(approvals, sim_ts),
                rationale=rationale,
            )
            policy = built["policy"]
            if built["proposal"] is None:
                summary["rejected"] += 1
                _audit(
                    store,
                    "policy_rejected",
                    workflow_id,
                    sim_ts,
                    {
                        "opportunity_id": opportunity["opportunity_id"],
                        "reason_codes": policy.get("reason_codes"),
                        "decision": policy.get("decision"),
                        "version": current.get("version"),
                    },
                )
                continue
            existing = store.get_proposal(built["proposal"]["proposal_id"])
            if existing is not None and existing["status"] in TERMINAL:
                continue
            _save(store, built["proposal"])
            _audit(
                store,
                "proposal_created",
                workflow_id,
                sim_ts,
                {
                    "proposal_id": built["proposal"]["proposal_id"],
                    "route": built["proposal"]["route"],
                    "version": current.get("version"),
                    "evidence_ids": evidence.get("evidence_ids"),
                    "outcome": "drafted",
                },
            )
            if not policy.get("ready_to_submit"):
                _audit(
                    store,
                    "approval_deferred",
                    workflow_id,
                    sim_ts,
                    {"proposal_id": built["proposal"]["proposal_id"], "reason_codes": policy.get("reason_codes")},
                )
                continue
            submitted = _submit(store, client, store.get_proposal(built["proposal"]["proposal_id"]), sim_ts)
            if submitted["route"] == "auto_apply" and submitted["status"] == "verified":
                summary["auto_applied"] += 1
                summary["verified"] += 1
            elif submitted["status"] in ("pending", "submitted", "approved"):
                summary["submitted"] += 1
    for proposal in store.proposals_by_status(("approved",)):
        fresh = client.get_config(proposal["workflow_id"])
        done = _apply_approved(store, client, proposal, sim_ts, fresh)
        if done["status"] == "verified":
            summary["applied"] += 1
            summary["verified"] += 1
    _monitor_applied(store, client, settings, sim_ts)
    for proposal in store.proposals_by_status(("drafted",)):
        if proposal["route"] not in ("auto_apply", "approval"):
            continue
        if proposal.get("outcome") == "rate_limited":
            _mark(store, proposal, "drafted", outcome="retry_after_rate_limit")
            continue
        fresh = client.get_config(proposal["workflow_id"])
        approvals = client.list_approvals(proposal["workflow_id"])
        documents = {item["workflow_id"]: item for item in client.workflows() if item.get("workflow_id")}
        workflow = documents.get(proposal["workflow_id"])
        if workflow is None:
            continue
        decision = evaluate_policy(
            workflow,
            fresh,
            proposal["proposed_config"],
            sim_ts=sim_ts,
            pending_approvals=_pending(approvals),
            recent_requests=_recent_requests(approvals, sim_ts),
            evidence_version=proposal["evidence"].get("config_version"),
            validation_run_id=proposal["evidence"].get("validation_run_id"),
        )
        if decision["decision"] == "rejected":
            _mark(store, proposal, "policy_rejected", outcome=",".join(decision["reason_codes"]), policy=decision)
            continue
        if not decision.get("ready_to_submit"):
            continue
        proposal = {**proposal, "policy": decision, "route": decision["route"]}
        _save(store, proposal)
        done = _submit(store, client, proposal, sim_ts)
        if done["status"] == "verified" and proposal["evidence"].get("rollback_of"):
            _audit(store, "rollback_applied", proposal["workflow_id"], sim_ts, {"proposal_id": proposal["proposal_id"], "outcome": "verified"})
            summary["applied"] += 1
    return summary
