"""Policy decisions for one proposed configuration change.

Numbers come from the workflow document returned by the simulator. This module
does not contain scenario-specific limits. Missing or ambiguous policy rejects
the change. A high impact score is not an input and cannot authorize a write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Documented configuration dimensions. The simulator diffs and total-variation
# check use this order. These are API fields, not a workflow's allowed list.
AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")
# mid_flight "next_campaign_only" keeps these at the campaign-creation version.
LOCKED_UNTIL_NEXT_CAMPAIGN = frozenset({"cohorts", "welcome_message", "voice", "caller_id"})
SHIFT_TOLERANCE = 1e-9

REQUIRED_GOVERNANCE = (
    "auto_apply",
    "approver_hours",
    "approver_latency_minutes",
    "proposal_ttl_hours",
    "apply_window_hours",
    "max_requests_per_24h",
)


def distribution(config: dict, cohort: str, dimension: str) -> dict:
    allocations = config.get("allocations") or {}
    default = (allocations.get("default") or {}).get(dimension) or {}
    specific = (allocations.get(cohort) or {}).get(dimension)
    return dict(specific or default)


def changed_dimensions(old: dict, new: dict) -> list[str]:
    """Dimensions that differ, in the documented change order."""
    changed: list[str] = []
    if old.get("cohorts") != new.get("cohorts"):
        changed.append("cohorts")
    keys = set((old.get("allocations") or {})) | set((new.get("allocations") or {}))
    for dimension in AGENT_DIMS:
        if any(distribution(old, key, dimension) != distribution(new, key, dimension) for key in keys):
            changed.append(dimension)
    if old.get("prompt_version") != new.get("prompt_version"):
        changed.append("prompt_version")
    if old.get("schedule") != new.get("schedule"):
        changed.append("schedule")
    return [dimension for dimension in CHANGE_DIMS if dimension in changed]


def total_variation(old: dict, new: dict) -> float:
    """Largest total-variation distance of an agent allocation across cohorts.

    A shift equal to the workflow maximum is allowed. The simulator uses the
    same 0.5 * L1 distance and a strict greater-than check.
    """
    old_ids = {row.get("id") for row in old.get("cohorts") or [] if isinstance(row, dict)}
    new_ids = {row.get("id") for row in new.get("cohorts") or [] if isinstance(row, dict)}
    keys = {"default"} | old_ids | new_ids
    worst = 0.0
    for key in keys:
        if not isinstance(key, str):
            continue
        for dimension in AGENT_DIMS:
            left = distribution(old, key, dimension)
            right = distribution(new, key, dimension)
            distance = 0.5 * sum(
                abs(float(left.get(option, 0)) - float(right.get(option, 0)))
                for option in set(left) | set(right)
            )
            worst = max(worst, distance)
    return worst


def _reject(reasons: list[str], evidence: dict, **extra: Any) -> dict:
    return {
        "decision": "rejected",
        "route": None,
        "ready_to_submit": False,
        "reason_codes": reasons,
        "policy_evidence": evidence,
        "current_version": extra.get("current_version"),
        "expected_version_after_apply": None,
        "changed_dims": extra.get("changed_dims") or [],
        "shift": extra.get("shift"),
        "max_shift": extra.get("max_shift"),
        "within_approver_hours": extra.get("within_approver_hours"),
        "expires_in_hours": extra.get("expires_in_hours"),
        "apply_window_hours": extra.get("apply_window_hours"),
        "approval_path": None,
    }


def within_approver_hours(sim_ts: str | None, hours: list) -> bool | None:
    """True when the simulator clock is inside [start, end). None if unknown."""
    if sim_ts is None or not isinstance(hours, list) or len(hours) != 2:
        return None
    try:
        moment = datetime.fromisoformat(sim_ts)
        start = int(hours[0])
        end = int(hours[1])
    except (TypeError, ValueError):
        return None
    if not 0 <= start < end <= 24:
        return None
    return start <= moment.hour < end


def _options_allow(workflow: dict, proposed: dict) -> list[str]:
    """Reject values the workflow document does not list. Missing options fail closed."""
    options = workflow.get("options")
    if not isinstance(options, dict):
        return ["missing_options"]
    reasons: list[str] = []
    prompt = proposed.get("prompt_version")
    allowed_prompts = options.get("prompt_version")
    if not isinstance(allowed_prompts, list) or prompt not in allowed_prompts:
        reasons.append("prompt_not_allowed")
    allocations = proposed.get("allocations")
    if not isinstance(allocations, dict) or "default" not in allocations:
        reasons.append("missing_default_allocation")
        return reasons
    for cohort, dims in allocations.items():
        if not isinstance(dims, dict):
            reasons.append("allocation_not_an_object")
            continue
        for dimension, weights in dims.items():
            if dimension not in AGENT_DIMS:
                reasons.append("unknown_allocation_dimension")
                continue
            allowed = options.get(dimension)
            if not isinstance(allowed, list):
                reasons.append("missing_dimension_options")
                continue
            if not isinstance(weights, dict) or not weights:
                reasons.append("empty_weights")
                continue
            if any(option not in allowed for option in weights):
                reasons.append("value_not_allowed")
            if any(not isinstance(weight, (int, float)) or weight < 0 for weight in weights.values()):
                reasons.append("weight_not_allowed")
    return reasons


def evaluate_policy(
    workflow: dict | None,
    current: dict | None,
    proposed: dict | None,
    *,
    sim_ts: str | None,
    pending_approvals: int | None,
    recent_requests: int | None,
    evidence_version: int | None,
    validation_run_id: str | None = None,
    proposal_age_hours: float | None = None,
) -> dict:
    """Return allowed routing for this exact change. Fail closed."""
    evidence: dict[str, Any] = {}
    if not isinstance(workflow, dict):
        return _reject(["missing_workflow"], evidence)
    governance = workflow.get("governance")
    if not isinstance(governance, dict):
        return _reject(["missing_policy"], evidence)
    missing = [key for key in REQUIRED_GOVERNANCE if key not in governance]
    if missing:
        return _reject(["ambiguous_policy"], {"missing": missing})
    auto = governance.get("auto_apply")
    if not isinstance(auto, dict) or "enabled" not in auto or "allowed_dims" not in auto:
        return _reject(["ambiguous_auto_apply"], {"auto_apply": auto})
    if not isinstance(auto.get("allowed_dims"), list):
        return _reject(["ambiguous_auto_apply"], {"auto_apply": auto})
    if auto.get("enabled") is True and not isinstance(auto.get("max_shift"), (int, float)):
        return _reject(["ambiguous_max_shift"], {"auto_apply": auto})
    if workflow.get("active") is False:
        return _reject(["workflow_inactive"], {"active": False})
    if not isinstance(current, dict) or not isinstance(proposed, dict):
        return _reject(["unknown_configuration"], evidence)
    try:
        current_version = int(current["version"])
    except (KeyError, TypeError, ValueError):
        return _reject(["unknown_configuration"], evidence)
    config = current.get("config")
    if not isinstance(config, dict):
        return _reject(["unknown_configuration"], evidence)
    if evidence_version is None or int(evidence_version) != current_version:
        return _reject(
            ["stale_configuration"],
            {"evidence_version": evidence_version, "current_version": current_version},
            current_version=current_version,
        )
    actor = current.get("actor")
    if actor == "supervisor" and evidence_version != current_version:
        return _reject(["supervisor_edit"], {"actor": actor}, current_version=current_version)
    # A supervisor edit is stale even when the caller passed the new version but
    # the evidence was computed on an older snapshot. The caller sets this by
    # passing evidence_version from the snapshot, which already mismatched above.
    # An explicit flag covers a same-version supervisor rewrite.
    if current.get("supervisor_after_evidence") is True:
        return _reject(["supervisor_edit"], {"actor": actor}, current_version=current_version)

    value_reasons = _options_allow(workflow, proposed)
    dims = changed_dimensions(config, proposed)
    shift = total_variation(config, proposed) if not value_reasons else None
    hours_ok = within_approver_hours(sim_ts, governance.get("approver_hours"))
    ttl = governance.get("proposal_ttl_hours")
    window = governance.get("apply_window_hours")
    extra = {
        "current_version": current_version,
        "changed_dims": dims,
        "shift": shift,
        "max_shift": auto.get("max_shift"),
        "within_approver_hours": hours_ok,
        "expires_in_hours": ttl,
        "apply_window_hours": window,
    }
    if value_reasons:
        return _reject(value_reasons, {"options": True}, **extra)
    if not dims:
        return _reject(["no_change"], {}, **extra)
    agent_changed = [dimension for dimension in dims if dimension in AGENT_DIMS]
    if agent_changed and shift == 0 and len(dims) == len(agent_changed):
        return _reject(["no_change"], {}, **extra)
    if pending_approvals is None:
        return _reject(["unknown_pending_approvals"], {}, **extra)
    if pending_approvals > 0:
        return _reject(["pending_approval"], {"pending_approvals": pending_approvals}, **extra)
    if recent_requests is None:
        return _reject(["unknown_request_rate"], {}, **extra)
    try:
        limit = int(governance["max_requests_per_24h"])
    except (TypeError, ValueError):
        return _reject(["ambiguous_policy"], {}, **extra)
    if recent_requests >= limit:
        return _reject(["rate_limit"], {"recent_requests": recent_requests, "limit": limit}, **extra)
    if proposal_age_hours is not None and isinstance(ttl, (int, float)) and proposal_age_hours > float(ttl):
        return _reject(["proposal_expired"], {"age_hours": proposal_age_hours, "ttl": ttl}, **extra)
    if "prompt_version" in dims and not validation_run_id:
        return _reject(["prompt_requires_validation"], {}, **extra)
    mid_flight = workflow.get("mid_flight")
    if mid_flight not in ("allowed", "next_campaign_only"):
        return _reject(["ambiguous_mid_flight"], {"mid_flight": mid_flight}, **extra)
    if mid_flight == "next_campaign_only" and LOCKED_UNTIL_NEXT_CAMPAIGN.intersection(dims):
        return _reject(["locked_until_next_campaign"], {"mid_flight": mid_flight, "dims": dims}, **extra)

    allowed_dims = list(auto["allowed_dims"])
    max_shift = auto.get("max_shift")
    auto_dims_ok = all(dimension in allowed_dims for dimension in dims)
    shift_ok = (
        auto.get("enabled") is True
        and isinstance(max_shift, (int, float))
        and shift is not None
        and shift <= float(max_shift) + SHIFT_TOLERANCE
    )
    evidence = {
        "auto_apply_enabled": auto.get("enabled") is True,
        "allowed_dims": allowed_dims,
        "max_shift": max_shift,
        "approver_hours": governance.get("approver_hours"),
        "approver_latency_minutes": governance.get("approver_latency_minutes"),
        "proposal_ttl_hours": ttl,
        "apply_window_hours": window,
        "max_requests_per_24h": limit,
        "min_validation_pass_rate": governance.get("min_validation_pass_rate"),
        "mid_flight": mid_flight,
        "shift": shift,
        "changed_dims": dims,
    }
    if auto.get("enabled") is True and auto_dims_ok and shift_ok:
        return {
            "decision": "auto_apply_eligible",
            "route": "auto_apply",
            "ready_to_submit": True,
            "reason_codes": ["within_auto_apply_policy"],
            "policy_evidence": evidence,
            "current_version": current_version,
            "expected_version_after_apply": current_version + 1,
            "changed_dims": dims,
            "shift": shift,
            "max_shift": max_shift,
            "within_approver_hours": hours_ok,
            "expires_in_hours": ttl,
            "apply_window_hours": window,
            "approval_path": None,
        }
    if hours_ok is None:
        return _reject(["ambiguous_approver_hours"], evidence, **extra)
    return {
        "decision": "approval_required",
        "route": "approval",
        "ready_to_submit": hours_ok is True,
        "reason_codes": ["approval_required"] if hours_ok else ["outside_approver_hours"],
        "policy_evidence": evidence,
        "current_version": current_version,
        "expected_version_after_apply": current_version + 1,
        "changed_dims": dims,
        "shift": shift,
        "max_shift": max_shift,
        "within_approver_hours": hours_ok,
        "expires_in_hours": ttl,
        "apply_window_hours": window,
        "approval_path": "POST /approvals then POST /workflows/{id}/apply",
    }
